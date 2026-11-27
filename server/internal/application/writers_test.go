package application

import (
	"context"
	"database/sql"
	"database/sql/driver"
	"encoding/json"
	"errors"
	"fmt"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/caltseng/phalanx/server/internal/contract"
	"github.com/caltseng/phalanx/server/internal/sqlite"
	driverSQLite "modernc.org/sqlite"
)

var dispatchReadCounterID atomic.Uint64

func countDispatchReads(t *testing.T, db *sql.DB) *atomic.Int64 {
	t.Helper()
	count := &atomic.Int64{}
	name := fmt.Sprintf("count_dispatch_reads_%d", dispatchReadCounterID.Add(1))
	if err := driverSQLite.RegisterScalarFunction(name, 0,
		func(_ *driverSQLite.FunctionContext, _ []driver.Value) (driver.Value, error) {
			count.Add(1)
			return int64(1), nil
		}); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`ALTER TABLE dispatch_settings RENAME TO dispatch_settings_rows;
		CREATE VIEW dispatch_settings AS SELECT * FROM dispatch_settings_rows WHERE ` + name + `()=1`); err != nil {
		t.Fatal(err)
	}
	return count
}

func TestWriterClaimsReadSettingsOnce(t *testing.T) {
	app, db := writerStore(t, 2, 1)
	count := countDispatchReads(t, db)
	ctx := context.Background()
	request := contract.WriterRegistration{ExpectedVersion: "1", CaptainID: "captain",
		Runtime: "synthetic-runtime", Nonce: "snapshot-nonce-0001", TokenDigest: contract.Digest("snapshot-token")}
	first, err := app.RegisterWriter(ctx, "up", "execution", request)
	if err != nil {
		t.Fatal(err)
	}
	if reads := count.Swap(0); reads != 1 {
		t.Fatal("new admission settings reads", reads)
	}
	_, oldID, _ := decodeBinding(t, first)
	if _, err := db.Exec("UPDATE writers SET building=0 WHERE id=?", oldID); err != nil {
		t.Fatal(err)
	}
	request.ExpectedVersion, request.Nonce = "2", "snapshot-nonce-0002"
	request.TokenDigest = contract.Digest("replacement-snapshot-token")
	request.Replaces, request.ReplacedStopped = &oldID, true
	replaced, err := app.RegisterWriter(ctx, "up", "execution", request)
	if err != nil {
		t.Fatal(err)
	}
	if reads := count.Swap(0); reads != 1 {
		t.Fatal("replacement claim settings reads", reads)
	}
	_, id, attempt := decodeBinding(t, replaced)
	if _, err := db.Exec("UPDATE attempts SET ended_at=? WHERE id=?", timestamp(time.Now()), *attempt); err != nil {
		t.Fatal(err)
	}
	queued, err := contract.Canonical(Start(MergedTarget, ReadyMode))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec("UPDATE executions SET state_json=?,ready_since=? WHERE id='execution'", queued, time.Now().UnixMilli()-1000); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`UPDATE writers SET building=0 WHERE id=?;
		UPDATE dispatch_settings_rows SET paused=1`, id); err != nil {
		t.Fatal(err)
	}
	blocked, err := app.WriterNext(ctx, id)
	if err != nil {
		t.Fatal(err)
	}
	if reads := count.Load(); reads != 1 {
		t.Fatal("blocked claim and binding did not share settings", reads)
	}
	var response struct {
		Writer struct {
			Building     bool    `json:"building"`
			CapacityWait *string `json:"capacity_wait"`
		} `json:"writer"`
	}
	_, _, blockedAttempt := decodeBinding(t, blocked)
	if err := json.Unmarshal(blocked, &response); err != nil {
		t.Fatal(err)
	}
	if blockedAttempt != nil || response.Writer.Building || response.Writer.CapacityWait == nil ||
		*response.Writer.CapacityWait != "Dispatch is paused" {
		t.Fatal("paused claim lost its allocation wait", string(blocked))
	}
}

func TestCaptainLivenessBoundariesAgreeAcrossViews(t *testing.T) {
	for _, ending := range []bool{false, true} {
		for _, offset := range []time.Duration{-time.Microsecond, 0, time.Microsecond} {
			t.Run(fmt.Sprintf("ending=%t/offset=%s", ending, offset), func(t *testing.T) {
				app, db := writerStore(t, 2, 1)
				now := time.Now().UTC().Truncate(time.Microsecond)
				app.Clock = func() time.Time { return now }
				heartbeat := timestamp(now.Add(-captainHeartbeatTimeout - offset))
				var endingAt *string
				if ending {
					heartbeat = timestamp(now)
					endingAt = pointer(timestamp(now.Add(-captainHandoffGrace - offset)))
				}
				if _, err := db.Exec("UPDATE captains SET heartbeat_at=?,ending_at=?", heartbeat, endingAt); err != nil {
					t.Fatal(err)
				}
				wantLive := offset < 0
				if err := app.Store.Read(context.Background(), func(tx *sqlite.Tx) error {
					current, err := pickupSnapshot(tx, now)
					if err != nil {
						return err
					}
					_, live := current.Live["captain"]
					if live != wantLive {
						t.Fatal("pickup liveness", live, wantLive)
					}
					return nil
				}); err != nil {
					t.Fatal(err)
				}
				raw, err := app.Captain(context.Background(), "captain")
				if err != nil {
					t.Fatal(err)
				}
				var captain CaptainView
				if err := json.Unmarshal(raw, &captain); err != nil {
					t.Fatal(err)
				}
				if captain.Live != wantLive || captain.Stale == wantLive {
					t.Fatal("Captain view liveness", string(raw))
				}
				raw, err = app.CurrentCaptain(context.Background())
				if err != nil {
					t.Fatal(err)
				}
				var current struct {
					Captain *struct {
						ID string `json:"id"`
					} `json:"captain"`
				}
				if err := json.Unmarshal(raw, &current); err != nil {
					t.Fatal(err)
				}
				if (current.Captain != nil) != wantLive {
					t.Fatal("current routing liveness", string(raw))
				}
			})
		}
	}
}

func TestArchiveStatusRetainsReplacementAndCaptainBlockers(t *testing.T) {
	app, db := writerStore(t, 2, 1)
	ctx := context.Background()
	stamp := timestamp(time.Now())
	session := "00000000-0000-0000-0000-000000000001"
	reference := "ghapp://sessions/" + session
	tx, err := db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	for index := range 12 {
		id := fmt.Sprintf("archived-%02d", index)
		if _, err := tx.Exec(`INSERT INTO captains(id,session_ref,charter_revision,state,created_at,heartbeat_at,handoff_note)
			VALUES (?,?,'charter','ended',?,?,'Stop condition met: complete')`, id, reference, stamp, stamp); err != nil {
			t.Fatal(err)
		}
		if _, err := tx.Exec(`INSERT INTO writers
			(id,execution_id,item_id,captain_id,runtime,nonce,token_digest,state,created_at,ended_at,session_id)
			VALUES (?,'execution','up',?,'synthetic-runtime',?,?,'stopped',?,?,?)`,
			id, id, id, contract.Digest(id), stamp, stamp, session); err != nil {
			t.Fatal(err)
		}
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}
	check := func(wantActive bool) {
		t.Helper()
		raw, err := app.ArchiveStatus(ctx, session)
		if err != nil {
			t.Fatal(err)
		}
		var view ArchiveStatusView
		if err := json.Unmarshal(raw, &view); err != nil {
			t.Fatal(err)
		}
		if view.Active != wantActive || len(view.Writers) != 12 {
			t.Fatal("archive blockers or writer membership changed", string(raw))
		}
	}
	// Archiving a writer session does not discard its Captains' unfinished cleanup.
	check(true)
	if _, err := app.RecordArchive(ctx, contract.SessionArchive{SessionID: session, Outcome: "refused",
		Reason: "Retain local work", ReportedBy: "fixture"}); err != nil {
		t.Fatal(err)
	}
	check(false)
	if _, err := db.Exec("UPDATE captains SET handoff_note=NULL WHERE id='archived-00'"); err != nil {
		t.Fatal(err)
	}
	check(true)
	if _, err := db.Exec(`INSERT INTO captains(id,session_ref,charter_revision,state,created_at,heartbeat_at,parent_id)
		VALUES ('successor','successor-session','charter','ended',?,?,'archived-00')`, stamp, stamp); err != nil {
		t.Fatal(err)
	}
	check(false)
	registered, err := app.RegisterWriter(ctx, "up", "execution", contract.WriterRegistration{
		ExpectedVersion: "1", CaptainID: "captain", Runtime: "synthetic-runtime",
		Nonce: "archive-replacement-0001", TokenDigest: contract.Digest("replacement")})
	if err != nil {
		t.Fatal(err)
	}
	_, writerID, _ := decodeBinding(t, registered)
	if _, err := db.Exec("UPDATE writers SET session_id='different-session' WHERE id=?", writerID); err != nil {
		t.Fatal(err)
	}
	check(true)
	if _, err := app.RecordArchive(ctx, contract.SessionArchive{SessionID: session, Outcome: "archived",
		Reason: "Attempt unsafe cleanup", ReportedBy: "fixture"}); err == nil {
		t.Fatal("another session's replacement writer did not prevent archive")
	}
}

func TestCaptainListRetainsPerCaptainCleanupLimit(t *testing.T) {
	app, db := writerStore(t, 2, 1)
	stamp := timestamp(time.Now())
	if _, err := db.Exec(`INSERT INTO captains(id,session_ref,charter_revision,state,created_at,heartbeat_at)
		VALUES ('second','second-session','charter','active',?,?)`, stamp, stamp); err != nil {
		t.Fatal(err)
	}
	tx, err := db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	for _, captainID := range []string{"captain", "second"} {
		for index := range 103 {
			id := fmt.Sprintf("%s-%03d", captainID, index)
			if _, err := tx.Exec(`INSERT INTO writers
				(id,execution_id,item_id,captain_id,runtime,nonce,token_digest,state,created_at,ended_at,session_id)
				VALUES (?,'execution','up',?,'synthetic-runtime',?,?,'stopped',?,?,?)`,
				id, captainID, id, contract.Digest(id), stamp, stamp, id); err != nil {
				t.Fatal(err)
			}
		}
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}
	raw, err := app.Captains(context.Background(), 100)
	if err != nil {
		t.Fatal(err)
	}
	var view struct {
		Captains []struct {
			ID             string `json:"id"`
			CleanupPending int    `json:"cleanup_pending"`
			EndedWriters   []struct {
				ID string `json:"id"`
			} `json:"ended_writers"`
		} `json:"captains"`
	}
	if err := json.Unmarshal(raw, &view); err != nil {
		t.Fatal(err)
	}
	if len(view.Captains) != 2 {
		t.Fatal(string(raw))
	}
	for _, captain := range view.Captains {
		if captain.CleanupPending != 103 || len(captain.EndedWriters) != 100 ||
			captain.EndedWriters[0].ID != captain.ID+"-000" || captain.EndedWriters[99].ID != captain.ID+"-099" {
			t.Fatal("cleanup count, ordering, or per-Captain limit changed", string(raw))
		}
	}
}

func writerStore(t *testing.T, total, building int64) (*App, *sql.DB) {
	t.Helper()
	store, db := dependencyStore(t)
	stamp := timestamp(time.Now())
	if _, err := db.Exec("INSERT INTO dispatch_settings VALUES (1,0,?,?)", total, building); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`INSERT INTO captains(id,session_ref,charter_revision,state,created_at,heartbeat_at)
		VALUES ('captain','synthetic-session','synthetic-charter','active',?,?)`, stamp, stamp); err != nil {
		t.Fatal(err)
	}
	putDependencyState(t, db, "execution", nil, Start(MergedTarget, ReadyMode))
	if _, err := db.Exec("UPDATE executions SET ready_since=?", time.Now().UnixMilli()-1000); err != nil {
		t.Fatal(err)
	}
	return New(store), db
}

func TestWriterRegistrationRefusesNullStoredDefaultsWithoutEffects(t *testing.T) {
	for _, field := range []string{"publication_mode"} {
		t.Run(field, func(t *testing.T) {
			app, db := writerStore(t, 1, 1)
			if _, err := db.Exec("UPDATE executions SET state_json=json_set(state_json,?,NULL) WHERE id='execution'", "$."+field); err != nil {
				t.Fatal(err)
			}
			var before, after string
			if err := db.QueryRow("SELECT state_json FROM executions WHERE id='execution'").Scan(&before); err != nil {
				t.Fatal(err)
			}
			_, err := app.RegisterWriter(context.Background(), "up", "execution", contract.WriterRegistration{
				ExpectedVersion: "1", CaptainID: "captain", Runtime: "synthetic-runtime",
				Nonce: "synthetic-null-0001", TokenDigest: contract.Digest("null-token")})
			if err == nil {
				t.Fatal("null authoritative state permitted writer registration")
			}
			var version, writers, attempts, history int
			if err := db.QueryRow(`SELECT version,state_json,(SELECT count(*) FROM writers),
				(SELECT count(*) FROM attempts),(SELECT count(*) FROM transition_history)
				FROM executions WHERE id='execution'`).Scan(&version, &after, &writers, &attempts, &history); err != nil {
				t.Fatal(err)
			}
			if before != after || version != 1 || writers != 0 || attempts != 0 || history != 0 {
				t.Fatal("refused registration changed state/grants/history", version, writers, attempts, history)
			}
			if _, err := db.Exec("UPDATE executions SET state_json=json_remove(state_json,?) WHERE id='execution'", "$."+field); err != nil {
				t.Fatal(err)
			}
			registered, err := app.RegisterWriter(context.Background(), "up", "execution", contract.WriterRegistration{
				ExpectedVersion: "1", CaptainID: "captain", Runtime: "synthetic-runtime",
				Nonce: "synthetic-null-0001", TokenDigest: contract.Digest("null-token")})
			if err != nil {
				t.Fatal("omitted publication mode no longer permits defaulted registration", err)
			}
			row, _, _ := decodeBinding(t, registered)
			state, err := readState(row.State)
			if err != nil || state.PublicationMode != ReadyMode {
				t.Fatal("omitted mode did not preserve the authoritative ready default", state, err)
			}
		})
	}
}

func decodeBinding(t *testing.T, raw json.RawMessage) (sqlite.Execution, string, *string) {
	t.Helper()
	var result struct {
		sqlite.Execution
		Writer struct {
			ID        string  `json:"id"`
			AttemptID *string `json:"attempt_id"`
		} `json:"writer"`
	}
	if err := json.Unmarshal(raw, &result); err != nil {
		t.Fatal(err)
	}
	return result.Execution, result.Writer.ID, result.Writer.AttemptID
}

func TestWriterRegistrationAtomicallyClaimsAndFencesNonce(t *testing.T) {
	app, db := writerStore(t, 2, 1)
	request := contract.WriterRegistration{ExpectedVersion: "1", CaptainID: "captain",
		Runtime: "synthetic-runtime", Nonce: "synthetic-nonce-0001", TokenDigest: contract.Digest("synthetic-token")}
	first, err := app.RegisterWriter(context.Background(), "up", "execution", request)
	if err != nil {
		t.Fatal(err)
	}
	execution, writerID, attemptID := decodeBinding(t, first)
	if execution.Version != 2 || attemptID == nil {
		t.Fatal("registration did not atomically open the first attempt", string(first))
	}
	var stage, historyAttempt string
	if err := db.QueryRow("SELECT stage FROM attempts WHERE id=?", *attemptID).Scan(&stage); err != nil || stage != "development" {
		t.Fatal(stage, err)
	}
	if err := db.QueryRow("SELECT attempt_id FROM transition_history WHERE execution_id='execution' AND version=2").
		Scan(&historyAttempt); err != nil || historyAttempt != *attemptID {
		t.Fatal("claim history did not retain the exact attempt", historyAttempt, err)
	}
	request.ExpectedVersion = "99"
	replayed, err := app.RegisterWriter(context.Background(), "up", "execution", request)
	if err != nil || string(replayed) != string(first) {
		t.Fatal("nonce replay reopened or changed a writer allocation", string(replayed), err)
	}
	request.Runtime = "another-runtime"
	if _, err := app.RegisterWriter(context.Background(), "up", "execution", request); err == nil {
		t.Fatal("changed nonce registration admitted")
	}
	next, err := app.WriterNext(context.Background(), writerID)
	if err != nil || string(next) != string(first) {
		t.Fatal("next attempt re-claimed a current attempt", string(next), err)
	}
}

func TestRacingWriterAdmissionsCannotOversubscribeBuilding(t *testing.T) {
	app, db := writerStore(t, 2, 1)
	if _, err := db.Exec("INSERT INTO work_items VALUES ('another','fixture','{}','stamp')"); err != nil {
		t.Fatal(err)
	}
	state, err := contract.Canonical(Start(MergedTarget, ReadyMode))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`INSERT INTO executions
		(id,item_id,version,state_json,created_at,updated_at,queue_repository,queue_priority,ready_since)
		VALUES ('another-execution','another',1,?,'stamp','stamp','fixture',1,?)`,
		state, time.Now().UnixMilli()-1000); err != nil {
		t.Fatal(err)
	}
	requests := []struct{ item, execution, nonce string }{
		{"up", "execution", "synthetic-racing-0001"},
		{"another", "another-execution", "synthetic-racing-0002"},
	}
	results := make([]error, len(requests))
	var workers sync.WaitGroup
	for index, request := range requests {
		workers.Add(1)
		go func(index int, item, execution, nonce string) {
			defer workers.Done()
			_, results[index] = app.RegisterWriter(context.Background(), item, execution, contract.WriterRegistration{
				ExpectedVersion: "1", CaptainID: "captain", Runtime: "synthetic-runtime",
				Nonce: nonce, TokenDigest: contract.Digest(nonce)})
		}(index, request.item, request.execution, request.nonce)
	}
	workers.Wait()
	successes := 0
	for _, err := range results {
		if err == nil {
			successes++
		} else {
			var conflict *Conflict
			if !errors.As(err, &conflict) {
				t.Fatal(err)
			}
		}
	}
	var writers, attempts int
	if err := db.QueryRow("SELECT count(*) FROM writers WHERE ended_at IS NULL").Scan(&writers); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRow("SELECT count(*) FROM attempts WHERE ended_at IS NULL").Scan(&attempts); err != nil {
		t.Fatal(err)
	}
	if successes != 1 || writers != 1 || attempts != 1 {
		t.Fatal("registration leaked an allocation/claim across the capacity refusal", successes, writers, attempts)
	}
}

func TestStoppedReplacementTransfersExactBuildingAllocation(t *testing.T) {
	app, db := writerStore(t, 2, 2)
	request := contract.WriterRegistration{ExpectedVersion: "1", CaptainID: "captain",
		Runtime: "synthetic-runtime", Nonce: "synthetic-replace-0001", TokenDigest: contract.Digest("old-token")}
	first, err := app.RegisterWriter(context.Background(), "up", "execution", request)
	if err != nil {
		t.Fatal(err)
	}
	_, oldID, oldAttempt := decodeBinding(t, first)
	if _, err := db.Exec("UPDATE dispatch_settings SET max_writers=1,max_building_writers=1"); err != nil {
		t.Fatal(err)
	}
	request.ExpectedVersion, request.Nonce, request.TokenDigest = "2", "synthetic-replace-0002", contract.Digest("new-token")
	request.Replaces, request.ReplacedStopped = &oldID, true
	replaced, err := app.RegisterWriter(context.Background(), "up", "execution", request)
	if err != nil {
		t.Fatal(err)
	}
	execution, newID, newAttempt := decodeBinding(t, replaced)
	if newID == oldID || newAttempt == nil || *newAttempt == *oldAttempt || execution.Version != 4 {
		t.Fatal("replacement did not fence/retry/claim under the new writer", string(replaced))
	}
	if err := app.Store.Read(context.Background(), func(tx *sqlite.Tx) error {
		old, err := tx.WriterRecord(oldID)
		if err != nil {
			return err
		}
		current, err := tx.WriterRecord(newID)
		if err != nil {
			return err
		}
		capacity, err := tx.DispatchSettings()
		if err != nil {
			return err
		}
		if old.EndedAt == nil || old.Building || old.AttemptID != nil ||
			!current.Building || capacity.ActiveWriters != 1 || capacity.BuildingWriters != 1 {
			t.Fatal("stopped replacement duplicated or lost its exact allocation", old, current, capacity)
		}
		return nil
	}); err != nil {
		t.Fatal(err)
	}
}

func TestCapacityHandoffDoesNotNormalizeEmptyDependencyWait(t *testing.T) {
	app, db := writerStore(t, 1, 1)
	first, err := app.RegisterWriter(context.Background(), "up", "execution", contract.WriterRegistration{
		ExpectedVersion: "1", CaptainID: "captain", Runtime: "synthetic-runtime",
		Nonce: "synthetic-handoff-0001", TokenDigest: contract.Digest("handoff-token")})
	if err != nil {
		t.Fatal(err)
	}
	_, id, _ := decodeBinding(t, first)
	state := Start(MergedTarget, ReadyMode)
	state.Stage, state.Status = Delivery, WaitingForExternal
	state.PrimaryPR = pointer("https://github.com/example/fixture/pull/1")
	text, err := contract.Canonical(state)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec("UPDATE executions SET state_json=? WHERE id='execution'", text); err != nil {
		t.Fatal(err)
	}
	if _, err := app.WriterCapacity(context.Background(), id,
		contract.WriterCapacity{Phase: "delivering", ExpectedRevision: "0"}); err == nil {
		t.Fatal("Building released without a reviewed head handoff")
	}
	head := "0123456789012345678901234567890123456789"
	if _, err := app.WriterCapacity(context.Background(), id,
		contract.WriterCapacity{Phase: "delivering", ExpectedRevision: "0", HandoffHead: &head}); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec("UPDATE executions SET dependency_wait_json='[]' WHERE id='execution'"); err != nil {
		t.Fatal(err)
	}
	if _, err := app.WriterCapacity(context.Background(), id,
		contract.WriterCapacity{Phase: "building", ExpectedRevision: "1"}); err == nil {
		t.Fatal("non-null dependency wait was normalized away by its empty projection")
	}
	if _, err := db.Exec("UPDATE executions SET dependency_wait_json=NULL WHERE id='execution'"); err != nil {
		t.Fatal(err)
	}
	if _, err := app.WriterCapacity(context.Background(), id,
		contract.WriterCapacity{Phase: "building", ExpectedRevision: "1"}); err != nil {
		t.Fatal(err)
	}
}

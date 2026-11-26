package application

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"sync"
	"testing"
	"time"

	"github.com/caltseng/phalanx/server/internal/contract"
	"github.com/caltseng/phalanx/server/internal/sqlite"
)

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

package application

import (
	"context"
	"encoding/json"
	"sync"
	"testing"
	"time"

	"github.com/caltseng/phalanx/server/internal/contract"
	"github.com/caltseng/phalanx/server/internal/sqlite"
)

func TestUsageRacingReplayRetainsMeasurementsWithoutLifecycleEffects(t *testing.T) {
	app, db := writerStore(t, 1, 1)
	ctx := context.Background()
	registered, err := app.RegisterWriter(ctx, "up", "execution", contract.WriterRegistration{
		ExpectedVersion: "1", CaptainID: "captain", Runtime: "synthetic-runtime",
		Nonce: "synthetic-usage-0001", TokenDigest: contract.Digest("usage-token")})
	if err != nil {
		t.Fatal(err)
	}
	row, writerID, attemptID := decodeBinding(t, registered)
	if _, err := app.UpdateWriter(ctx, "up", "execution", contract.WriterUpdate{
		CaptainID: "captain", WriterID: writerID, SessionID: pointer("synthetic-session")}); err != nil {
		t.Fatal(err)
	}
	before, err := app.WriterNext(ctx, writerID)
	if err != nil {
		t.Fatal(err)
	}
	stamp := contract.AwareTime{Time: time.Now().UTC()}
	input, output := json.Number("10"), json.Number("0")
	snapshot := contract.UsageSnapshot{Runtime: "synthetic-runtime", Provider: "synthetic-provider",
		Account: "synthetic-account", Origin: "synthetic-origin", SessionID: "synthetic-session",
		Source: "synthetic-source", SourceVersion: "v1", Epoch: "synthetic-epoch", Sequence: "1",
		WindowStart: stamp, Cutoff: stamp, ObservedAt: stamp, Scope: "item_exclusive", Status: "observed",
		Reason: "Synthetic coverage", Tokens: contract.Tokens{Input: &input, Output: &output}}
	results := make([]json.RawMessage, 2)
	failures := make([]error, 2)
	var workers sync.WaitGroup
	for index := range 2 {
		workers.Add(1)
		go func(index int) {
			defer workers.Done()
			results[index], failures[index] = app.OwnUsage(ctx, writerID, snapshot)
		}(index)
	}
	workers.Wait()
	accepted := 0
	for index, err := range failures {
		if err != nil {
			t.Fatal(err)
		}
		var response struct{ Accepted bool }
		if err := json.Unmarshal(results[index], &response); err != nil {
			t.Fatal(err)
		}
		if response.Accepted {
			accepted++
		}
	}
	if accepted != 1 {
		t.Fatal("racing identical snapshots were both accepted", results)
	}
	var version int64
	var ended *string
	if err := db.QueryRow("SELECT version FROM executions WHERE id='execution'").Scan(&version); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRow("SELECT ended_at FROM attempts WHERE id=?", *attemptID).Scan(&ended); err != nil {
		t.Fatal(err)
	}
	if version != row.Version || ended != nil {
		t.Fatal("usage affected execution or current attempt", version, ended, string(before))
	}
	snapshot.Sequence, snapshot.Status, snapshot.Reason, snapshot.Tokens = "2", "unavailable", "Synthetic acquisition gap", contract.Tokens{}
	if _, err := app.OwnUsage(ctx, writerID, snapshot); err != nil {
		t.Fatal(err)
	}
	var projection map[string]any
	if err := app.Store.Read(ctx, func(tx *sqlite.Tx) error {
		var err error
		projection, err = usageProjection(tx, "up", "execution")
		return err
	}); err != nil {
		t.Fatal(err)
	}
	execution := projection["execution"].(map[string]any)
	tokens := execution["tokens"].(map[string]any)
	if execution["status"] != "partial" || tokens["input"] != json.Number("10") ||
		tokens["output"] != json.Number("0") || tokens["cache_read"] != nil || execution["covered_sessions"] != 1 {
		t.Fatal("acquisition gap erased measurements, synthesized zero, or changed coverage", projection)
	}
	sessions := projection["sessions"].([]usageSession)
	if sessions[0].Status != "partial" || sessions[0].Reason != "Synthetic acquisition gap" ||
		sessions[0].AcquisitionError == nil {
		t.Fatal(projection)
	}
}

func TestUsageSubtotalExcludesSharedSessionsAndPreservesOverflow(t *testing.T) {
	counter := json.Number("9007199254740991")
	stamp := contract.AwareTime{Time: time.Now().UTC()}
	snapshot := contract.UsageSnapshot{WindowStart: stamp, Cutoff: stamp, ObservedAt: stamp,
		Scope: "item_exclusive", Status: "observed", Reason: "Synthetic coverage",
		Tokens: contract.Tokens{Input: &counter}}
	raw, err := contract.PythonJSON(snapshot)
	if err != nil {
		t.Fatal(err)
	}
	ended := stamp.Format(time.RFC3339Nano)
	exclusive, err := projectedUsage(sqlite.UsageProjectionRow{WriterID: "one", SnapshotJSON: &raw, EndedAt: &ended, Bindings: 1})
	if err != nil {
		t.Fatal(err)
	}
	shared, err := projectedUsage(sqlite.UsageProjectionRow{WriterID: "shared", SnapshotJSON: &raw, EndedAt: &ended, Bindings: 2})
	if err != nil {
		t.Fatal(err)
	}
	subtotal, err := usageSubtotal([]usageSession{exclusive, exclusive, shared})
	if err != nil {
		t.Fatal(err)
	}
	tokens := subtotal["tokens"].(map[string]any)
	if tokens["input"] != "18014398509481982" || tokens["output"] != nil || subtotal["covered_sessions"] != 2 ||
		subtotal["bound_sessions"] != 3 || subtotal["status"] != "partial" || shared.Included {
		t.Fatal("subtotal billed shared context, lost precision, or invented coverage", subtotal, shared)
	}
}

package application

import (
	"context"
	"encoding/json"
	"testing"
	"time"

	"github.com/caltseng/phalanx/server/internal/contract"
	"github.com/caltseng/phalanx/server/internal/sqlite"
)

func TestUnavailableFinalizerReturnsCommittedPendingIntent(t *testing.T) {
	store, db := dependencyStore(t)
	putDependencyState(t, db, "stop", nil, Start(MergedTarget, ReadyMode))
	app := New(store)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	holder, err := db.Conn(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer holder.Close()
	app.afterIntent = func() {
		if _, err := holder.ExecContext(ctx, "BEGIN IMMEDIATE"); err != nil {
			t.Fatal(err)
		}
	}
	result, err := app.RequestStop(ctx, "up", "stop",
		contract.Cancellation{ExpectedVersion: "1", Reason: "Durable stop"}, Cancel, nil)
	if _, release := holder.ExecContext(context.Background(), "ROLLBACK"); release != nil {
		t.Fatal(release)
	}
	if err != nil {
		t.Fatal("committed intent was lost when phase-two store access failed", err)
	}
	var row sqlite.Execution
	if err := json.Unmarshal(responseJSON(t, result), &row); err != nil {
		t.Fatal(err)
	}
	state, err := readState(row.State)
	if err != nil || row.Version != 2 || state.PendingOutput == nil || *state.PendingOutput != Cancelled ||
		state.Status != WaitingForExternal || state.FinalOutput != nil {
		t.Fatal("store failure was normalized into a false closed success", string(responseJSON(t, result)), err)
	}
	var intents, reports int
	if err := db.QueryRow("SELECT count(*) FROM report_finalizations WHERE execution_id='stop'").Scan(&intents); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRow("SELECT count(*) FROM reports WHERE execution_id='stop'").Scan(&reports); err != nil {
		t.Fatal(err)
	}
	if intents != 1 || reports != 0 {
		t.Fatal("phase-two failure lost intent or invented stored Markdown", intents, reports)
	}
	app.afterIntent = nil
	finished, err := app.FinalizeAttempt(context.Background(), "stop")
	if err != nil || finished == nil {
		t.Fatal(err)
	}
	state, err = readState(finished.State)
	if err != nil || state.Status != Cancelled || state.PendingOutput != nil || state.FinalOutput == nil ||
		state.FinalOutput.Report == nil || finished.Version != 3 {
		t.Fatal("pending finalization did not resume exactly once", finished, err)
	}
}

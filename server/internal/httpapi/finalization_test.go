package httpapi

import (
	"context"
	"database/sql"
	"encoding/json"
	"testing"
	"time"

	"github.com/caltseng/phalanx/server/internal/application"
	"github.com/caltseng/phalanx/server/internal/contract"
	"github.com/caltseng/phalanx/server/internal/sqlite"
)

func pendingReport(t *testing.T, app *application.App, path string, stage application.Stage, markdown string) (string, string) {
	t.Helper()
	body, err := app.Intake(context.Background(), contract.Intake{
		RepositoryID: "fixture", Goal: "Report finalization", Target: "report", Priority: "normal",
		PublicationMode: "ready", ContextLinks: []string{}, AcceptanceCriteria: []string{},
		Dependencies: []contract.Prerequisite{},
	}, nil)
	if err != nil {
		t.Fatal(err)
	}
	var item struct {
		ID        string           `json:"id"`
		Execution sqlite.Execution `json:"execution"`
	}
	if err := json.Unmarshal(body, &item); err != nil {
		t.Fatal(err)
	}
	revision := contract.Digest(markdown)
	state := application.Start(application.ReportTarget, application.ReadyMode)
	completed := application.Completed
	state.Stage, state.Status, state.ReportDraft, state.PendingOutput =
		stage, application.WaitingForExternal, &revision, &completed
	state.Wait = &application.Wait{Reason: "Store the final Markdown report", Trigger: "retry finalization"}
	text, err := contract.Canonical(state)
	if err != nil {
		t.Fatal(err)
	}
	db, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	tx, err := db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	for _, command := range []struct {
		query string
		args  []any
	}{
		{"UPDATE executions SET version=2, state_json=?, ready_since=NULL WHERE id=?", []any{text, item.Execution.ID}},
		{"INSERT INTO transition_history VALUES (?,2,NULL,'result','{}',?,?)", []any{item.Execution.ID, text, item.Execution.CreatedAt}},
		{"INSERT INTO report_drafts VALUES (?, ?, ?)", []any{item.Execution.ID, revision, markdown}},
		{"INSERT INTO report_finalizations(execution_id,intent_version,due_at) VALUES (?,2,0)", []any{item.Execution.ID}},
	} {
		if _, err := tx.Exec(command.query, command.args...); err != nil {
			t.Fatal(err)
		}
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}
	return item.ID, item.Execution.ID
}

func TestFinalizationSavepointBackoffAndOwnerRecovery(t *testing.T) {
	app, path := fixtureAppDatabase(t)
	itemID, executionID := pendingReport(t, app, path, application.ReportDrafting, "# Reviewed Unicode \u96ea\n")
	db, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	var current *sqlite.Execution
	for failure := int64(1); failure <= 5; failure++ {
		before := time.Now().Unix()
		current, err = app.FinalizeAttempt(context.Background(), executionID)
		after := time.Now().Unix()
		if err != nil {
			t.Fatal(err)
		}
		var storedFailures int64
		var due *int64
		if err := db.QueryRow("SELECT failures, due_at FROM report_finalizations WHERE execution_id=?", executionID).
			Scan(&storedFailures, &due); err != nil {
			t.Fatal(err)
		}
		if storedFailures != failure {
			t.Fatal("failure not durably recorded", storedFailures, failure)
		}
		if failure < 5 {
			offset := int64(1 << failure)
			if due == nil || *due < before+offset || *due > after+offset {
				t.Fatal("backoff did not use the exact 2/4/8/16 contract", due, offset)
			}
			if _, err := db.Exec("UPDATE report_finalizations SET due_at=0 WHERE execution_id=?", executionID); err != nil {
				t.Fatal(err)
			}
		} else if due != nil {
			t.Fatal("fifth failure did not stop automated retries", *due)
		}
		var reports int
		if err := db.QueryRow("SELECT count(*) FROM reports WHERE execution_id=?", executionID).Scan(&reports); err != nil || reports != 0 {
			t.Fatal("failed finalization leaked partial report insert through savepoint", reports, err)
		}
	}
	var state application.State
	if err := json.Unmarshal(current.State, &state); err != nil {
		t.Fatal(err)
	}
	state.Stage = application.ReportStorage
	text, err := contract.Canonical(state)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec("UPDATE executions SET state_json=? WHERE id=?", text, executionID); err != nil {
		t.Fatal(err)
	}
	result, err := app.RetryFinalization(context.Background(), itemID, executionID,
		contract.Versioned{ExpectedVersion: json.Number("7")}, nil)
	if err != nil {
		t.Fatal(err)
	}
	var restored sqlite.Execution
	if err := json.Unmarshal(result, &restored); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(restored.State, &state); err != nil || state.Status != application.Completed ||
		state.FinalOutput == nil || state.FinalOutput.Report == nil || restored.Version != 9 {
		t.Fatal("repaired report did not resume and close through owner retry", string(result), err)
	}
	var markdown string
	if err := db.QueryRow("SELECT markdown FROM reports WHERE execution_id=?", executionID).Scan(&markdown); err != nil ||
		markdown != "# Reviewed Unicode \u96ea\n" {
		t.Fatal("persisted report lost exact UTF-8 Markdown", markdown, err)
	}
}

func TestReportFinalizationResumesFromExistingIntent(t *testing.T) {
	app, path := fixtureAppDatabase(t)
	_, executionID := pendingReport(t, app, path, application.ReportStorage, "# Durable report\n")
	// Opening a fresh application/store simulates restart, without replacing data.
	store, err := sqlite.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	restarted := application.New(store)
	if err := restarted.ProcessFinalizations(context.Background()); err != nil {
		t.Fatal(err)
	}
	result, err := restarted.FinalizeAttempt(context.Background(), executionID)
	if err != nil || result == nil {
		t.Fatal(err)
	}
	var state application.State
	if err := json.Unmarshal(result.State, &state); err != nil ||
		state.Status != application.Completed || result.Version != 3 {
		t.Fatal("restart lost or repeated durable finalization", result, err)
	}
}

func TestReportFinalizationRefreshesDependenciesWithoutAdvancingTheirVersion(t *testing.T) {
	app, path := fixtureAppDatabase(t)
	upstream, executionID := pendingReport(t, app, path, application.ReportStorage, "# Release prerequisite\n")
	raw, err := app.Intake(context.Background(), contract.Intake{
		RepositoryID: "fixture", Goal: "Dependent report consumer", Target: "checked_in", Priority: "normal",
		PublicationMode: "ready", ContextLinks: []string{}, AcceptanceCriteria: []string{},
		Dependencies: []contract.Prerequisite{{UpstreamID: upstream, Milestone: "report"}},
	}, nil)
	if err != nil {
		t.Fatal(err)
	}
	var item struct {
		Execution sqlite.Execution `json:"execution"`
	}
	if err := json.Unmarshal(raw, &item); err != nil {
		t.Fatal(err)
	}
	db, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	retryAt := time.Now().Add(time.Hour).UnixMilli()
	if _, err := db.Exec("UPDATE executions SET retry_at=? WHERE id=?", retryAt, item.Execution.ID); err != nil {
		t.Fatal(err)
	}
	if _, err := app.FinalizeAttempt(context.Background(), executionID); err != nil {
		t.Fatal(err)
	}
	var wait *string
	var readySince, version int64
	if err := db.QueryRow("SELECT dependency_wait_json,ready_since,version FROM executions WHERE id=?", item.Execution.ID).
		Scan(&wait, &readySince, &version); err != nil {
		t.Fatal(err)
	}
	if wait != nil || readySince != retryAt || version != 1 {
		t.Fatal("final report did not atomically release the dependency while preserving scheduling/version", wait, readySince, version)
	}
}

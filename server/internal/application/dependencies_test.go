package application

import (
	"context"
	"database/sql"
	"testing"

	"github.com/caltseng/phalanx/server/internal/contract"
	"github.com/caltseng/phalanx/server/internal/sqlite"
	"github.com/caltseng/phalanx/server/internal/testdb"
)

func dependencyStore(t *testing.T) (*sqlite.Store, *sql.DB) {
	t.Helper()
	path, db := testdb.New(t)
	if _, err := db.Exec(`INSERT INTO repositories VALUES ('fixture','https://github.com/example/fixture.git','stamp');
		INSERT INTO work_items VALUES ('up','fixture','{}','stamp');
		INSERT INTO work_items VALUES ('down','fixture','{}','stamp');
		INSERT INTO work_dependencies VALUES ('down','up','ready_for_check_in')`); err != nil {
		t.Fatal(err)
	}
	store, err := sqlite.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := store.Close(); err != nil {
			t.Error(err)
		}
	})
	return store, db
}

func putDependencyState(t *testing.T, db *sql.DB, id string, previous *string, state State) {
	t.Helper()
	text, err := contract.Canonical(state)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`INSERT INTO executions
		(id,item_id,previous_id,version,state_json,created_at,updated_at,queue_repository,queue_priority,ready_since)
		VALUES (?,'up',?,1,?,'stamp','stamp','fixture',1,NULL)`, id, previous, text); err != nil {
		t.Fatal(err)
	}
}

func TestDependencyPinsVerifiedRevisionAndLatestExecution(t *testing.T) {
	store, db := dependencyStore(t)
	state := Start(MergedTarget, ReadyMode)
	state.Stage, state.Status = Delivery, WaitingForExternal
	state.Candidate, state.PrimaryPR = pointer("head"), pointer("https://github.com/example/fixture/pull/1")
	state.ReadyRevision = &Revision{"head", "main"}
	putDependencyState(t, db, "old", nil, state)
	var pins []resolved
	var waiting []unmet
	read := func() {
		t.Helper()
		if err := store.Read(context.Background(), func(tx *sqlite.Tx) error {
			var err error
			pins, waiting, err = resolveDependencies(tx, "down")
			return err
		}); err != nil {
			t.Fatal(err)
		}
	}
	read()
	if len(pins) != 1 || len(waiting) != 0 || pins[0].ExecutionID != "old" || pins[0].Revision != "head" ||
		pins[0].Reference != *state.PrimaryPR || pins[0].ReviewedRevision == nil || *pins[0].ReviewedRevision != *state.ReadyRevision {
		t.Fatal("ready dependency did not pin exact verified execution/head/base", pins, waiting)
	}
	state.Candidate = pointer("changed")
	text, err := contract.Canonical(state)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec("UPDATE executions SET state_json=? WHERE id='old'", text); err != nil {
		t.Fatal(err)
	}
	read()
	if len(pins) != 0 || len(waiting) != 1 {
		t.Fatal("stale ready revision released a dependent after the candidate changed", pins, waiting)
	}
	state.Candidate = pointer("head")
	state.Status = Completed
	state.FinalOutput = &FinalOutput{PR: &PullRequest{URL: *state.PrimaryPR, Revision: *state.ReadyRevision,
		MergeCommit: pointer("merge"), Closed: true}}
	text, err = contract.Canonical(state)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec("UPDATE executions SET state_json=? WHERE id='old'; UPDATE work_dependencies SET milestone='checked_in'", text); err != nil {
		t.Fatal(err)
	}
	read()
	if len(pins) != 1 || pins[0].Revision != "merge" || pins[0].ReviewedRevision == nil || pins[0].ReviewedRevision.Head != "head" {
		t.Fatal("check-in dependency pinned head rather than delivered merge revision", pins)
	}
	putDependencyState(t, db, "new", pointer("old"), Start(MergedTarget, ReadyMode))
	read()
	if len(pins) != 0 || len(waiting) != 1 || waiting[0].ExecutionID == nil || *waiting[0].ExecutionID != "new" {
		t.Fatal("previous execution evidence satisfied a dependency after rerun", pins, waiting)
	}
}

func TestReportDependencyRequiresStoredMarkdown(t *testing.T) {
	store, db := dependencyStore(t)
	state := Start(ReportTarget, ReadyMode)
	state.Stage, state.Status = ReportStorage, Completed
	state.ReportDraft = pointer("draft")
	state.FinalOutput = &FinalOutput{Report: &StoredReport{Reference: "/report", DraftRevision: state.ReportDraft}}
	putDependencyState(t, db, "report", nil, state)
	if _, err := db.Exec("UPDATE work_dependencies SET milestone='report'"); err != nil {
		t.Fatal(err)
	}
	var pins []resolved
	var waiting []unmet
	read := func() {
		t.Helper()
		if err := store.Read(context.Background(), func(tx *sqlite.Tx) error {
			var err error
			pins, waiting, err = resolveDependencies(tx, "down")
			return err
		}); err != nil {
			t.Fatal(err)
		}
	}
	read()
	if len(pins) != 0 || len(waiting) != 1 {
		t.Fatal("report dependency was admitted without durable Markdown", pins, waiting)
	}
	if _, err := db.Exec("INSERT INTO reports VALUES ('report','# Stored report')"); err != nil {
		t.Fatal(err)
	}
	read()
	if len(pins) != 1 || len(waiting) != 0 || pins[0].ExecutionID != "report" ||
		pins[0].Revision != "draft" || pins[0].ReviewedRevision != nil {
		t.Fatal("stored report dependency pin did not retain its exact draft revision", pins, waiting)
	}
}

package application

import (
	"context"
	"testing"

	"github.com/caltseng/phalanx/server/internal/sqlite"
)

func TestDependencySnapshotOrdersLinksAndPreservesMissingExecutions(t *testing.T) {
	store, db := dependencyStore(t)
	state := Start(ReportTarget, ReadyMode)
	state.Stage, state.Status = ReportStorage, Completed
	state.FinalOutput = &FinalOutput{Report: &StoredReport{Reference: "/report", DraftRevision: pointer("draft")}}
	putDependencyState(t, db, "old", nil, state)
	putDependencyState(t, db, "new", pointer("old"), Start(ReportTarget, ReadyMode))
	if _, err := db.Exec(`INSERT INTO reports VALUES ('old','# Previous report');
		INSERT INTO work_items VALUES ('z','fixture','{}','stamp'), ('a','fixture','{}','stamp');
		INSERT INTO work_dependencies VALUES ('down','z','report'), ('down','a','checked_in');
		UPDATE work_dependencies SET milestone='report' WHERE upstream_id='up'`); err != nil {
		t.Fatal(err)
	}
	if err := store.Read(context.Background(), func(tx *sqlite.Tx) error {
		rows, err := tx.Dependencies("down")
		if err != nil {
			return err
		}
		if len(rows) != 3 || rows[0].UpstreamID != "a" || rows[1].UpstreamID != "up" || rows[2].UpstreamID != "z" {
			t.Fatalf("snapshot did not preserve ordered links: %+v", rows)
		}
		if rows[0].ExecutionID != nil || rows[0].State != nil || rows[0].ReportExists ||
			rows[2].ExecutionID != nil || rows[2].State != nil || rows[2].ReportExists {
			t.Fatalf("missing executions were lost or given evidence: %+v", rows)
		}
		if rows[1].ExecutionID == nil || *rows[1].ExecutionID != "new" || rows[1].State == nil || rows[1].ReportExists {
			t.Fatalf("snapshot used previous execution/report: %+v", rows[1])
		}
		pins, waiting, err := resolveDependencies(tx, "down")
		if err != nil {
			return err
		}
		if len(pins) != 0 || len(waiting) != 3 || waiting[0].UpstreamID != "a" ||
			waiting[1].UpstreamID != "up" || waiting[2].UpstreamID != "z" ||
			waiting[0].ExecutionID != nil || waiting[2].ExecutionID != nil {
			t.Fatalf("resolution changed snapshot order or missing-execution waits: %+v %+v", pins, waiting)
		}
		empty, err := tx.Dependencies("a")
		if err == nil && (empty == nil || len(empty) != 0) {
			t.Fatalf("item without dependencies did not return an empty list: %+v", empty)
		}
		return err
	}); err != nil {
		t.Fatal(err)
	}
}

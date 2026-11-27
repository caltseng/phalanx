package httpapi

import (
	"context"
	"encoding/json"
	"fmt"
	"sort"
	"strings"
	"testing"

	"github.com/caltseng/phalanx/server/internal/application"
	"github.com/caltseng/phalanx/server/internal/contract"
	"github.com/caltseng/phalanx/server/internal/sqlite"
	"github.com/caltseng/phalanx/server/internal/testdb"
)

func TestBoundedDependencyRefreshAndRestartAuditCursor(t *testing.T) {
	app, path := fixtureAppDatabase(t)
	body := contract.Intake{
		RepositoryID: "fixture", Goal: "Prerequisite", Target: "report", Priority: "normal",
		PublicationMode: "ready", ContextLinks: []string{}, AcceptanceCriteria: []string{},
		Dependencies: []contract.Prerequisite{},
	}
	readItem := func(raw json.RawMessage, err error) (string, string) {
		t.Helper()
		if err != nil {
			t.Fatal(err)
		}
		var item struct {
			ID        string           `json:"id"`
			Execution sqlite.Execution `json:"execution"`
		}
		if err := json.Unmarshal(raw, &item); err != nil {
			t.Fatal(err)
		}
		return item.ID, item.Execution.ID
	}
	upstream, executionID := readItem(app.Intake(context.Background(), body, nil))
	body.Dependencies = []contract.Prerequisite{{UpstreamID: upstream, Milestone: "report"}}
	items := []string{}
	for index := 0; index < 37; index++ {
		body.Goal = fmt.Sprintf("Dependent %d", index)
		itemID, _ := readItem(app.Intake(context.Background(), body, nil))
		items = append(items, itemID)
	}
	sort.Strings(items)
	if _, err := app.RequestStop(context.Background(), upstream, executionID,
		contract.Cancellation{ExpectedVersion: "1", Reason: "Stop prerequisite"}, application.Cancel, nil); err != nil {
		t.Fatal(err)
	}
	db := testdb.Open(t, path)
	var cursor string
	if err := db.QueryRow("SELECT cursor FROM dependency_refresh_jobs WHERE upstream_id=?", upstream).Scan(&cursor); err != nil ||
		cursor != items[31] {
		t.Fatal("transition did not fence a bounded 32-item refresh continuation", cursor, err)
	}
	var refreshed int
	if err := db.QueryRow("SELECT count(*) FROM executions WHERE dependency_wait_json LIKE '%Prerequisite status is cancelled%'").
		Scan(&refreshed); err != nil || refreshed != 32 {
		t.Fatal("transition did not limit immediate dependency fanout to 32", refreshed, err)
	}
	store, err := sqlite.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	restarted := application.New(store)
	if worked, err := restarted.ProcessRefresh(context.Background()); err != nil || !worked {
		t.Fatal("persisted dependency continuation did not resume", worked, err)
	}
	var jobs int
	if err := db.QueryRow("SELECT count(*) FROM dependency_refresh_jobs").Scan(&jobs); err != nil || jobs != 0 {
		t.Fatal("finished bounded continuation did not clear its job", jobs, err)
	}
	if err := db.QueryRow("SELECT count(*) FROM executions WHERE dependency_wait_json LIKE '%Prerequisite status is cancelled%'").
		Scan(&refreshed); err != nil || refreshed != 37 {
		t.Fatal("restart lost the remaining dependency fanout", refreshed, err)
	}
	if _, err := db.Exec("INSERT INTO dependency_audit(id,cursor) VALUES (1,?)", items[0]); err != nil {
		t.Fatal(err)
	}
	if err := restarted.StartReconciliation(context.Background()); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRow("SELECT cursor FROM dependency_audit WHERE id=1").Scan(&cursor); err != nil ||
		cursor != items[32] {
		t.Fatal("restart reset an unfinished dependency audit cursor", cursor, err)
	}
	if worked, err := restarted.ProcessRefresh(context.Background()); err != nil || !worked {
		t.Fatal("audit did not resume its remaining batch", worked, err)
	}
	var finalCursor *string
	if err := db.QueryRow("SELECT cursor FROM dependency_audit WHERE id=1").Scan(&finalCursor); err != nil || finalCursor != nil {
		t.Fatal("finished restart audit did not clear its cursor", finalCursor, err)
	}
	var state string
	if err := db.QueryRow("SELECT state_json FROM executions WHERE id=?", executionID).Scan(&state); err != nil ||
		!strings.Contains(state, `"status":"cancelled"`) {
		t.Fatal("dependency job changed the prerequisite lifecycle", state, err)
	}
}

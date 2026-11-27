package httpapi

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	"github.com/caltseng/phalanx/server/internal/contract"
	"github.com/caltseng/phalanx/server/internal/sqlite"
)

func TestTitleReceiptRacePreservesExecution(t *testing.T) {
	app := fixtureApp(t)
	body, err := app.Intake(context.Background(), contract.Intake{
		RepositoryID: "fixture", Goal: "Title race", Target: "checked_in", Priority: "normal", PublicationMode: "ready",
		ContextLinks: []string{}, AcceptanceCriteria: []string{}, Dependencies: []contract.Prerequisite{},
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
	handler := &Handler{App: app, Owner: "owner", Runner: "runner"}
	update := func(key, title string) *httptest.ResponseRecorder {
		r := httptest.NewRequest(http.MethodPut, "/v1/items/"+item.ID+"/title",
			strings.NewReader(`{"expected_revision":0,"title":"`+title+`"}`))
		r.Header.Set("Authorization", "Bearer owner")
		r.Header.Set("Content-Type", "application/json")
		r.Header.Set("Idempotency-Key", key)
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, r)
		return w
	}
	var responses [2]*httptest.ResponseRecorder
	var workers sync.WaitGroup
	for i := range responses {
		workers.Add(1)
		go func(i int) {
			defer workers.Done()
			responses[i] = update(string(rune('a'+i)), string(rune('A'+i)))
		}(i)
	}
	workers.Wait()
	winner := -1
	for i, response := range responses {
		if response.Code == 200 {
			if winner != -1 {
				t.Fatal("both revision-fenced writes committed")
			}
			winner = i
		} else if response.Code != 409 {
			t.Fatal(response.Code, response.Body.String())
		}
	}
	if winner == -1 {
		t.Fatal("no admitted title write")
	}
	replay := update(string(rune('a'+winner)), string(rune('A'+winner)))
	if replay.Code != 200 || replay.Body.String() != responses[winner].Body.String() {
		t.Fatal("receipt did not replay the exact title result", replay.Body.String())
	}
	changed := update(string(rune('a'+winner)), "different")
	if changed.Code != 409 {
		t.Fatal("changed receipt payload accepted")
	}
	err = app.Store.Read(context.Background(), func(tx *sqlite.Tx) error {
		execution, err := tx.CurrentExecution(item.ID)
		if err != nil {
			return err
		}
		title, err := tx.Title(item.ID)
		if err != nil {
			return err
		}
		if title.Revision != 1 || execution.Version != item.Execution.Version ||
			string(execution.State) != string(item.Execution.State) {
			t.Fatal("display-only title changed execution or appended extra title revisions")
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
}

func TestHistoryAndTimelineScopeAndMissing(t *testing.T) {
	app := fixtureApp(t)
	body, err := app.Intake(context.Background(), contract.Intake{
		RepositoryID: "fixture", Goal: "Read history", Target: "report", Priority: "normal", PublicationMode: "ready",
		ContextLinks: []string{}, AcceptanceCriteria: []string{}, Dependencies: []contract.Prerequisite{},
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
	handler := &Handler{App: app, Owner: "owner", Runner: "runner"}
	for _, test := range []struct {
		path   string
		token  string
		status int
	}{
		{"/v1/items/" + item.ID + "/executions", "owner", 200},
		{"/v1/items/" + item.ID + "/executions/" + item.Execution.ID + "/timeline", "runner", 200},
		{"/v1/items/missing/executions", "owner", 404},
		{"/v1/items/missing/executions/" + item.Execution.ID + "/timeline", "owner", 404},
		{"/v1/items/" + item.ID + "/executions/" + item.Execution.ID + "/timeline", "", 401},
		{"/v1/items/" + item.ID + "/title", "runner", 403},
	} {
		r := httptest.NewRequest(http.MethodGet, test.path, nil)
		if test.token != "" {
			r.Header.Set("Authorization", "Bearer "+test.token)
		}
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, r)
		if w.Code != test.status {
			t.Fatalf("%s: %d %s", test.path, w.Code, w.Body)
		}
		if w.Code == 200 {
			var values []map[string]any
			if err := json.Unmarshal(w.Body.Bytes(), &values); err != nil || len(values) != 1 {
				t.Fatal("history read omitted intake event or execution", err, w.Body.String())
			}
		}
	}
}

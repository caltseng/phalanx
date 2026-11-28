package httpapi

import (
	"encoding/json"
	"log/slog"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"

	"github.com/caltseng/phalanx/server/internal/testdb"
)

func nativeRequest(h *Handler, method, path, token, body, key string) *httptest.ResponseRecorder {
	r := httptest.NewRequest(method, path, strings.NewReader(body))
	if token != "" {
		r.Header.Set("Authorization", "Bearer "+token)
	}
	r.Header.Set("Content-Type", "application/json")
	if key != "" {
		r.Header.Set("Idempotency-Key", key)
	}
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	return w
}

func TestNativeValidationDetailsAndIntakeReplay(t *testing.T) {
	h := &Handler{App: fixtureApp(t), Owner: "owner", Runner: "runner"}
	for _, tt := range []struct {
		method, path, body string
		loc                []string
	}{
		{"POST", "/v1/items", `{}`, []string{"body", "goal"}},
		{"POST", "/v1/items", `{"repository_id":"fixture","goal":""}`, []string{"body", "goal"}},
		{"GET", "/v1/owner/inbox?limit=invalid", "", []string{"query", "limit"}},
		{"GET", "/v1/operator/executions?limit=0", "", []string{"query", "limit"}},
	} {
		t.Run(tt.path+tt.body, func(t *testing.T) {
			w := nativeRequest(h, tt.method, tt.path, "owner", tt.body, "")
			var body struct {
				Detail []struct {
					Loc []string `json:"loc"`
					Msg string   `json:"msg"`
				} `json:"detail"`
			}
			if w.Code != 422 || w.Header().Get("Content-Type") != "application/json" {
				t.Fatalf("validation response: %d %s", w.Code, w.Body.String())
			}
			if err := json.Unmarshal(w.Body.Bytes(), &body); err != nil {
				t.Fatal(err)
			}
			for _, detail := range body.Detail {
				if strings.Join(detail.Loc, ".") == strings.Join(tt.loc, ".") && detail.Msg != "" {
					return
				}
			}
			t.Fatalf("missing field-highlightable validation detail: %s", w.Body.String())
		})
	}
	payload := `{"repository_id":"fixture","goal":"Native receipt"}`
	first := nativeRequest(h, "POST", "/v1/items", "owner", payload, "native-intake")
	replay := nativeRequest(h, "POST", "/v1/items", "owner", payload, "native-intake")
	if first.Code != 201 || replay.Code != 201 || first.Body.String() != replay.Body.String() {
		t.Fatalf("intake did not replay exact response: %d %s / %d %s",
			first.Code, first.Body.String(), replay.Code, replay.Body.String())
	}
	conflict := nativeRequest(h, "POST", "/v1/items", "owner",
		`{"repository_id":"fixture","goal":"Changed"}`, "native-intake")
	if conflict.Code != 409 {
		t.Fatal("changed idempotent request accepted", conflict.Code, conflict.Body.String())
	}
	var item struct {
		ID string `json:"id"`
	}
	if err := json.Unmarshal(first.Body.Bytes(), &item); err != nil {
		t.Fatal(err)
	}
	history := nativeRequest(h, "GET", "/v1/items/"+item.ID+"/executions", "owner", "", "")
	var executions []json.RawMessage
	if err := json.Unmarshal(history.Body.Bytes(), &executions); err != nil || history.Code != 200 || len(executions) != 1 {
		t.Fatal("refusal or replay created extra execution history", history.Body.String(), err)
	}
}

func TestNativeInboxKeysetAndOperatorCursorScope(t *testing.T) {
	app, path := fixtureAppDatabase(t)
	h := &Handler{App: app, Owner: "owner", Runner: "runner", Logger: slog.Default()}
	db := testdb.Open(t, path)
	if _, err := db.Exec(`INSERT INTO dispatch_settings VALUES (1,0,2,1);
		INSERT INTO operator_change VALUES ('AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA');
		INSERT INTO issue_scout_settings(id) VALUES (1)`); err != nil {
		t.Fatal(err)
	}
	for _, goal := range []string{"First question", "Second question"} {
		w := nativeRequest(h, "POST", "/v1/items", "owner",
			`{"repository_id":"fixture","goal":"`+goal+`"}`, "")
		if w.Code != 201 {
			t.Fatal(w.Code, w.Body.String())
		}
	}
	for _, token := range []string{"", "runner"} {
		w := nativeRequest(h, "GET", "/v1/owner/inbox", token, "", "")
		want := 403
		if token == "" {
			want = 401
		}
		if w.Code != want {
			t.Fatal("inbox authority changed", w.Code, w.Body.String())
		}
	}
	const stamp = "2026-01-02T03:04:05+00:00"
	if _, err := db.Exec(`UPDATE executions SET version=2,updated_at=?,
		state_json=json_set(state_json,'$.status','waiting_for_input',
		'$.wait',json('{"reason":"Choose an option","trigger":"reply"}'));
		INSERT INTO transition_history(execution_id,version,attempt_id,event,details_json,state_json,created_at)
		SELECT id,2,NULL,'question','{"question":{"prompt":"Choose","options":["A","B"]}}',state_json,updated_at
		FROM executions`, stamp); err != nil {
		t.Fatal(err)
	}
	type inboxEntry struct {
		ExecutionID string `json:"execution_id"`
		Version     int    `json:"version"`
		Question    struct {
			Prompt  string   `json:"prompt"`
			Options []string `json:"options"`
		} `json:"question"`
		Wait struct {
			Reason string `json:"reason"`
		} `json:"wait"`
	}
	var first, second []inboxEntry
	readInbox := func(path string, target *[]inboxEntry) {
		t.Helper()
		w := nativeRequest(h, "GET", path, "owner", "", "")
		if err := json.Unmarshal(w.Body.Bytes(), target); err != nil || w.Code != 200 || len(*target) != 1 {
			t.Fatal("inbox page", w.Code, w.Body.String(), err)
		}
		entry := (*target)[0]
		if entry.Version != 2 || entry.Question.Prompt != "Choose" ||
			strings.Join(entry.Question.Options, ",") != "A,B" || entry.Wait.Reason != "Choose an option" {
			t.Fatal("inbox lost durable question/wait", w.Body.String())
		}
	}
	readInbox("/v1/owner/inbox?limit=1", &first)
	after := url.Values{"limit": {"1"}, "after_time": {stamp}, "after_id": {first[0].ExecutionID}}
	readInbox("/v1/owner/inbox?"+after.Encode(), &second)
	if first[0].ExecutionID >= second[0].ExecutionID {
		t.Fatal("inbox keyset repeated or skipped equal-timestamp ordering", first, second)
	}
	page := nativeRequest(h, "GET", "/v1/operator/executions?limit=1", "owner", "", "")
	var listing struct {
		Items []struct {
			ID string `json:"id"`
		} `json:"items"`
		Next *string `json:"next_cursor"`
	}
	if err := json.Unmarshal(page.Body.Bytes(), &listing); err != nil || page.Code != 200 ||
		len(listing.Items) != 1 || listing.Next == nil {
		t.Fatal("operator first page", page.Code, page.Body.String(), err)
	}
	firstID, cursor := listing.Items[0].ID, url.QueryEscape(*listing.Next)
	page = nativeRequest(h, "GET", "/v1/operator/executions?limit=1&cursor="+cursor, "owner", "", "")
	if err := json.Unmarshal(page.Body.Bytes(), &listing); err != nil || page.Code != 200 ||
		len(listing.Items) != 1 || listing.Items[0].ID == firstID || listing.Next != nil {
		t.Fatal("operator continuation", page.Code, page.Body.String(), err)
	}
	for _, query := range []string{"cursor=invalid", "q=different&cursor=" + cursor, "kind=closed&cursor=" + cursor} {
		w := nativeRequest(h, "GET", "/v1/operator/executions?limit=1&"+query, "owner", "", "")
		if w.Code != 400 {
			t.Fatal("invalid/cross-filter cursor accepted", query, w.Code, w.Body.String())
		}
	}
}

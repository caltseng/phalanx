package httpapi

import (
	"context"
	"database/sql"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/caltseng/phalanx/server/internal/contract"
)

func TestOperatorConditionalPoll(t *testing.T) {
	for _, route := range []string{"list", "filtered", "cursor", "overview", "group", "item"} {
		t.Run(route, func(t *testing.T) {
			for _, phase := range []string{"initial", "conditional", "invalidate"} {
				t.Run(phase, func(t *testing.T) {
					app, path := fixtureAppDatabase(t)
					now := time.Now().UTC()
					app.Clock = func() time.Time { return now }
					var itemID string
					label := "poll"
					for range 2 {
						body, err := app.Intake(context.Background(), contract.Intake{
							RepositoryID: "fixture", Goal: "Poll needle", Target: "checked_in", Priority: "normal",
							PublicationMode: "ready", Label: &label, ContextLinks: []string{},
							AcceptanceCriteria: []string{}, Dependencies: []contract.Prerequisite{},
						}, nil)
						if err != nil {
							t.Fatal(err)
						}
						var item struct{ ID string }
						if err := json.Unmarshal(responseJSON(t, body), &item); err != nil {
							t.Fatal(err)
						}
						itemID = item.ID
					}
					db, err := sql.Open("sqlite", path)
					if err != nil {
						t.Fatal(err)
					}
					defer db.Close()
					stamp := now.Format(time.RFC3339Nano)
					if _, err := db.Exec(`INSERT INTO dispatch_settings VALUES (1,0,10,5);
						INSERT INTO operator_change VALUES ('AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA');
						INSERT INTO issue_scout_settings(id) VALUES (1);
						INSERT INTO captains(id,session_ref,charter_revision,state,created_at,heartbeat_at,pickup_json)
						VALUES ('poll-captain','session','charter','active',?,?,
						'{"repositories":{"fixture":null},"opaque":{"nested":[1,true,null]}}')`, stamp, stamp); err != nil {
						t.Fatal(err)
					}
					handler := &Handler{App: app, Owner: "owner", Runner: "runner"}
					get := func(path, token, tag string) *httptest.ResponseRecorder {
						r := httptest.NewRequest(http.MethodGet, path, nil)
						if token != "" {
							r.Header.Set("Authorization", "Bearer "+token)
						}
						if tag != "" {
							r.Header.Set("If-None-Match", tag)
						}
						w := httptest.NewRecorder()
						handler.ServeHTTP(w, r)
						return w
					}
					target := "/v1/operator/executions"
					switch route {
					case "filtered", "cursor":
						target += "?q=needle&limit=1"
						if route == "cursor" {
							first := get(target, "owner", "")
							var page struct {
								Next *string `json:"next_cursor"`
							}
							if err := json.Unmarshal(first.Body.Bytes(), &page); err != nil || first.Code != 200 || page.Next == nil {
								t.Fatal("missing continuation", first.Code, first.Body.String(), err)
							}
							target += "&cursor=" + *page.Next
						}
					case "overview":
						target = "/v1/operator/overview"
					case "group":
						target = "/v1/operator/groups/poll"
					case "item":
						target = "/v1/operator/items/" + itemID
					}
					first := get(target, "owner", "")
					tag := first.Header().Get("ETag")
					if first.Code != 200 || tag == "" || first.Body.Len() == 0 {
						t.Fatal("initial poll", first.Code, first.Body.String())
					}
					if phase == "initial" {
						return
					}
					poll := get(target, "owner", tag)
					if poll.Code != http.StatusNotModified || poll.Body.Len() != 0 ||
						poll.Header().Get("ETag") != tag || poll.Header().Get("Cache-Control") != "private, no-cache" {
						t.Fatal("conditional poll", poll.Code, poll.Body.String(), poll.Header())
					}
					if phase == "conditional" {
						return
					}
					if response := get(target, "", tag); response.Code != 401 {
						t.Fatal("conditional poll bypassed authentication", response.Code)
					}
					if _, err := db.Exec("UPDATE dispatch_settings SET paused=NOT paused"); err != nil {
						t.Fatal(err)
					}
					if response := get(target, "owner", tag); response.Code != 200 || response.Header().Get("ETag") == tag {
						t.Fatal("state mutation failed to invalidate", response.Code, response.Body.String())
					}
					if response := get("/v1/operator/executions?cursor=invalid", "owner", "*"); response.Code != 400 {
						t.Fatal("conditional poll bypassed cursor validation", response.Code)
					}
				})
			}
		})
	}
}

func TestLocalBoardAuthorityRequiresDirectPeerAndNoProxyOrAuthorization(t *testing.T) {
	handler := Handler{LocalBoard: true}
	for _, fixture := range []struct {
		name, peer, host string
		header, value    string
		allowed          bool
	}{
		{"direct", "127.0.0.1:1234", "localhost:8000", "", "", true},
		{"ipv6", "[::1]:1234", "[::1]", "", "", true},
		{"remote", "192.0.2.1:1234", "localhost:8000", "", "", false},
		{"rebound", "127.0.0.1:1234", "invalid.example", "", "", false},
		{"forwarded", "127.0.0.1:1234", "localhost", "Forwarded", "for=127.0.0.1", false},
		{"forwarded-prefix", "127.0.0.1:1234", "localhost", "X-Forwarded-Anything", "local", false},
		{"real-ip", "127.0.0.1:1234", "localhost", "X-Real-IP", "127.0.0.1", false},
		{"empty-auth", "127.0.0.1:1234", "localhost", "Authorization", "", false},
	} {
		t.Run(fixture.name, func(t *testing.T) {
			request := httptest.NewRequest("GET", "http://localhost/v1/operator/overview", nil)
			request.RemoteAddr, request.Host = fixture.peer, fixture.host
			if fixture.header != "" {
				request.Header.Set(fixture.header, fixture.value)
			}
			if actual := handler.localBoardAuthority(request); actual != fixture.allowed {
				t.Fatal("direct local authority changed", actual, fixture.allowed)
			}
		})
	}
	request := httptest.NewRequest("GET", "http://localhost/v1/operator/overview", nil)
	request.RemoteAddr = "127.0.0.1:1234"
	request.Header.Add("Host", "localhost")
	request.Header.Add("Host", "localhost")
	if handler.localBoardAuthority(request) {
		t.Fatal("duplicate Host metadata granted direct authority")
	}
}

func TestLocalBoardMutationRequiresOneMatchingOrigin(t *testing.T) {
	read := httptest.NewRequest("GET", "http://localhost/operator/api/repositories", nil)
	if !localBoardOrigin(read) {
		t.Fatal("read unexpectedly required Origin")
	}

	write := httptest.NewRequest("POST", "http://localhost/operator/api/items", nil)
	if localBoardOrigin(write) {
		t.Fatal("anonymous local mutation accepted missing Origin")
	}
	write.Header.Set("Origin", "http://localhost")
	if !localBoardOrigin(write) {
		t.Fatal("matching direct mutation refused")
	}
	write.Header.Add("Origin", "http://localhost")
	if localBoardOrigin(write) {
		t.Fatal("duplicate Origin accepted")
	}
}

func TestStaticRangesKeepRefusalPrecedenceAndBoundedMultipart(t *testing.T) {
	if _, status, _ := fileRanges("bytes=9-0,999-", 100); status != 416 {
		t.Fatal("unsatisfied range no longer precedes reversed-range error", status)
	}
	ranges, status, _ := fileRanges("bytes=30-39,0-9,8-29", 100)
	if status != 0 || len(ranges) != 1 || ranges[0].start != 0 || ranges[0].end != 40 {
		t.Fatal("overlapping/adjacent ranges were not sorted and merged", ranges, status)
	}
	ranges, status, _ = fileRanges("bytes="+strings.Repeat("0-1,", 100)+"0-1", 100)
	if status != 0 || len(ranges) != 0 {
		t.Fatal("over-limit range list did not fall back to complete file", ranges, status)
	}
	if _, status, message := fileRanges("items=0-1", 100); status != 400 || message != "Only support bytes range" {
		t.Fatal(status, message)
	}
}

func TestRetainedDashboardPeerAndOriginVectors(t *testing.T) {
	handler := Handler{LocalBoard: true}
	for _, fixture := range []struct {
		peer, host, origin, site string
		status                   int
	}{
		{"127.0.0.1:1234", "localhost:8000", "http://localhost:8000", "same-origin", 200},
		{"[::1]:1234", "[::1]:8000", "http://[::1]:8000", "same-origin", 200},
		{"203.0.113.8:1234", "localhost:8000", "http://localhost:8000", "same-origin", 401},
		{"[::ffff:203.0.113.8]:1234", "localhost:8000", "http://localhost:8000", "same-origin", 401},
		{"", "localhost:8000", "http://localhost:8000", "same-origin", 401},
		{"127.0.0.1:1234", "evil.example", "http://evil.example", "same-origin", 401},
		{"127.0.0.1:1234", "localhost:8000", "http://localhost:8001", "same-origin", 403},
		{"127.0.0.1:1234", "localhost:8000", "null", "same-origin", 403},
		{"127.0.0.1:1234", "localhost:8000", "", "same-origin", 403},
		{"127.0.0.1:1234", "localhost:8000", "http://localhost:8000", "cross-site", 403},
	} {
		request := httptest.NewRequest("POST", "http://localhost/operator/api/items", nil)
		request.RemoteAddr, request.Host = fixture.peer, fixture.host
		if fixture.origin != "" {
			request.Header.Set("Origin", fixture.origin)
		}
		request.Header.Set("Sec-Fetch-Site", fixture.site)
		status := 200
		if !handler.localBoardAuthority(request) {
			status = 401
		} else if !localBoardOrigin(request) {
			status = 403
		}
		if status != fixture.status {
			t.Fatalf("retained actual-peer/origin vector changed: %+v: %d", fixture, status)
		}
	}
}

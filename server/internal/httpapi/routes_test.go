package httpapi

import (
	"net/http/httptest"
	"strings"
	"testing"
)

var roleMatrix = []struct {
	endpoints, allowed, refusal string
}{
	{`
GET /health
GET /operator/
GET /operator/queue
GET /operator/history
GET /operator/items/{itemID}
GET /operator/executions/{executionID}
GET /operator/assets/app.js
GET /openapi.json
HEAD /openapi.json
GET /docs
HEAD /docs
GET /docs/oauth2-redirect
HEAD /docs/oauth2-redirect
GET /redoc
HEAD /redoc`, "owner runner writer local", ""},
	{`
GET /v1/owner/status
GET /v1/owner/inbox
PUT /v1/issue-scout/settings
PUT /v1/dispatch/settings
POST /v1/repositories
POST /v1/items
POST /v1/issue-scout/issues/{repositoryID}/{number}/decision
GET /v1/items/{itemID}/title
PUT /v1/items/{itemID}/title
PATCH /v1/items/{itemID}/executions/{executionID}
POST /v1/items/{itemID}/executions/{executionID}/cancel
POST /v1/items/{itemID}/executions/{executionID}/close-failure
POST /v1/items/{itemID}/executions/{executionID}/retry-finalization
POST /v1/items/{itemID}/executions/{executionID}/reply
POST /v1/items/{itemID}/executions/{executionID}/rerun`, "owner", "Owner token required"},
	{`
GET /v1/runner/status
POST /v1/items/{itemID}/executions/{executionID}/github-observation
POST /v1/items/{itemID}/executions/{executionID}/local-checks
POST /v1/reconcilers/{runnerID}/heartbeat`, "runner", "Runner token required"},
	{`
POST /v1/writer/attempt
GET /v1/writer
POST /v1/writer/progress
POST /v1/writer/capacity
POST /v1/writer/usage
POST /v1/items/{itemID}/executions/{executionID}/result
POST /v1/items/{itemID}/executions/{executionID}/stop-confirmed`, "writer", "Writer token required"},
	{`
POST /v1/issue-scout/claim
PUT /v1/flags
POST /v1/flags/clear
GET /v1/muster/status
POST /v1/captains
GET /v1/session-archives
PUT /v1/session-archives
POST /v1/issue-scout/passes/{passID}/page
POST /v1/issue-scout/passes/{passID}/failure
POST /v1/items/{itemID}/captain-actions
POST /v1/items/{itemID}/executions/{executionID}/writer
PATCH /v1/items/{itemID}/executions/{executionID}/writer
POST /v1/items/{itemID}/executions/{executionID}/writer/withdraw
POST /v1/items/{itemID}/executions/{executionID}/writers/{writerID}/usage
DELETE /v1/reconcilers/{runnerID}
PUT /v1/captains/{captainID}
PUT /v1/captains/{captainID}/reinforcement`, "owner runner", "Owner or runner token required"},
	{`
GET /operator/api/repositories
POST /operator/api/items
GET /operator/api/dispatch/settings
PUT /operator/api/dispatch/settings
POST /operator/api/issue-scout/{repositoryID}/{number}/decision`, "owner local", "Owner token required"},
	{`
GET /v1/issue-scout
GET /v1/flags
GET /v1/operator/executions
GET /v1/operator/overview
GET /v1/dispatch/settings
GET /v1/captains
GET /v1/captains/current
GET /v1/operator/items/{itemID}
GET /v1/operator/groups/{label}
GET /v1/operator/executions/{executionID}/timeline
GET /v1/items/{itemID}/executions/{executionID}/report
GET /v1/captains/{captainID}`, "owner runner writer local", ""},
	{`
GET /v1/repositories
GET /v1/items/{itemID}
GET /v1/items/{itemID}/executions
GET /v1/items/{itemID}/executions/{executionID}/timeline
GET /v1/items/{itemID}/executions/{executionID}/drafts/{revision}`, "owner runner writer", ""},
}

func TestRouteRoleMatrix(t *testing.T) {
	covered := map[*routeSpec]bool{}
	for _, tt := range roleMatrix {
		fields := strings.Fields(tt.endpoints)
		for i := 0; i < len(fields); i += 2 {
			method, path := fields[i], samplePath(fields[i+1])
			t.Run(method+" "+path, func(t *testing.T) {
				target, _ := match(path, method)
				if target.routeSpec == nil {
					t.Fatal("route missing")
				}
				covered[target.routeSpec] = true
				for _, role := range strings.Fields("owner runner writer local") {
					// Local identity is only available to explicitly opted-in routes.
					if role == "local" && target.policy != public && !target.policy.localAuthority() {
						continue
					}
					request := httptest.NewRequest(method, "http://localhost"+path, nil)
					request.Header.Set("Origin", "http://localhost")
					q := requestState{route: target, role: role}
					want := tt.refusal
					if strings.Contains(" "+tt.allowed+" ", " "+role+" ") {
						want = ""
					}
					if got := q.refusal(request); got != want {
						t.Errorf("%s: refusal %q, want %q", role, got, want)
					}
				}
			})
		}
	}
	for i := range routes {
		if !covered[&routes[i]] {
			t.Errorf("role matrix missing %s %s", routes[i].method, routes[i].pattern)
		}
	}
}

func samplePath(pattern string) string {
	for _, name := range []string{"itemID", "executionID", "captainID", "runnerID", "writerID", "passID", "repositoryID"} {
		pattern = strings.ReplaceAll(pattern, "{"+name+"}", strings.Repeat("a", 32))
	}
	return strings.NewReplacer("{number}", "1", "{label}", "group", "{revision}", strings.Repeat("b", 64)).Replace(pattern)
}

func TestRouteOriginMatrix(t *testing.T) {
	tests := []struct {
		method, path, role, origin, site, want string
	}{
		{"POST", "/v1/items", "owner", "http://evil", "", "Browser writes require a same-origin request"},
		{"POST", "/v1/items", "runner", "http://evil", "", "Owner token required"},
		{"POST", "/v1/repositories", "owner", "http://evil", "", ""},
		{"PUT", "/v1/dispatch/settings", "owner", "", "", ""},
		{"PUT", "/v1/dispatch/settings", "owner", "", "same-origin", "Browser writes require a same-origin request"},
		{"POST", "/operator/api/items", "local", "", "", "Local dashboard actions require a same-origin request"},
		{"POST", "/operator/api/items", "owner", "", "", ""},
		{"POST", "/operator/api/items", "local", "http://localhost", "", ""},
		{"POST", "/operator/api/items", "owner", "http://evil", "", "Browser writes require a same-origin request"},
		{"POST", "/operator/api/items", "runner", "http://evil", "", "Owner token required"},
		{"GET", "/operator/api/repositories", "local", "http://evil", "", "Local dashboard actions require a same-origin request"},
		{"GET", "/operator/api/repositories", "owner", "http://evil", "", ""},
		{"GET", "/v1/operator/overview", "local", "http://evil", "", ""},
	}
	for _, tt := range tests {
		t.Run(tt.method+" "+tt.path+"/"+tt.role+"/"+tt.origin+"/"+tt.site, func(t *testing.T) {
			target, _ := match(tt.path, tt.method)
			request := httptest.NewRequest(tt.method, "http://localhost"+tt.path, nil)
			if tt.origin != "" {
				request.Header.Set("Origin", tt.origin)
			}
			if tt.site != "" {
				request.Header.Set("Sec-Fetch-Site", tt.site)
			}
			q := requestState{route: target, role: tt.role}
			if got := q.refusal(request); got != tt.want {
				t.Fatalf("refusal %q, want %q", got, tt.want)
			}
		})
	}
}

func TestRouteMethodWireBytes(t *testing.T) {
	tests := []struct{ path, allow string }{
		{"/health", "GET"}, {"/v1/flags", "PUT"}, {"/v1/captains", "POST"},
		{"/v1/repositories", "POST"}, {"/v1/dispatch/settings", "GET"},
		{"/v1/session-archives", "GET"}, {"/v1/captains/id", "PUT"},
		{"/v1/items/id/title", "GET"}, {"/v1/items/id/executions/execution/writer", "POST"},
		{"/operator/api/dispatch/settings", "GET"}, {"/docs", "GET, HEAD"},
		{"/operator/assets/app.js", "GET, HEAD"}, {"/operator/queue", "GET"},
	}
	for _, tt := range tests {
		t.Run(tt.path, func(t *testing.T) {
			request := httptest.NewRequest("OPTIONS", tt.path, nil)
			response := httptest.NewRecorder()
			(&Handler{}).ServeHTTP(response, request)
			if response.Code != 405 || response.Header().Get("Allow") != tt.allow ||
				response.Header().Get("Content-Type") != "application/json" ||
				response.Body.String() != `{"detail":"Method Not Allowed"}` {
				t.Fatalf("method response changed: %d %v %s", response.Code, response.Header(), response.Body.String())
			}
		})
	}
}

func TestRouteAuthorityWireBytes(t *testing.T) {
	tests := []struct {
		method, path, token, origin, detail string
		local                               bool
		status                              int
	}{
		{"GET", "/v1/owner/status", "", "", "Valid bearer token required", false, 401},
		{"GET", "/v1/owner/status", "runner", "", "Owner token required", false, 403},
		{"GET", "/v1/runner/status", "owner", "", "Runner token required", false, 403},
		{"GET", "/v1/writer", "owner", "", "Writer token required", false, 403},
		{"GET", "/v1/muster/status", "", "", "Valid bearer token required", true, 401},
		{"POST", "/v1/items", "runner", "http://evil", "Owner token required", false, 403},
		{"POST", "/v1/items", "owner", "http://evil", "Browser writes require a same-origin request", false, 403},
		{"GET", "/v1/items/invalid/title", "runner", "", "Owner token required", false, 403},
		{"GET", "/operator/api/repositories", "runner", "", "Owner token required", true, 403},
		{"POST", "/operator/api/items", "", "", "Local dashboard actions require a same-origin request", true, 403},
		{"POST", "/operator/api/items", "", "http://evil", "Local dashboard actions require a same-origin request", true, 403},
		{"POST", "/operator/api/items", "owner", "http://evil", "Browser writes require a same-origin request", true, 403},
	}
	for _, tt := range tests {
		t.Run(tt.method+" "+tt.path+"/"+tt.token+"/"+tt.origin, func(t *testing.T) {
			request := httptest.NewRequest(tt.method, "http://localhost"+tt.path, strings.NewReader("{}"))
			if tt.token != "" {
				request.Header.Set("Authorization", "Bearer "+tt.token)
			}
			if tt.origin != "" {
				request.Header.Set("Origin", tt.origin)
			}
			if tt.local {
				request.RemoteAddr = "127.0.0.1:1234"
			}
			response := httptest.NewRecorder()
			(&Handler{Owner: "owner", Runner: "runner", LocalBoard: true}).ServeHTTP(response, request)
			if response.Code != tt.status || response.Body.String() != `{"detail":"`+tt.detail+`"}` ||
				response.Header().Get("Content-Type") != "application/json" {
				t.Fatalf("authority response changed: %d %v %s", response.Code, response.Header(), response.Body.String())
			}
			wantChallenge := ""
			if tt.status == 401 {
				wantChallenge = "Bearer"
			}
			if response.Header().Get("WWW-Authenticate") != wantChallenge {
				t.Fatal("authentication challenge changed", response.Header())
			}
		})
	}
}

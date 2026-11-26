// Package httpapi owns routing, wire validation, role/scope checks, and errors.
package httpapi

import (
	"bytes"
	"context"
	"crypto/subtle"
	"database/sql"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"strings"

	"github.com/caltseng/phalanx/server/internal/application"
	"github.com/caltseng/phalanx/server/internal/contract"
	"github.com/caltseng/phalanx/server/internal/sqlite"
)

type Handler struct {
	App      *application.App
	Owner    string
	Runner   string
	Logger   *slog.Logger
	dispatch func() // test-only barrier at mutation dispatch, never wire/config input
}

type route struct {
	kind        string
	itemID      string
	executionID string
	captainID   string
	runnerID    string
	writerID    string
	revision    string
	method      string
}

func match(path, method string) (route, string) {
	switch path {
	case "/health":
		return route{kind: "health", method: "GET"}, "GET"
	case "/v1/owner/status":
		return route{kind: "owner", method: "GET"}, "GET"
	case "/v1/runner/status":
		return route{kind: "runner", method: "GET"}, "GET"
	case "/v1/owner/inbox":
		return route{kind: "inbox", method: "GET"}, "GET"
	case "/v1/writer/attempt":
		return route{kind: "writer-attempt", method: "POST"}, "POST"
	case "/v1/writer/progress":
		return route{kind: "writer-progress", method: "POST"}, "POST"
	case "/v1/writer/capacity":
		return route{kind: "writer-capacity", method: "POST"}, "POST"
	case "/v1/writer/usage":
		return route{kind: "writer-usage", method: "POST"}, "POST"
	case "/v1/dispatch/settings":
		if method == "PUT" {
			return route{kind: "set-dispatch", method: "PUT"}, "GET"
		}
		return route{kind: "dispatch", method: "GET"}, "GET"
	case "/v1/captains":
		if method == "POST" {
			return route{kind: "captain-register", method: "POST"}, "POST"
		}
		return route{kind: "captains", method: "GET"}, "POST"
	case "/v1/captains/current":
		return route{kind: "captain-current", method: "GET"}, "GET"
	case "/v1/session-archives":
		if method == "PUT" {
			return route{kind: "archive-write", method: "PUT"}, "GET"
		}
		return route{kind: "archive-read", method: "GET"}, "GET"
	case "/v1/repositories":
		if method == "POST" {
			return route{kind: "register", method: "POST"}, "POST"
		}
		return route{kind: "repositories", method: "GET"}, "POST"
	case "/v1/items":
		return route{kind: "intake", method: "POST"}, "POST"
	}
	if strings.HasPrefix(path, "/v1/items/") {
		parts := strings.Split(strings.TrimPrefix(path, "/v1/items/"), "/")
		if parts[0] == "" {
			return route{}, ""
		}
		target := route{itemID: parts[0], method: "GET"}
		switch {
		case len(parts) == 1:
			target.kind = "inspect"
		case len(parts) == 2 && parts[1] == "title":
			target.kind = "title"
			if method == "PUT" {
				target.kind, target.method = "set-title", "PUT"
			}
			return target, "GET"
		case len(parts) == 2 && parts[1] == "executions":
			target.kind = "history"
		case len(parts) == 2 && parts[1] == "captain-actions":
			target.kind, target.method = "captain-action", "POST"
			return target, "POST"
		case len(parts) == 3 && parts[1] == "executions" && parts[2] != "":
			target.kind, target.executionID, target.method = "steer", parts[2], "PATCH"
			return target, "PATCH"
		case len(parts) == 4 && parts[1] == "executions" && parts[2] != "" && parts[3] == "timeline":
			target.kind, target.executionID = "timeline", parts[2]
		case len(parts) == 4 && parts[1] == "executions" && parts[2] != "" && parts[3] == "report":
			target.kind, target.executionID = "report", parts[2]
		case len(parts) == 4 && parts[1] == "executions" && parts[2] != "" &&
			(parts[3] == "github-observation" || parts[3] == "local-checks"):
			target.kind, target.executionID, target.method = parts[3], parts[2], "POST"
			return target, "POST"
		case len(parts) == 4 && parts[1] == "executions" && parts[2] != "" &&
			(parts[3] == "cancel" || parts[3] == "close-failure" || parts[3] == "retry-finalization"):
			target.kind, target.executionID, target.method = parts[3], parts[2], "POST"
			return target, "POST"
		case len(parts) == 4 && parts[1] == "executions" && parts[2] != "" && parts[3] == "writer":
			target.kind, target.executionID, target.method = "writer-register", parts[2], "POST"
			if method == "PATCH" {
				target.kind, target.method = "writer-update", "PATCH"
			}
			return target, "POST"
		case len(parts) == 4 && parts[1] == "executions" && parts[2] != "" &&
			(parts[3] == "result" || parts[3] == "stop-confirmed" || parts[3] == "reply" || parts[3] == "rerun"):
			target.kind, target.executionID, target.method = parts[3], parts[2], "POST"
			if parts[3] == "result" {
				target.kind = "writer-result"
			} else if parts[3] == "stop-confirmed" {
				target.kind = "writer-stop"
			}
			return target, "POST"
		case len(parts) == 5 && parts[1] == "executions" && parts[2] != "" && parts[3] == "writer" && parts[4] == "withdraw":
			target.kind, target.executionID, target.method = "writer-withdraw", parts[2], "POST"
			return target, "POST"
		case len(parts) == 5 && parts[1] == "executions" && parts[2] != "" && parts[3] == "drafts" && parts[4] != "":
			target.kind, target.executionID, target.revision = "draft", parts[2], parts[4]
		case len(parts) == 6 && parts[1] == "executions" && parts[2] != "" && parts[3] == "writers" &&
			parts[4] != "" && parts[5] == "usage":
			target.kind, target.executionID, target.writerID, target.method = "captain-usage", parts[2], parts[4], "POST"
			return target, "POST"
		default:
			return route{}, ""
		}
		return target, "GET"
	}
	if strings.HasPrefix(path, "/v1/reconcilers/") {
		parts := strings.Split(strings.TrimPrefix(path, "/v1/reconcilers/"), "/")
		if parts[0] != "" {
			target := route{runnerID: parts[0], kind: "reconciler-retire", method: "DELETE"}
			if len(parts) == 1 {
				return target, "DELETE"
			}
			if len(parts) == 2 && parts[1] == "heartbeat" {
				target.kind, target.method = "reconciler-heartbeat", "POST"
				return target, "POST"
			}
		}
	}
	if strings.HasPrefix(path, "/v1/captains/") {
		parts := strings.Split(strings.TrimPrefix(path, "/v1/captains/"), "/")
		if parts[0] != "" {
			target := route{captainID: parts[0], kind: "captain", method: "GET"}
			if len(parts) == 1 {
				if method == "PUT" {
					target.kind, target.method = "captain-heartbeat", "PUT"
				}
				return target, "PUT"
			}
			if len(parts) == 2 && parts[1] == "reinforcement" {
				target.kind, target.method = "captain-reinforce", "PUT"
				return target, "PUT"
			}
		}
	}
	return route{}, ""
}

func bodyRoute(kind string) bool {
	switch kind {
	case "intake", "register", "set-title", "cancel", "close-failure", "retry-finalization",
		"writer-register", "writer-update", "writer-withdraw", "writer-progress", "writer-capacity", "set-dispatch",
		"writer-result", "writer-stop", "reply", "steer", "rerun", "captain-register", "captain-heartbeat", "captain-reinforce", "captain-action", "archive-write",
		"github-observation", "local-checks", "reconciler-heartbeat", "writer-usage", "captain-usage":
		return true
	default:
		return false
	}
}

func (h *Handler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	route, allowed := match(r.URL.Path, r.Method)
	if route.kind == "" {
		// Starlette's trailing-slash redirect is performed before dependencies.
		path := strings.TrimRight(r.URL.Path, "/")
		if path != r.URL.Path {
			other, _ := match(path, r.Method)
			if other.kind != "" {
				target := *r.URL
				target.Path = path
				target.RawPath = ""
				target.Scheme = scheme(r)
				target.Host = headerText(r.Host)
				w.Header().Set("Location", target.String())
				w.WriteHeader(http.StatusTemporaryRedirect)
				return
			}
		}
		writeJSON(w, http.StatusNotFound, map[string]any{"detail": "Not Found"})
		return
	}
	if r.Method != route.method {
		w.Header().Set("Allow", allowed)
		writeJSON(w, http.StatusMethodNotAllowed, map[string]any{"detail": "Method Not Allowed"})
		return
	}
	// Python dispatched sync work continues even when the socket disconnects,
	// including waiting for admission. Explicit interruption is a store API
	// context, not an HTTP caller's cancel signal.
	ctx := context.WithoutCancel(r.Context())
	if route.kind == "health" {
		if err := h.App.Store.Health(ctx); err != nil {
			writeJSON(w, 503, map[string]any{"detail": "SQLite store unavailable"})
		} else {
			writeJSON(w, 200, map[string]any{"status": "ok"})
		}
		return
	}
	var input any
	var keys []string
	bodyPresent := false
	unpaired := false
	if bodyRoute(route.kind) {
		data, err := io.ReadAll(r.Body)
		if err != nil {
			h.failure(w, err)
			return
		}
		if len(data) > 0 {
			bodyPresent = true
			contentType := strings.ToLower(strings.TrimSpace(strings.Split(r.Header.Get("Content-Type"), ";")[0]))
			if contentType == "" || contentType == "application/json" ||
				(strings.HasPrefix(contentType, "application/") && strings.HasSuffix(contentType, "+json")) {
				input, keys, err = contract.Decode(data)
				if err != nil {
					var malformed *contract.BodyError
					if errors.As(err, &malformed) {
						writeJSON(w, 400, map[string]any{"detail": malformed.Error()})
						return
					}
					var syntax *contract.JSONError
					if !errors.As(err, &syntax) {
						h.failure(w, err)
						return
					}
					writeJSON(w, 422, map[string]any{"detail": []contract.Issue{
						{Type: "json_invalid", Loc: []any{"body", syntax.Offset}, Msg: "JSON decode error", Input: map[string]any{}, Ctx: map[string]any{"error": syntax.Message}},
					}})
					return
				}
				unpaired = contract.HasUnpaired(input)
			} else {
				// FastAPI exposes non-JSON body bytes as validation input.
				input = string(data)
			}
		}
	}
	role, writerID, err := h.authenticate(ctx, r, route.itemID, route.executionID)
	if err != nil {
		h.failure(w, err)
		return
	}
	if role == "" {
		w.Header().Set("WWW-Authenticate", "Bearer")
		writeJSON(w, 401, map[string]any{"detail": "Valid bearer token required"})
		return
	}
	if role == "out_of_scope" {
		writeJSON(w, 403, map[string]any{"detail": "This writer token is scoped to another item"})
		return
	}
	if (route.kind == "writer-register" || route.kind == "writer-update" || route.kind == "writer-withdraw" ||
		route.kind == "captain-register" || route.kind == "captain-heartbeat" || route.kind == "captain-reinforce" || route.kind == "captain-action" ||
		route.kind == "archive-read" || route.kind == "archive-write" || route.kind == "reconciler-retire" || route.kind == "captain-usage") &&
		role != "owner" && role != "runner" {
		writeJSON(w, 403, map[string]any{"detail": "Owner or runner token required"})
		return
	}
	if (route.kind == "writer-attempt" || route.kind == "writer-progress" || route.kind == "writer-capacity" ||
		route.kind == "writer-result" || route.kind == "writer-stop" || route.kind == "writer-usage") &&
		role != "writer" {
		writeJSON(w, 403, map[string]any{"detail": "Writer token required"})
		return
	}
	required := ""
	if route.kind == "owner" || route.kind == "intake" || route.kind == "register" ||
		route.kind == "title" || route.kind == "set-title" || route.kind == "cancel" ||
		route.kind == "close-failure" || route.kind == "retry-finalization" || route.kind == "inbox" ||
		route.kind == "set-dispatch" || route.kind == "reply" || route.kind == "steer" || route.kind == "rerun" {
		required = "owner"
	} else if route.kind == "runner" || route.kind == "github-observation" || route.kind == "local-checks" || route.kind == "reconciler-heartbeat" {
		required = "runner"
	}
	if required != "" && role != required {
		message := "Owner token required"
		if required == "runner" {
			message = "Runner token required"
		}
		writeJSON(w, 403, map[string]any{"detail": message})
		return
	}
	if (route.kind == "intake" || route.kind == "set-title" || route.kind == "set-dispatch") && !sameOrigin(r) {
		writeJSON(w, 403, map[string]any{"detail": "Browser writes require a same-origin request"})
		return
	}
	if unpaired {
		h.failure(w, errors.New("unpaired surrogate in request validation input"))
		return
	}
	var pathIssues []contract.Issue
	if route.itemID != "" {
		pathIssues = append(pathIssues, contract.ValidateIdentifier(route.itemID, "item_id")...)
	}
	if route.executionID != "" {
		pathIssues = append(pathIssues, contract.ValidateIdentifier(route.executionID, "execution_id")...)
	}
	if route.captainID != "" {
		pathIssues = append(pathIssues, contract.ValidateIdentifier(route.captainID, "captain_id")...)
	}
	if route.runnerID != "" {
		pathIssues = append(pathIssues, contract.ValidateIdentifier(route.runnerID, "runner_id")...)
	}
	if route.writerID != "" {
		pathIssues = append(pathIssues, contract.ValidateIdentifier(route.writerID, "writer_id")...)
	}
	if route.revision != "" {
		pathIssues = append(pathIssues, contract.ValidateRevision(route.revision)...)
	}
	if !bodyRoute(route.kind) && len(pathIssues) > 0 {
		writeJSON(w, 422, map[string]any{"detail": pathIssues})
		return
	}
	var result json.RawMessage
	var status = 200
	switch route.kind {
	case "writer-usage", "captain-usage":
		if !bodyPresent {
			writeJSON(w, 422, map[string]any{"detail": append(pathIssues, missingBody()...)})
			return
		}
		var issues []contract.Issue
		if route.kind == "writer-usage" {
			request, bodyIssues := contract.ValidateUsageSnapshot(input, keys)
			issues = append(pathIssues, bodyIssues...)
			if len(issues) == 0 {
				result, err = h.App.OwnUsage(ctx, writerID, request)
			}
		} else {
			request, bodyIssues := contract.ValidateCaptainUsage(input, keys)
			issues = append(pathIssues, bodyIssues...)
			if len(issues) == 0 {
				result, err = h.App.CaptainUsage(ctx, route.itemID, route.executionID, route.writerID, request)
			}
		}
		if len(issues) > 0 {
			writeJSON(w, 422, map[string]any{"detail": issues})
			return
		}
	case "github-observation", "local-checks", "reconciler-heartbeat", "reconciler-retire":
		if bodyRoute(route.kind) && !bodyPresent && route.kind != "reconciler-heartbeat" {
			writeJSON(w, 422, map[string]any{"detail": append(pathIssues, missingBody()...)})
			return
		}
		var issues []contract.Issue
		switch route.kind {
		case "github-observation":
			request, bodyIssues := contract.ValidateObservation(input, keys)
			issues = append(pathIssues, bodyIssues...)
			if len(issues) == 0 {
				result, err = h.App.RecordObservation(ctx, route.itemID, route.executionID, request)
			}
		case "local-checks":
			request, bodyIssues := contract.ValidateLocalChecks(input, keys)
			issues = append(pathIssues, bodyIssues...)
			if len(issues) == 0 {
				result, err = h.App.RecordLocalChecks(ctx, route.itemID, route.executionID, request)
			}
		case "reconciler-heartbeat":
			var request *contract.ReconcilerHeartbeat
			issues = pathIssues
			if input != nil {
				body, bodyIssues := contract.ValidateReconcilerHeartbeat(input, keys)
				request = &body
				issues = append(issues, bodyIssues...)
			}
			if len(issues) == 0 {
				result, err = h.App.ReconcilerHeartbeat(ctx, route.runnerID, request)
			}
		case "reconciler-retire":
			result, err = h.App.RetireReconciler(ctx, route.runnerID)
		}
		if len(issues) > 0 {
			writeJSON(w, 422, map[string]any{"detail": issues})
			return
		}
	case "archive-read":
		sessionID, issues := contract.ValidateArchiveQuery(r.URL.Query())
		if len(issues) > 0 {
			writeJSON(w, 422, map[string]any{"detail": issues})
			return
		}
		result, err = h.App.ArchiveStatus(ctx, sessionID)
	case "archive-write":
		if !bodyPresent {
			writeJSON(w, 422, map[string]any{"detail": missingBody()})
			return
		}
		request, issues := contract.ValidateArchive(input, keys)
		if len(issues) > 0 {
			writeJSON(w, 422, map[string]any{"detail": issues})
			return
		}
		result, err = h.App.RecordArchive(ctx, request)
	case "captains", "captain", "captain-current", "captain-register", "captain-heartbeat", "captain-reinforce", "captain-action":
		key, keyIssues := requestKey(r)
		if route.kind == "captain-register" || route.kind == "captain-action" {
			pathIssues = append(pathIssues, keyIssues...)
		} else {
			key = nil
		}
		if bodyRoute(route.kind) && !bodyPresent {
			writeJSON(w, 422, map[string]any{"detail": append(pathIssues, missingBody()...)})
			return
		}
		var issues []contract.Issue
		result, issues, err = h.captainOperation(ctx, r, route, input, keys, pathIssues, key)
		if len(issues) > 0 {
			writeJSON(w, 422, map[string]any{"detail": issues})
			return
		}
		if route.kind == "captain-register" || route.kind == "captain-action" {
			status = 201
		}
	case "writer-register", "writer-update", "writer-withdraw", "writer-attempt", "writer-progress", "writer-capacity", "writer-result", "writer-stop":
		var key *string
		if route.kind == "writer-result" || route.kind == "writer-stop" {
			var keyIssues []contract.Issue
			key, keyIssues = requestKey(r)
			pathIssues = append(pathIssues, keyIssues...)
		}
		if bodyRoute(route.kind) && !bodyPresent {
			writeJSON(w, 422, map[string]any{"detail": append(pathIssues, missingBody()...)})
			return
		}
		var issues []contract.Issue
		result, issues, err = h.writerOperation(ctx, route, writerID, input, keys, pathIssues, key)
		if len(issues) > 0 {
			writeJSON(w, 422, map[string]any{"detail": issues})
			return
		}
	case "reply":
		key, issues := requestKey(r)
		issues = append(pathIssues, issues...)
		if key == nil {
			issues = append(issues, contract.Issue{Type: "missing", Loc: []any{"header", "Idempotency-Key"}, Msg: "Field required", Input: nil})
		}
		if !bodyPresent {
			writeJSON(w, 422, map[string]any{"detail": append(issues, missingBody()...)})
			return
		}
		request, bodyIssues := contract.ValidateReply(input, keys)
		issues = append(issues, bodyIssues...)
		if len(issues) > 0 {
			writeJSON(w, 422, map[string]any{"detail": issues})
			return
		}
		result, err = h.App.Reply(ctx, route.itemID, route.executionID, request, *key)
	case "steer", "rerun":
		key, issues := requestKey(r)
		issues = append(pathIssues, issues...)
		if !bodyPresent {
			writeJSON(w, 422, map[string]any{"detail": append(issues, missingBody()...)})
			return
		}
		if route.kind == "steer" {
			request, bodyIssues := contract.ValidateSteering(input, keys)
			issues = append(issues, bodyIssues...)
			if len(issues) > 0 {
				writeJSON(w, 422, map[string]any{"detail": issues})
				return
			}
			result, err = h.App.Steer(ctx, route.itemID, route.executionID, request, key)
		} else {
			request, bodyIssues := contract.ValidateRerun(input, keys)
			issues = append(issues, bodyIssues...)
			if len(issues) > 0 {
				writeJSON(w, 422, map[string]any{"detail": issues})
				return
			}
			result, err = h.App.Rerun(ctx, route.itemID, route.executionID, request, key)
		}
	case "owner", "runner":
		writeJSON(w, 200, map[string]any{"role": role})
		return
	case "repositories":
		result, err = h.App.Repositories(ctx)
	case "dispatch":
		result, err = h.App.DispatchSettings(ctx)
	case "set-dispatch":
		if !bodyPresent {
			writeJSON(w, 422, map[string]any{"detail": missingBody()})
			return
		}
		request, issues := contract.ValidateDispatchUpdate(input, keys)
		if len(issues) > 0 {
			writeJSON(w, 422, map[string]any{"detail": issues})
			return
		}
		result, err = h.App.SetDispatch(ctx, request)
	case "inbox":
		query, issues := contract.ValidateInboxQuery(r.URL.Query())
		if len(issues) > 0 {
			writeJSON(w, 422, map[string]any{"detail": issues})
			return
		}
		result, err = h.App.Inbox(ctx, query)
	case "register":
		if !bodyPresent {
			writeJSON(w, 422, map[string]any{"detail": missingBody()})
			return
		}
		request, issues := contract.ValidateRegistration(input, keys)
		if len(issues) > 0 {
			writeJSON(w, 422, map[string]any{"detail": issues})
			return
		}
		result, err = h.App.Register(ctx, request)
	case "inspect":
		result, err = h.App.Inspect(ctx, route.itemID)
	case "title":
		result, err = h.App.DisplayTitle(ctx, route.itemID)
	case "history":
		result, err = h.App.History(ctx, route.itemID)
	case "timeline":
		result, err = h.App.Timeline(ctx, route.itemID, route.executionID)
	case "draft":
		var markdown string
		markdown, err = h.App.Draft(ctx, route.itemID, route.executionID, route.revision)
		if err == nil {
			w.Header().Set("Content-Type", "text/plain; charset=utf-8")
			w.WriteHeader(200)
			w.Write([]byte(markdown))
			return
		}
	case "report":
		var markdown string
		markdown, err = h.App.Report(ctx, route.itemID, route.executionID)
		if err == nil {
			w.Header().Set("Content-Type", "text/plain; charset=utf-8")
			w.WriteHeader(200)
			w.Write([]byte(markdown))
			return
		}
	case "set-title":
		issues := pathIssues
		key := ""
		if values, exists := r.Header["Idempotency-Key"]; exists && len(values) > 0 {
			key = headerText(values[0])
			issues = append(issues, contract.ValidateKey(key)...)
		} else {
			issues = append(issues, contract.Issue{Type: "missing", Loc: []any{"header", "Idempotency-Key"}, Msg: "Field required", Input: nil})
		}
		if !bodyPresent {
			writeJSON(w, 422, map[string]any{"detail": append(issues, missingBody()...)})
			return
		}
		request, bodyIssues := contract.ValidateDisplayTitle(input, keys)
		issues = append(issues, bodyIssues...)
		if len(issues) > 0 {
			writeJSON(w, 422, map[string]any{"detail": issues})
			return
		}
		result, err = h.App.SetDisplayTitle(ctx, route.itemID, request, key)
	case "cancel", "close-failure", "retry-finalization":
		key, issues := requestKey(r)
		issues = append(pathIssues, issues...)
		if !bodyPresent {
			writeJSON(w, 422, map[string]any{"detail": append(issues, missingBody()...)})
			return
		}
		if route.kind == "retry-finalization" {
			request, bodyIssues := contract.ValidateVersioned(input, keys)
			issues = append(issues, bodyIssues...)
			if len(issues) > 0 {
				writeJSON(w, 422, map[string]any{"detail": issues})
				return
			}
			result, err = h.App.RetryFinalization(ctx, route.itemID, route.executionID, request, key)
		} else {
			request, bodyIssues := contract.ValidateCancellation(input, keys)
			issues = append(issues, bodyIssues...)
			if len(issues) > 0 {
				writeJSON(w, 422, map[string]any{"detail": issues})
				return
			}
			event := application.Cancel
			if route.kind == "close-failure" {
				event = application.CloseFailure
			}
			result, err = h.App.RequestStop(ctx, route.itemID, route.executionID, request, event, key)
		}
	case "intake":
		var issues []contract.Issue
		var key *string
		if values, exists := r.Header["Idempotency-Key"]; exists && len(values) > 0 {
			value := headerText(values[0])
			key = &value
			issues = contract.ValidateKey(value)
		}
		if !bodyPresent {
			writeJSON(w, 422, map[string]any{"detail": append(issues, missingBody()...)})
			return
		}
		request, bodyIssues := contract.ValidateIntake(input, keys)
		issues = append(issues, bodyIssues...)
		if len(issues) > 0 {
			writeJSON(w, 422, map[string]any{"detail": issues})
			return
		}
		if h.dispatch != nil {
			h.dispatch()
		}
		result, err = h.App.Intake(ctx, request, key)
		status = 201
	}
	if err != nil {
		h.failure(w, err)
		return
	}
	value, _, err := contract.Decode(result)
	if err != nil {
		h.failure(w, err)
		return
	}
	writeJSON(w, status, value)
}

func requestKey(r *http.Request) (*string, []contract.Issue) {
	if values, exists := r.Header["Idempotency-Key"]; exists && len(values) > 0 {
		value := headerText(values[0])
		return &value, contract.ValidateKey(value)
	}
	return nil, nil
}

func (h *Handler) authenticate(ctx context.Context, r *http.Request, itemID, executionID string) (string, string, error) {
	authorization := headerText(r.Header.Get("Authorization"))
	scheme, token, found := strings.Cut(authorization, " ")
	if !found || !strings.EqualFold(scheme, "bearer") || token == "" {
		return "", "", nil
	}
	if subtle.ConstantTimeCompare([]byte(token), []byte(h.Owner)) == 1 {
		return "owner", "", nil
	}
	if subtle.ConstantTimeCompare([]byte(token), []byte(h.Runner)) == 1 {
		return "runner", "", nil
	}
	var writer sqlite.Writer
	err := h.App.Store.Read(ctx, func(tx *sqlite.Tx) error {
		var err error
		writer, err = tx.Writer(contract.Digest(token))
		return err
	})
	if err == sql.ErrNoRows {
		return "", "", nil
	}
	if err != nil {
		return "", "", err
	}
	if r.URL.Path == "/v1/writer" || strings.HasPrefix(r.URL.Path, "/v1/writer/") {
		return "writer", writer.ID, nil
	}
	if itemID != writer.ItemID || (executionID != "" && executionID != writer.ExecutionID) {
		return "out_of_scope", "", nil
	}
	return "writer", writer.ID, nil
}

func scheme(r *http.Request) string {
	if r.TLS != nil {
		return "https"
	}
	return "http"
}

func sameOrigin(r *http.Request) bool {
	origin, present := r.Header["Origin"]
	site, sitePresent := r.Header["Sec-Fetch-Site"]
	value := ""
	if len(origin) > 0 {
		value = headerText(origin[0])
	}
	return (!present || value == scheme(r)+"://"+headerText(r.Host)) &&
		(!sitePresent || (len(site) > 0 && site[0] == "same-origin" && present))
}

// ASGI/Starlette decodes HTTP header bytes as Latin-1, not UTF-8.
func headerText(value string) string {
	runes := make([]rune, len(value))
	for i := 0; i < len(value); i++ {
		runes[i] = rune(value[i])
	}
	return string(runes)
}

func missingBody() []contract.Issue {
	return []contract.Issue{{Type: "missing", Loc: []any{"body"}, Msg: "Field required", Input: nil}}
}

func (h *Handler) failure(w http.ResponseWriter, err error) {
	var missing *application.Missing
	var conflict *application.Conflict
	var forbidden *application.Forbidden
	switch {
	case errors.As(err, &missing):
		writeJSON(w, 404, map[string]any{"detail": missing.Message})
	case errors.As(err, &conflict):
		writeJSON(w, 409, map[string]any{"detail": conflict.Message})
	case errors.As(err, &forbidden):
		writeJSON(w, 403, map[string]any{"detail": forbidden.Message})
	case sqlite.IsBusy(err):
		w.Header().Set("Retry-After", "1")
		writeJSON(w, 503, map[string]any{"detail": "SQLite store unavailable (" + sqlite.BusyReason(err) + "); retry the same request"})
	default:
		if h.Logger != nil {
			// No payload, authorization, token, or database rows in diagnostics.
			h.Logger.Error("HTTP operation failed", "error", err)
		}
		internalError(w)
	}
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	var buffer bytes.Buffer
	encoder := json.NewEncoder(&buffer)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(value); err != nil {
		slog.Error("Could not encode HTTP response", "error", err)
		internalError(w)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	w.Write(bytes.TrimSuffix(buffer.Bytes(), []byte("\n")))
}

func internalError(w http.ResponseWriter) {
	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	w.WriteHeader(500)
	w.Write([]byte("Internal Server Error"))
}

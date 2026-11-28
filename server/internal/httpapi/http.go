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
	App             *application.App
	Owner           string
	Runner          string
	Logger          *slog.Logger
	Home            *string
	LocalBoard      bool
	AssetsDirectory string
	dispatch        func() // test-only barrier at mutation dispatch, never wire/config input
}

func (h *Handler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.ProtoMajor == 1 && r.ProtoMinor == 0 {
		// Parsing is complete; net/http serializes the reply from these fields.
		r.Proto, r.ProtoMinor = "HTTP/1.1", 1
		w.Header().Set("Connection", "close")
	}
	route, allowed := match(r.URL.Path, r.Method)
	if route.routeSpec == nil {
		h.notFound(w, r)
		return
	}
	if route.method != "*" && r.Method != route.method {
		w.Header().Set("Allow", allowed)
		writeJSON(w, http.StatusMethodNotAllowed, map[string]any{"detail": "Method Not Allowed"})
		return
	}
	// Dispatched Python work survives caller disconnect, including admission.
	q := &requestState{ctx: context.WithoutCancel(r.Context()), route: route}
	if route.policy != public {
		if !h.readBody(w, r, q) || !h.authorize(w, r, q) {
			return
		}
		q.validatePath()
		if !route.body && len(q.pathIssues) > 0 && route.operation != opOperatorGroup && route.operation != opOperatorTimeline {
			writeJSON(w, 422, map[string]any{"detail": q.pathIssues})
			return
		}
	}
	route.serve(h, w, r, q)
}

func (h *Handler) notFound(w http.ResponseWriter, r *http.Request) {
	// Starlette's trailing-slash redirect precedes dependencies.
	path := strings.TrimRight(r.URL.Path, "/")
	if path == r.URL.Path {
		path += "/"
	}
	if path != r.URL.Path {
		other, _ := match(path, r.Method)
		if other.routeSpec != nil {
			target := *r.URL
			target.Path, target.RawPath = path, ""
			target.Scheme, target.Host = scheme(r), headerText(r.Host)
			w.Header().Set("Location", target.String())
			w.WriteHeader(http.StatusTemporaryRedirect)
			return
		}
	}
	writeJSON(w, http.StatusNotFound, map[string]any{"detail": "Not Found"})
}

func (h *Handler) readBody(w http.ResponseWriter, r *http.Request, q *requestState) bool {
	var data []byte
	var err error
	if q.route.body {
		data, err = io.ReadAll(r.Body)
	} else {
		// Even ignored bodies must leave no unread request bytes that can
		// reset an authorization reply when the connection closes.
		_, err = io.Copy(io.Discard, r.Body)
	}
	if err != nil {
		if h.Logger != nil {
			h.Logger.Error("HTTP body framing failed", "error", err)
		}
		w.Header().Set("Content-Type", "text/plain; charset=utf-8")
		w.WriteHeader(400)
		w.Write([]byte("Invalid HTTP request received."))
		return false
	}
	if q.route.body {
		if len(data) > 0 {
			q.bodyPresent = true
			contentType := strings.ToLower(strings.TrimSpace(strings.Split(r.Header.Get("Content-Type"), ";")[0]))
			if contentType == "" || contentType == "application/json" ||
				(strings.HasPrefix(contentType, "application/") && strings.HasSuffix(contentType, "+json")) {
				q.input, q.keys, err = contract.Decode(data)
				if err != nil {
					writeJSON(w, 422, map[string]any{"detail": []contract.Issue{
						{Loc: []any{"body"}, Msg: "Invalid JSON body"},
					}})
					return false
				}
			} else {
				q.input = string(data)
			}
		}
	}
	return true
}

func (q *requestState) validatePath() {
	route := q.route
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
	if route.passID != "" {
		pathIssues = append(pathIssues, contract.ValidateIdentifier(route.passID, "pass_id")...)
	}
	if route.repositoryID != "" && route.policy != boardOwner {
		pathIssues = append(pathIssues, contract.ValidateIdentifier(route.repositoryID, "repository_id")...)
	}
	if route.label != "" {
		pathIssues = append(pathIssues, contract.ValidateLabel(route.label, "path")...)
	}
	var pathNumber json.Number
	if route.number != "" {
		var issues []contract.Issue
		pathNumber, issues = contract.ValidatePathInteger(route.number, "number")
		pathIssues = append(pathIssues, issues...)
	}
	if route.revision != "" {
		pathIssues = append(pathIssues, contract.ValidateRevision(route.revision)...)
	}
	q.pathIssues, q.pathNumber = pathIssues, pathNumber

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
	if value, ok := r.Context().Value(proxySchemeKey{}).(string); ok {
		return value
	}
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
	return []contract.Issue{{Loc: []any{"body"}, Msg: "Field required"}}
}

func (h *Handler) failure(w http.ResponseWriter, err error) {
	var missing *application.Missing
	var conflict *application.Conflict
	var forbidden *application.Forbidden
	var bad *application.BadRequest
	switch {
	case errors.As(err, &missing):
		writeJSON(w, 404, map[string]any{"detail": missing.Message})
	case errors.As(err, &conflict):
		writeJSON(w, 409, map[string]any{"detail": conflict.Message})
	case errors.As(err, &forbidden):
		writeJSON(w, 403, map[string]any{"detail": forbidden.Message})
	case errors.As(err, &bad):
		writeJSON(w, 400, map[string]any{"detail": bad.Message})
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

func (h *Handler) respond(w http.ResponseWriter, status int, value any, err error) {
	if err != nil {
		h.failure(w, err)
		return
	}
	writeJSONValue(w, status, value, true)
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	writeJSONValue(w, status, value, false)
}

func writeJSONValue(w http.ResponseWriter, status int, value any, escapeHTML bool) {
	var buffer bytes.Buffer
	encoder := json.NewEncoder(&buffer)
	// Preserve application responses' historical HTML escaping.
	encoder.SetEscapeHTML(escapeHTML)
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

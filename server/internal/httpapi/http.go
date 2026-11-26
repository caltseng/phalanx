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
	kind   string
	itemID string
	method string
}

func match(path, method string) (route, string) {
	switch path {
	case "/health":
		return route{kind: "health", method: "GET"}, "GET"
	case "/v1/owner/status":
		return route{kind: "owner", method: "GET"}, "GET"
	case "/v1/runner/status":
		return route{kind: "runner", method: "GET"}, "GET"
	case "/v1/repositories":
		if method == "POST" {
			return route{kind: "register", method: "POST"}, "GET, POST"
		}
		return route{kind: "repositories", method: "GET"}, "GET, POST"
	case "/v1/items":
		return route{kind: "intake", method: "POST"}, "POST"
	}
	if strings.HasPrefix(path, "/v1/items/") {
		id := strings.TrimPrefix(path, "/v1/items/")
		if id != "" && !strings.Contains(id, "/") {
			return route{kind: "inspect", itemID: id, method: "GET"}, "GET"
		}
	}
	return route{}, ""
}

func (h *Handler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	route, allowed := match(r.URL.Path, r.Method)
	if route.kind == "" {
		// Starlette's trailing-slash redirect is performed before dependencies.
		path := strings.TrimSuffix(r.URL.Path, "/")
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
	if route.kind == "intake" || route.kind == "register" {
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
				unpaired = contract.HasUnpairedSurrogate(data)
			} else {
				// FastAPI exposes non-JSON body bytes as validation input.
				input = string(data)
			}
		}
	}
	role, err := h.authenticate(ctx, r, route.itemID)
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
	required := ""
	if route.kind == "owner" || route.kind == "intake" || route.kind == "register" {
		required = "owner"
	} else if route.kind == "runner" {
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
	if route.kind == "intake" && !sameOrigin(r) {
		writeJSON(w, 403, map[string]any{"detail": "Browser writes require a same-origin request"})
		return
	}
	if unpaired {
		h.failure(w, errors.New("unpaired surrogate in request validation input"))
		return
	}
	var result json.RawMessage
	var status = 200
	switch route.kind {
	case "owner", "runner":
		writeJSON(w, 200, map[string]any{"role": role})
		return
	case "repositories":
		result, err = h.App.Repositories(ctx)
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
		if issues := contract.ValidateIdentifier(route.itemID, "item_id"); len(issues) > 0 {
			writeJSON(w, 422, map[string]any{"detail": issues})
			return
		}
		result, err = h.App.Inspect(ctx, route.itemID)
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

func (h *Handler) authenticate(ctx context.Context, r *http.Request, itemID string) (string, error) {
	authorization := headerText(r.Header.Get("Authorization"))
	scheme, token, found := strings.Cut(authorization, " ")
	if !found || !strings.EqualFold(scheme, "bearer") || token == "" {
		return "", nil
	}
	if subtle.ConstantTimeCompare([]byte(token), []byte(h.Owner)) == 1 {
		return "owner", nil
	}
	if subtle.ConstantTimeCompare([]byte(token), []byte(h.Runner)) == 1 {
		return "runner", nil
	}
	var writer sqlite.Writer
	err := h.App.Store.Read(ctx, func(tx *sqlite.Tx) error {
		var err error
		writer, err = tx.Writer(contract.Digest(token))
		return err
	})
	if err == sql.ErrNoRows {
		return "", nil
	}
	if err != nil {
		return "", err
	}
	if itemID != writer.ItemID {
		return "out_of_scope", nil
	}
	return "writer", nil
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
	switch {
	case errors.As(err, &missing):
		writeJSON(w, 404, map[string]any{"detail": missing.Message})
	case errors.As(err, &conflict):
		writeJSON(w, 409, map[string]any{"detail": conflict.Message})
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

package httpapi

import (
	"net/http"

	"github.com/caltseng/phalanx/server/internal/contract"
)

func (h *Handler) serveHealth(w http.ResponseWriter, r *http.Request, q *requestState) {
	if err := h.App.Store.Health(q.ctx); err != nil {
		writeJSON(w, 503, map[string]any{"detail": "SQLite store unavailable"})
	} else {
		writeJSON(w, 200, map[string]any{"status": "ok"})
	}
}

func (h *Handler) serveOperatorShell(w http.ResponseWriter, r *http.Request, q *requestState) {
	var issues []contract.Issue
	if q.route.itemID != "" {
		issues = contract.ValidateBoardIdentifier(q.route.itemID, "item_id")
	} else if q.route.executionID != "" {
		issues = contract.ValidateBoardIdentifier(q.route.executionID, "execution_id")
	}
	if len(issues) > 0 {
		writeJSON(w, 422, map[string]any{"detail": issues})
		return
	}
	h.serveAsset(w, r, true)
}

func (h *Handler) serveOperatorAsset(w http.ResponseWriter, r *http.Request, q *requestState) {
	h.serveAsset(w, r, false)
}

func (h *Handler) serveDocumentationRoute(w http.ResponseWriter, r *http.Request, q *requestState) {
	h.serveDocumentation(w, r)
}

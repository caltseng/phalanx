package httpapi

import "net/http"

type accessPolicy uint8

const (
	public accessPolicy = iota
	authenticated
	ownerOnly
	runnerOnly
	writerOnly
	ownerRunner
	boardReader
	boardOwner
)

type originRule uint8

const (
	noOrigin originRule = iota
	browserOrigin
	localOrigin
	browserAndLocalOrigin
)

func (p accessPolicy) localAuthority() bool {
	return p == boardReader || p == boardOwner
}

func (p accessPolicy) refusal(role string) string {
	switch p {
	case ownerOnly, boardOwner:
		if role != "owner" && !(p == boardOwner && role == "local") {
			return "Owner token required"
		}
	case runnerOnly:
		if role != "runner" {
			return "Runner token required"
		}
	case writerOnly:
		if role != "writer" {
			return "Writer token required"
		}
	case ownerRunner:
		if role != "owner" && role != "runner" {
			return "Owner or runner token required"
		}
	}
	return ""
}

func (h *Handler) authorize(w http.ResponseWriter, r *http.Request, q *requestState) bool {
	var err error
	if q.route.policy.localAuthority() && h.localBoardAuthority(r) {
		q.role = "local"
	} else {
		q.role, q.writerID, err = h.authenticate(q.ctx, r, q.route.itemID, q.route.executionID)
	}
	if err != nil {
		h.failure(w, err)
		return false
	}
	if q.role == "" {
		w.Header().Set("WWW-Authenticate", "Bearer")
		writeJSON(w, 401, map[string]any{"detail": "Valid bearer token required"})
		return false
	}
	if q.role == "out_of_scope" {
		writeJSON(w, 403, map[string]any{"detail": "This writer token is scoped to another item"})
		return false
	}
	if message := q.refusal(r); message != "" {
		writeJSON(w, 403, map[string]any{"detail": message})
		return false
	}
	return true
}

func (q *requestState) refusal(r *http.Request) string {
	origin := q.route.origin
	if q.role == "local" && (origin == localOrigin || origin == browserAndLocalOrigin) && !localBoardOrigin(r) {
		return "Local dashboard actions require a same-origin request"
	}
	if message := q.route.policy.refusal(q.role); message != "" {
		return message
	}
	if (origin == browserOrigin || origin == browserAndLocalOrigin) && !sameOrigin(r) {
		return "Browser writes require a same-origin request"
	}
	return ""
}

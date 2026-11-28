package httpapi

import (
	"net/http"

	"github.com/caltseng/phalanx/server/internal/application"
	"github.com/caltseng/phalanx/server/internal/contract"
)

func (h *Handler) serveOperatorRead(w http.ResponseWriter, r *http.Request, q *requestState) {
	var result any
	var err error
	status := 200
	var request contract.OperatorQuery
	var issues []contract.Issue
	if q.route.operation == opOperatorList {
		request, issues = contract.ValidateOperatorQuery(r.URL.Query())
	} else if q.route.operation == opOperatorGroup || q.route.operation == opOperatorTimeline {
		request, issues = contract.ValidateOperatorPage(r.URL.Query())
	}
	issues = append(q.pathIssues, issues...)
	if len(issues) > 0 {
		writeJSON(w, 422, map[string]any{"detail": issues})
		return
	}
	condition := application.ConditionalRead{Path: r.URL.Path, Query: r.URL.RawQuery, IfNoneMatch: headerText(r.Header.Get("If-None-Match"))}
	var response application.OperatorResponse
	switch q.route.operation {
	case opOperatorList:
		response, err = h.App.OperatorList(q.ctx, request, condition)
	case opOperatorOverview:
		response, err = h.App.OperatorOverview(q.ctx, condition)
	case opOperatorItem:
		response, err = h.App.OperatorItem(q.ctx, q.route.itemID, condition)
	case opOperatorGroup:
		response, err = h.App.OperatorGroup(q.ctx, q.route.label, request, condition)
	case opOperatorTimeline:
		result, err = h.App.OperatorTimeline(q.ctx, q.route.executionID, request)
	}
	if err != nil {
		h.failure(w, err)
		return
	}
	if response.ETag != "" {
		w.Header().Set("ETag", response.ETag)
		w.Header().Set("Cache-Control", "private, no-cache")
	}
	if response.NotModified {
		w.WriteHeader(304)
		return
	}
	if q.route.operation != opOperatorTimeline {
		result = response.Body
	}
	writeJSONValue(w, status, result, true)
}

func (h *Handler) serveMusterStatus(w http.ResponseWriter, r *http.Request, q *requestState) {
	result, err := h.App.MusterStatus(q.ctx, h.Home)
	h.respond(w, 200, result, err)
}

func (h *Handler) serveFlags(w http.ResponseWriter, r *http.Request, q *requestState) {
	result, err := h.App.Flags(q.ctx)
	h.respond(w, 200, result, err)
}

func (h *Handler) serveWriterCurrent(w http.ResponseWriter, r *http.Request, q *requestState) {
	result, err := h.App.CurrentWriter(q.ctx, q.writerID)
	h.respond(w, 200, result, err)
}

func (h *Handler) serveFlagMutation(w http.ResponseWriter, r *http.Request, q *requestState) {
	var result any
	var err error
	status := 200
	if !q.bodyPresent {
		writeJSON(w, 422, map[string]any{"detail": missingBody()})
		return
	}
	if q.route.operation == opFlagWrite {
		request, issues := contract.ValidateFlag(q.input, q.keys)
		if len(issues) > 0 {
			writeJSON(w, 422, map[string]any{"detail": issues})
			return
		}
		result, err = h.App.RaiseFlag(q.ctx, request)
	} else {
		request, issues := contract.ValidateFlagKey(q.input, q.keys)
		if len(issues) > 0 {
			writeJSON(w, 422, map[string]any{"detail": issues})
			return
		}
		result, err = h.App.ClearFlag(q.ctx, request)
	}
	h.respond(w, status, result, err)
}

func (h *Handler) serveScout(w http.ResponseWriter, r *http.Request, q *requestState) {
	var result any
	var err error
	status := 200
	if q.route.body && !q.bodyPresent {
		writeJSON(w, 422, map[string]any{"detail": append(q.pathIssues, missingBody()...)})
		return
	}
	var issues []contract.Issue
	switch q.route.operation {
	case opScoutStatus:
		result, err = h.App.ScoutStatus(q.ctx)
	case opScoutSettings:
		request, bodyIssues := contract.ValidateScoutSettings(q.input, q.keys)
		issues = append(q.pathIssues, bodyIssues...)
		if len(issues) == 0 {
			result, err = h.App.ConfigureScout(q.ctx, request)
		}
	case opScoutClaim:
		request, bodyIssues := contract.ValidateScoutClaim(q.input, q.keys)
		issues = append(q.pathIssues, bodyIssues...)
		if len(issues) == 0 {
			result, err = h.App.ClaimScout(q.ctx, request)
		}
	case opScoutPage:
		request, bodyIssues := contract.ValidateScoutPage(q.input, q.keys)
		issues = append(q.pathIssues, bodyIssues...)
		if len(issues) == 0 {
			result, err = h.App.ScoutPage(q.ctx, q.route.passID, request)
		}
	case opScoutFailure:
		request, bodyIssues := contract.ValidateScoutFailure(q.input, q.keys)
		issues = append(q.pathIssues, bodyIssues...)
		if len(issues) == 0 {
			result, err = h.App.ScoutFailure(q.ctx, q.route.passID, request)
		}
	case opScoutDecision:
		request, bodyIssues := contract.ValidateScoutDecision(q.input, q.keys)
		issues = append(q.pathIssues, bodyIssues...)
		if len(issues) == 0 {
			result, err = h.App.ScoutDecision(q.ctx, q.route.repositoryID, q.pathNumber, request)
		}
	}
	if len(issues) > 0 {
		writeJSON(w, 422, map[string]any{"detail": issues})
		return
	}
	h.respond(w, status, result, err)
}

func (h *Handler) serveUsage(w http.ResponseWriter, r *http.Request, q *requestState) {
	var result any
	var err error
	status := 200
	if !q.bodyPresent {
		writeJSON(w, 422, map[string]any{"detail": append(q.pathIssues, missingBody()...)})
		return
	}
	var issues []contract.Issue
	if q.route.operation == opWriterUsage {
		request, bodyIssues := contract.ValidateUsageSnapshot(q.input, q.keys, h.App.Clock())
		issues = append(q.pathIssues, bodyIssues...)
		if len(issues) == 0 {
			result, err = h.App.OwnUsage(q.ctx, q.writerID, request)
		}
	} else {
		request, bodyIssues := contract.ValidateCaptainUsage(q.input, q.keys, h.App.Clock())
		issues = append(q.pathIssues, bodyIssues...)
		if len(issues) == 0 {
			result, err = h.App.CaptainUsage(q.ctx, q.route.itemID, q.route.executionID, q.route.writerID, request)
		}
	}
	if len(issues) > 0 {
		writeJSON(w, 422, map[string]any{"detail": issues})
		return
	}
	h.respond(w, status, result, err)
}

func (h *Handler) serveReconciliation(w http.ResponseWriter, r *http.Request, q *requestState) {
	var result any
	var err error
	status := 200
	if q.route.body && !q.bodyPresent && q.route.operation != opReconcilerHeartbeat {
		writeJSON(w, 422, map[string]any{"detail": append(q.pathIssues, missingBody()...)})
		return
	}
	var issues []contract.Issue
	switch q.route.operation {
	case opGithubObservation:
		request, bodyIssues := contract.ValidateObservation(q.input, q.keys)
		issues = append(q.pathIssues, bodyIssues...)
		if len(issues) == 0 {
			result, err = h.App.RecordObservation(q.ctx, q.route.itemID, q.route.executionID, request)
		}
	case opLocalChecks:
		request, bodyIssues := contract.ValidateLocalChecks(q.input, q.keys)
		issues = append(q.pathIssues, bodyIssues...)
		if len(issues) == 0 {
			result, err = h.App.RecordLocalChecks(q.ctx, q.route.itemID, q.route.executionID, request)
		}
	case opReconcilerHeartbeat:
		var request *contract.ReconcilerHeartbeat
		issues = q.pathIssues
		if q.input != nil {
			body, bodyIssues := contract.ValidateReconcilerHeartbeat(q.input, q.keys)
			request = &body
			issues = append(issues, bodyIssues...)
		}
		if len(issues) == 0 {
			result, err = h.App.ReconcilerHeartbeat(q.ctx, q.route.runnerID, request)
		}
	case opReconcilerRetire:
		result, err = h.App.RetireReconciler(q.ctx, q.route.runnerID)
	}
	if len(issues) > 0 {
		writeJSON(w, 422, map[string]any{"detail": issues})
		return
	}
	h.respond(w, status, result, err)
}

func (h *Handler) serveArchiveRead(w http.ResponseWriter, r *http.Request, q *requestState) {
	var result any
	var err error
	status := 200
	sessionID, issues := contract.ValidateArchiveQuery(r.URL.Query())
	if len(issues) > 0 {
		writeJSON(w, 422, map[string]any{"detail": issues})
		return
	}
	result, err = h.App.ArchiveStatus(q.ctx, sessionID)
	h.respond(w, status, result, err)
}

func (h *Handler) serveArchiveWrite(w http.ResponseWriter, r *http.Request, q *requestState) {
	var result any
	var err error
	status := 200
	if !q.bodyPresent {
		writeJSON(w, 422, map[string]any{"detail": missingBody()})
		return
	}
	request, issues := contract.ValidateArchive(q.input, q.keys)
	if len(issues) > 0 {
		writeJSON(w, 422, map[string]any{"detail": issues})
		return
	}
	result, err = h.App.RecordArchive(q.ctx, request)
	h.respond(w, status, result, err)
}

func (h *Handler) serveCaptains(w http.ResponseWriter, r *http.Request, q *requestState) {
	var result any
	var err error
	status := 200
	key, keyIssues := requestKey(r)
	if q.route.operation == opCaptainRegister || q.route.operation == opCaptainAction {
		q.pathIssues = append(q.pathIssues, keyIssues...)
	} else {
		key = nil
	}
	if q.route.body && !q.bodyPresent {
		writeJSON(w, 422, map[string]any{"detail": append(q.pathIssues, missingBody()...)})
		return
	}
	var issues []contract.Issue
	result, issues, err = h.captainOperation(q.ctx, r, q.route, q.input, q.keys, q.pathIssues, key)
	if len(issues) > 0 {
		writeJSON(w, 422, map[string]any{"detail": issues})
		return
	}
	if q.route.operation == opCaptainRegister || q.route.operation == opCaptainAction {
		status = 201
	}
	h.respond(w, status, result, err)
}

func (h *Handler) serveWriter(w http.ResponseWriter, r *http.Request, q *requestState) {
	var result any
	var err error
	status := 200
	var key *string
	if q.route.operation == opWriterResult || q.route.operation == opWriterStop {
		var keyIssues []contract.Issue
		key, keyIssues = requestKey(r)
		q.pathIssues = append(q.pathIssues, keyIssues...)
	}
	if q.route.body && !q.bodyPresent {
		writeJSON(w, 422, map[string]any{"detail": append(q.pathIssues, missingBody()...)})
		return
	}
	var issues []contract.Issue
	result, issues, err = h.writerOperation(q.ctx, q.route, q.writerID, q.input, q.keys, q.pathIssues, key)
	if len(issues) > 0 {
		writeJSON(w, 422, map[string]any{"detail": issues})
		return
	}
	h.respond(w, status, result, err)
}

func (h *Handler) serveReply(w http.ResponseWriter, r *http.Request, q *requestState) {
	var result any
	var err error
	status := 200
	key, issues := requestKey(r)
	issues = append(q.pathIssues, issues...)
	if key == nil {
		issues = append(issues, contract.Issue{Loc: []any{"header", "Idempotency-Key"}, Msg: "Field required"})
	}
	if !q.bodyPresent {
		writeJSON(w, 422, map[string]any{"detail": append(issues, missingBody()...)})
		return
	}
	request, bodyIssues := contract.ValidateReply(q.input, q.keys)
	issues = append(issues, bodyIssues...)
	if len(issues) > 0 {
		writeJSON(w, 422, map[string]any{"detail": issues})
		return
	}
	result, err = h.App.Reply(q.ctx, q.route.itemID, q.route.executionID, request, *key)
	h.respond(w, status, result, err)
}

func (h *Handler) serveSteering(w http.ResponseWriter, r *http.Request, q *requestState) {
	var result any
	var err error
	status := 200
	key, issues := requestKey(r)
	issues = append(q.pathIssues, issues...)
	if !q.bodyPresent {
		writeJSON(w, 422, map[string]any{"detail": append(issues, missingBody()...)})
		return
	}
	if q.route.operation == opSteer {
		request, bodyIssues := contract.ValidateSteering(q.input, q.keys)
		issues = append(issues, bodyIssues...)
		if len(issues) > 0 {
			writeJSON(w, 422, map[string]any{"detail": issues})
			return
		}
		result, err = h.App.Steer(q.ctx, q.route.itemID, q.route.executionID, request, key)
	} else {
		request, bodyIssues := contract.ValidateRerun(q.input, q.keys)
		issues = append(issues, bodyIssues...)
		if len(issues) > 0 {
			writeJSON(w, 422, map[string]any{"detail": issues})
			return
		}
		result, err = h.App.Rerun(q.ctx, q.route.itemID, q.route.executionID, request, key)
	}
	h.respond(w, status, result, err)
}

func (h *Handler) serveRole(w http.ResponseWriter, r *http.Request, q *requestState) {
	writeJSON(w, 200, map[string]any{"role": q.role})
}

func (h *Handler) serveRepositories(w http.ResponseWriter, r *http.Request, q *requestState) {
	result, err := h.App.Repositories(q.ctx)
	h.respond(w, 200, result, err)
}

func (h *Handler) serveDispatch(w http.ResponseWriter, r *http.Request, q *requestState) {
	result, err := h.App.DispatchSettings(q.ctx)
	h.respond(w, 200, result, err)
}

func (h *Handler) serveSetDispatch(w http.ResponseWriter, r *http.Request, q *requestState) {
	var result any
	var err error
	status := 200
	if !q.bodyPresent {
		writeJSON(w, 422, map[string]any{"detail": missingBody()})
		return
	}
	request, issues := contract.ValidateDispatchUpdate(q.input, q.keys)
	if len(issues) > 0 {
		writeJSON(w, 422, map[string]any{"detail": issues})
		return
	}
	result, err = h.App.SetDispatch(q.ctx, request)
	h.respond(w, status, result, err)
}

func (h *Handler) serveInbox(w http.ResponseWriter, r *http.Request, q *requestState) {
	var result any
	var err error
	status := 200
	query, issues := contract.ValidateInboxQuery(r.URL.Query())
	if len(issues) > 0 {
		writeJSON(w, 422, map[string]any{"detail": issues})
		return
	}
	result, err = h.App.Inbox(q.ctx, query)
	h.respond(w, status, result, err)
}

func (h *Handler) serveRegister(w http.ResponseWriter, r *http.Request, q *requestState) {
	var result any
	var err error
	status := 200
	if !q.bodyPresent {
		writeJSON(w, 422, map[string]any{"detail": missingBody()})
		return
	}
	request, issues := contract.ValidateRegistration(q.input, q.keys)
	if len(issues) > 0 {
		writeJSON(w, 422, map[string]any{"detail": issues})
		return
	}
	result, err = h.App.Register(q.ctx, request)
	h.respond(w, status, result, err)
}

func (h *Handler) serveInspect(w http.ResponseWriter, r *http.Request, q *requestState) {
	result, err := h.App.Inspect(q.ctx, q.route.itemID)
	h.respond(w, 200, result, err)
}

func (h *Handler) serveTitle(w http.ResponseWriter, r *http.Request, q *requestState) {
	result, err := h.App.DisplayTitle(q.ctx, q.route.itemID)
	h.respond(w, 200, result, err)
}

func (h *Handler) serveHistory(w http.ResponseWriter, r *http.Request, q *requestState) {
	result, err := h.App.History(q.ctx, q.route.itemID)
	h.respond(w, 200, result, err)
}

func (h *Handler) serveTimeline(w http.ResponseWriter, r *http.Request, q *requestState) {
	result, err := h.App.Timeline(q.ctx, q.route.itemID, q.route.executionID)
	h.respond(w, 200, result, err)
}

func (h *Handler) serveDraft(w http.ResponseWriter, r *http.Request, q *requestState) {
	markdown, err := h.App.Draft(q.ctx, q.route.itemID, q.route.executionID, q.route.revision)
	if err != nil {
		h.failure(w, err)
		return
	}
	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	w.WriteHeader(200)
	w.Write([]byte(markdown))
}

func (h *Handler) serveReport(w http.ResponseWriter, r *http.Request, q *requestState) {
	markdown, err := h.App.Report(q.ctx, q.route.itemID, q.route.executionID)
	if err != nil {
		h.failure(w, err)
		return
	}
	w.Header().Set("Content-Type", "text/plain; charset=utf-8")
	w.WriteHeader(200)
	w.Write([]byte(markdown))
}

func (h *Handler) serveSetTitle(w http.ResponseWriter, r *http.Request, q *requestState) {
	var result any
	var err error
	status := 200
	issues := q.pathIssues
	key := ""
	if values, exists := r.Header["Idempotency-Key"]; exists && len(values) > 0 {
		key = headerText(values[0])
		issues = append(issues, contract.ValidateKey(key)...)
	} else {
		issues = append(issues, contract.Issue{Loc: []any{"header", "Idempotency-Key"}, Msg: "Field required"})
	}
	if !q.bodyPresent {
		writeJSON(w, 422, map[string]any{"detail": append(issues, missingBody()...)})
		return
	}
	request, bodyIssues := contract.ValidateDisplayTitle(q.input, q.keys)
	issues = append(issues, bodyIssues...)
	if len(issues) > 0 {
		writeJSON(w, 422, map[string]any{"detail": issues})
		return
	}
	result, err = h.App.SetDisplayTitle(q.ctx, q.route.itemID, request, key)
	h.respond(w, status, result, err)
}

func (h *Handler) serveStopRequest(w http.ResponseWriter, r *http.Request, q *requestState) {
	var result any
	var err error
	status := 200
	key, issues := requestKey(r)
	issues = append(q.pathIssues, issues...)
	if !q.bodyPresent {
		writeJSON(w, 422, map[string]any{"detail": append(issues, missingBody()...)})
		return
	}
	if q.route.operation == opRetryFinalization {
		request, bodyIssues := contract.ValidateVersioned(q.input, q.keys)
		issues = append(issues, bodyIssues...)
		if len(issues) > 0 {
			writeJSON(w, 422, map[string]any{"detail": issues})
			return
		}
		result, err = h.App.RetryFinalization(q.ctx, q.route.itemID, q.route.executionID, request, key)
	} else {
		request, bodyIssues := contract.ValidateCancellation(q.input, q.keys)
		issues = append(issues, bodyIssues...)
		if len(issues) > 0 {
			writeJSON(w, 422, map[string]any{"detail": issues})
			return
		}
		event := application.Cancel
		if q.route.operation == opCloseFailure {
			event = application.CloseFailure
		}
		result, err = h.App.RequestStop(q.ctx, q.route.itemID, q.route.executionID, request, event, key)
	}
	h.respond(w, status, result, err)
}

func (h *Handler) serveIntake(w http.ResponseWriter, r *http.Request, q *requestState) {
	var result any
	var err error
	status := 200
	var issues []contract.Issue
	var key *string
	if values, exists := r.Header["Idempotency-Key"]; exists && len(values) > 0 {
		value := headerText(values[0])
		key = &value
		issues = contract.ValidateKey(value)
	}
	if !q.bodyPresent {
		writeJSON(w, 422, map[string]any{"detail": append(issues, missingBody()...)})
		return
	}
	request, bodyIssues := contract.ValidateIntake(q.input, q.keys)
	issues = append(issues, bodyIssues...)
	if len(issues) > 0 {
		writeJSON(w, 422, map[string]any{"detail": issues})
		return
	}
	if h.dispatch != nil {
		h.dispatch()
	}
	result, err = h.App.Intake(q.ctx, request, key)
	status = 201
	h.respond(w, status, result, err)
}

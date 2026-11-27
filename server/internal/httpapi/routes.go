package httpapi

import (
	"context"
	"encoding/json"
	"net/http"
	"strings"

	"github.com/caltseng/phalanx/server/internal/contract"
)

type operation uint8

const (
	opUnknown operation = iota
	opArchiveRead
	opArchiveWrite
	opCancel
	opCaptain
	opCaptainAction
	opCaptainCurrent
	opCaptainHeartbeat
	opCaptainRegister
	opCaptainReinforce
	opCaptainUsage
	opCaptains
	opCloseFailure
	opDispatch
	opDocumentation
	opDraft
	opFlagClear
	opFlagWrite
	opFlags
	opGithubObservation
	opHealth
	opHistory
	opInbox
	opInspect
	opIntake
	opLocalChecks
	opMusterStatus
	opOperatorAsset
	opOperatorGroup
	opOperatorItem
	opOperatorList
	opOperatorOverview
	opOperatorShell
	opOperatorTimeline
	opOwner
	opReconcilerHeartbeat
	opReconcilerRetire
	opRegister
	opReply
	opReport
	opRepositories
	opRerun
	opRetryFinalization
	opRunner
	opScoutClaim
	opScoutDecision
	opScoutFailure
	opScoutPage
	opScoutSettings
	opScoutStatus
	opSetDispatch
	opSetTitle
	opSteer
	opTimeline
	opTitle
	opWriterAttempt
	opWriterCapacity
	opWriterCurrent
	opWriterProgress
	opWriterRegister
	opWriterResult
	opWriterStop
	opWriterUpdate
	opWriterUsage
	opWriterWithdraw
)

type routeHandler func(*Handler, http.ResponseWriter, *http.Request, *requestState)

type routeSpec struct {
	pattern   string
	method    string
	allow     string
	operation operation
	policy    accessPolicy
	origin    originRule
	body      bool
	serve     routeHandler
}

type route struct {
	*routeSpec
	itemID, executionID, captainID, runnerID, writerID, passID string
	repositoryID, number, label, revision                      string
}

type requestState struct {
	ctx                   context.Context
	route                 route
	input                 any
	keys                  []string
	bodyPresent, unpaired bool
	role, writerID        string
	pathIssues            []contract.Issue
	pathNumber            json.Number
}

// Ordering selects the legacy method fallback and Allow bytes, not a union.
var routes = []routeSpec{
	{"/health", "GET", "GET", opHealth, public, noOrigin, false, (*Handler).serveHealth},
	{"/v1/owner/status", "GET", "GET", opOwner, ownerOnly, noOrigin, false, (*Handler).serveRole},
	{"/v1/runner/status", "GET", "GET", opRunner, runnerOnly, noOrigin, false, (*Handler).serveRole},
	{"/v1/owner/inbox", "GET", "GET", opInbox, ownerOnly, noOrigin, false, (*Handler).serveInbox},
	{"/v1/writer/attempt", "POST", "POST", opWriterAttempt, writerOnly, noOrigin, false, (*Handler).serveWriter},
	{"/v1/writer", "GET", "GET", opWriterCurrent, writerOnly, noOrigin, false, (*Handler).serveWriterCurrent},
	{"/v1/writer/progress", "POST", "POST", opWriterProgress, writerOnly, noOrigin, true, (*Handler).serveWriter},
	{"/v1/writer/capacity", "POST", "POST", opWriterCapacity, writerOnly, noOrigin, true, (*Handler).serveWriter},
	{"/v1/writer/usage", "POST", "POST", opWriterUsage, writerOnly, noOrigin, true, (*Handler).serveUsage},
	{"/v1/issue-scout", "GET", "GET", opScoutStatus, boardReader, noOrigin, false, (*Handler).serveScout},
	{"/v1/issue-scout/settings", "PUT", "PUT", opScoutSettings, ownerOnly, browserOrigin, true, (*Handler).serveScout},
	{"/v1/issue-scout/claim", "POST", "POST", opScoutClaim, ownerRunner, noOrigin, true, (*Handler).serveScout},
	{"/v1/flags", "PUT", "PUT", opFlagWrite, ownerRunner, noOrigin, true, (*Handler).serveFlagMutation},
	{"/v1/flags", "GET", "PUT", opFlags, boardReader, noOrigin, false, (*Handler).serveFlags},
	{"/v1/flags/clear", "POST", "POST", opFlagClear, ownerRunner, noOrigin, true, (*Handler).serveFlagMutation},
	{"/v1/operator/executions", "GET", "GET", opOperatorList, boardReader, noOrigin, false, (*Handler).serveOperatorRead},
	{"/v1/operator/overview", "GET", "GET", opOperatorOverview, boardReader, noOrigin, false, (*Handler).serveOperatorRead},
	{"/v1/muster/status", "GET", "GET", opMusterStatus, ownerRunner, noOrigin, false, (*Handler).serveMusterStatus},
	{"/operator/api/repositories", "GET", "GET", opRepositories, boardOwner, localOrigin, false, (*Handler).serveRepositories},
	{"/operator/api/items", "POST", "POST", opIntake, boardOwner, browserAndLocalOrigin, true, (*Handler).serveIntake},
	{"/operator/api/dispatch/settings", "GET", "GET", opDispatch, boardOwner, localOrigin, false, (*Handler).serveDispatch},
	{"/operator/api/dispatch/settings", "PUT", "GET", opSetDispatch, boardOwner, browserAndLocalOrigin, true, (*Handler).serveSetDispatch},
	{"/operator/", "GET", "GET", opOperatorShell, public, noOrigin, false, (*Handler).serveOperatorShell},
	{"/operator/queue", "GET", "GET", opOperatorShell, public, noOrigin, false, (*Handler).serveOperatorShell},
	{"/operator/history", "GET", "GET", opOperatorShell, public, noOrigin, false, (*Handler).serveOperatorShell},
	{"/openapi.json", "GET", "GET, HEAD", opDocumentation, public, noOrigin, false, (*Handler).serveDocumentationRoute},
	{"/openapi.json", "HEAD", "GET, HEAD", opDocumentation, public, noOrigin, false, (*Handler).serveDocumentationRoute},
	{"/docs", "GET", "GET, HEAD", opDocumentation, public, noOrigin, false, (*Handler).serveDocumentationRoute},
	{"/docs", "HEAD", "GET, HEAD", opDocumentation, public, noOrigin, false, (*Handler).serveDocumentationRoute},
	{"/docs/oauth2-redirect", "GET", "GET, HEAD", opDocumentation, public, noOrigin, false, (*Handler).serveDocumentationRoute},
	{"/docs/oauth2-redirect", "HEAD", "GET, HEAD", opDocumentation, public, noOrigin, false, (*Handler).serveDocumentationRoute},
	{"/redoc", "GET", "GET, HEAD", opDocumentation, public, noOrigin, false, (*Handler).serveDocumentationRoute},
	{"/redoc", "HEAD", "GET, HEAD", opDocumentation, public, noOrigin, false, (*Handler).serveDocumentationRoute},
	{"/v1/dispatch/settings", "GET", "GET", opDispatch, boardReader, noOrigin, false, (*Handler).serveDispatch},
	{"/v1/dispatch/settings", "PUT", "GET", opSetDispatch, ownerOnly, browserOrigin, true, (*Handler).serveSetDispatch},
	{"/v1/captains", "GET", "POST", opCaptains, boardReader, noOrigin, false, (*Handler).serveCaptains},
	{"/v1/captains", "POST", "POST", opCaptainRegister, ownerRunner, noOrigin, true, (*Handler).serveCaptains},
	{"/v1/captains/current", "GET", "GET", opCaptainCurrent, boardReader, noOrigin, false, (*Handler).serveCaptains},
	{"/v1/session-archives", "GET", "GET", opArchiveRead, ownerRunner, noOrigin, false, (*Handler).serveArchiveRead},
	{"/v1/session-archives", "PUT", "GET", opArchiveWrite, ownerRunner, noOrigin, true, (*Handler).serveArchiveWrite},
	{"/v1/repositories", "GET", "POST", opRepositories, authenticated, noOrigin, false, (*Handler).serveRepositories},
	{"/v1/repositories", "POST", "POST", opRegister, ownerOnly, noOrigin, true, (*Handler).serveRegister},
	{"/v1/items", "POST", "POST", opIntake, ownerOnly, browserOrigin, true, (*Handler).serveIntake},
	{"/operator/assets/{asset...}", "*", "", opOperatorAsset, public, noOrigin, false, (*Handler).serveOperatorAsset},
	{"/operator/items/{itemID}", "GET", "GET", opOperatorShell, public, noOrigin, false, (*Handler).serveOperatorShell},
	{"/operator/executions/{executionID}", "GET", "GET", opOperatorShell, public, noOrigin, false, (*Handler).serveOperatorShell},
	{"/operator/api/issue-scout/{repositoryID}/{number}/decision", "POST", "POST", opScoutDecision, boardOwner, browserAndLocalOrigin, true, (*Handler).serveScout},
	{"/v1/operator/items/{itemID}", "GET", "GET", opOperatorItem, boardReader, noOrigin, false, (*Handler).serveOperatorRead},
	{"/v1/operator/groups/{label}", "GET", "GET", opOperatorGroup, boardReader, noOrigin, false, (*Handler).serveOperatorRead},
	{"/v1/operator/executions/{executionID}/timeline", "GET", "GET", opOperatorTimeline, boardReader, noOrigin, false, (*Handler).serveOperatorRead},
	{"/v1/issue-scout/passes/{passID}/page", "POST", "POST", opScoutPage, ownerRunner, noOrigin, true, (*Handler).serveScout},
	{"/v1/issue-scout/passes/{passID}/failure", "POST", "POST", opScoutFailure, ownerRunner, noOrigin, true, (*Handler).serveScout},
	{"/v1/issue-scout/issues/{repositoryID}/{number}/decision", "POST", "POST", opScoutDecision, ownerOnly, browserOrigin, true, (*Handler).serveScout},
	{"/v1/items/{itemID}", "GET", "GET", opInspect, authenticated, noOrigin, false, (*Handler).serveInspect},
	{"/v1/items/{itemID}/title", "GET", "GET", opTitle, ownerOnly, noOrigin, false, (*Handler).serveTitle},
	{"/v1/items/{itemID}/title", "PUT", "GET", opSetTitle, ownerOnly, browserOrigin, true, (*Handler).serveSetTitle},
	{"/v1/items/{itemID}/executions", "GET", "GET", opHistory, authenticated, noOrigin, false, (*Handler).serveHistory},
	{"/v1/items/{itemID}/captain-actions", "POST", "POST", opCaptainAction, ownerRunner, noOrigin, true, (*Handler).serveCaptains},
	{"/v1/items/{itemID}/executions/{executionID}", "PATCH", "PATCH", opSteer, ownerOnly, noOrigin, true, (*Handler).serveSteering},
	{"/v1/items/{itemID}/executions/{executionID}/timeline", "GET", "GET", opTimeline, authenticated, noOrigin, false, (*Handler).serveTimeline},
	{"/v1/items/{itemID}/executions/{executionID}/report", "GET", "GET", opReport, boardReader, noOrigin, false, (*Handler).serveReport},
	{"/v1/items/{itemID}/executions/{executionID}/github-observation", "POST", "POST", opGithubObservation, runnerOnly, noOrigin, true, (*Handler).serveReconciliation},
	{"/v1/items/{itemID}/executions/{executionID}/local-checks", "POST", "POST", opLocalChecks, runnerOnly, noOrigin, true, (*Handler).serveReconciliation},
	{"/v1/items/{itemID}/executions/{executionID}/cancel", "POST", "POST", opCancel, ownerOnly, noOrigin, true, (*Handler).serveStopRequest},
	{"/v1/items/{itemID}/executions/{executionID}/close-failure", "POST", "POST", opCloseFailure, ownerOnly, noOrigin, true, (*Handler).serveStopRequest},
	{"/v1/items/{itemID}/executions/{executionID}/retry-finalization", "POST", "POST", opRetryFinalization, ownerOnly, noOrigin, true, (*Handler).serveStopRequest},
	{"/v1/items/{itemID}/executions/{executionID}/writer", "POST", "POST", opWriterRegister, ownerRunner, noOrigin, true, (*Handler).serveWriter},
	{"/v1/items/{itemID}/executions/{executionID}/writer", "PATCH", "POST", opWriterUpdate, ownerRunner, noOrigin, true, (*Handler).serveWriter},
	{"/v1/items/{itemID}/executions/{executionID}/result", "POST", "POST", opWriterResult, writerOnly, noOrigin, true, (*Handler).serveWriter},
	{"/v1/items/{itemID}/executions/{executionID}/stop-confirmed", "POST", "POST", opWriterStop, writerOnly, noOrigin, true, (*Handler).serveWriter},
	{"/v1/items/{itemID}/executions/{executionID}/reply", "POST", "POST", opReply, ownerOnly, noOrigin, true, (*Handler).serveReply},
	{"/v1/items/{itemID}/executions/{executionID}/rerun", "POST", "POST", opRerun, ownerOnly, noOrigin, true, (*Handler).serveSteering},
	{"/v1/items/{itemID}/executions/{executionID}/writer/withdraw", "POST", "POST", opWriterWithdraw, ownerRunner, noOrigin, true, (*Handler).serveWriter},
	{"/v1/items/{itemID}/executions/{executionID}/drafts/{revision}", "GET", "GET", opDraft, authenticated, noOrigin, false, (*Handler).serveDraft},
	{"/v1/items/{itemID}/executions/{executionID}/writers/{writerID}/usage", "POST", "POST", opCaptainUsage, ownerRunner, noOrigin, true, (*Handler).serveUsage},
	{"/v1/reconcilers/{runnerID}", "DELETE", "DELETE", opReconcilerRetire, ownerRunner, noOrigin, false, (*Handler).serveReconciliation},
	{"/v1/reconcilers/{runnerID}/heartbeat", "POST", "POST", opReconcilerHeartbeat, runnerOnly, noOrigin, true, (*Handler).serveReconciliation},
	{"/v1/captains/{captainID}", "GET", "PUT", opCaptain, boardReader, noOrigin, false, (*Handler).serveCaptains},
	{"/v1/captains/{captainID}", "PUT", "PUT", opCaptainHeartbeat, ownerRunner, noOrigin, true, (*Handler).serveCaptains},
	{"/v1/captains/{captainID}/reinforcement", "PUT", "PUT", opCaptainReinforce, ownerRunner, noOrigin, true, (*Handler).serveCaptains},
}

func match(path, method string) (route, string) {
	var fallback route
	for i := range routes {
		spec := &routes[i]
		// Once a path matches, only its other method variants participate.
		if fallback.routeSpec != nil && spec.pattern != fallback.pattern {
			continue
		}
		target, ok := matchPattern(spec, path)
		if !ok {
			continue
		}
		if fallback.routeSpec == nil {
			fallback = target
		}
		if spec.method == method || spec.method == "*" {
			return target, spec.allow
		}
	}
	if fallback.routeSpec != nil {
		return fallback, fallback.allow
	}
	return route{}, ""
}

func matchPattern(spec *routeSpec, path string) (route, bool) {
	target := route{routeSpec: spec}
	if !strings.Contains(spec.pattern, "{") {
		return target, path == spec.pattern
	}
	if strings.HasSuffix(spec.pattern, "{asset...}") {
		return target, strings.HasPrefix(path, strings.TrimSuffix(spec.pattern, "{asset...}"))
	}
	pattern, parts := strings.Split(spec.pattern, "/"), strings.Split(path, "/")
	if len(pattern) != len(parts) {
		return route{}, false
	}
	for i, segment := range pattern {
		if !strings.HasPrefix(segment, "{") {
			if segment != parts[i] {
				return route{}, false
			}
			continue
		}
		if parts[i] == "" {
			return route{}, false
		}
		switch segment {
		case "{itemID}":
			target.itemID = parts[i]
		case "{executionID}":
			target.executionID = parts[i]
		case "{captainID}":
			target.captainID = parts[i]
		case "{runnerID}":
			target.runnerID = parts[i]
		case "{writerID}":
			target.writerID = parts[i]
		case "{passID}":
			target.passID = parts[i]
		case "{repositoryID}":
			target.repositoryID = parts[i]
		case "{number}":
			target.number = parts[i]
		case "{label}":
			target.label = parts[i]
		case "{revision}":
			target.revision = parts[i]
		}
	}
	return target, true
}

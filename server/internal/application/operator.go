package application

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"math/big"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/caltseng/phalanx/server/internal/contract"
	"github.com/caltseng/phalanx/server/internal/sqlite"
)

type ConditionalRead struct {
	Path, Query, IfNoneMatch string
}

type OperatorResponse struct {
	Body        json.RawMessage
	ETag        string
	NotModified bool
}

type operatorContext struct {
	Pickup   pickupContext
	Sessions map[sessionKey]string
}

func operatorSnapshot(tx *sqlite.Tx, now time.Time) (operatorContext, error) {
	var result operatorContext
	var err error
	result.Pickup, err = pickupSnapshot(tx, now)
	if err != nil {
		return result, err
	}
	result.Sessions, err = sessionReportsSnapshot(tx)
	return result, err
}

func nullableTextEqual(first, second *string) bool {
	return first == nil && second == nil || first != nil && second != nil && *first == *second
}

func boolValue(value *bool) bool { return value != nil && *value }

func automaticISO(value time.Time) string {
	layout := "2006-01-02T15:04:05"
	if value.Nanosecond() != 0 {
		layout += ".000000"
	}
	return value.Format(layout + "-07:00")
}

func operatorWait(row sqlite.OperatorRow, state State, now time.Time, current operatorContext) (*Wait, *string, *PickupDecision, error) {
	since := row.WaitSince
	if row.Status == "queued" && (row.WriterID == nil || row.WriterEndedAt != nil) {
		decision, err := pickupDecision(sqlite.QueueProjection{ID: row.ID, ItemID: row.ItemID, RepositoryID: row.RepositoryID,
			Stage: row.Stage, StateJSON: row.StateJSON, ReadySince: row.ReadySince, RetryAt: row.RetryAt,
			DependencyWait: row.DependencyWait}, now, current.Pickup)
		if err != nil {
			return nil, nil, nil, err
		}
		var wait *Wait
		if !decision.Eligible {
			wait = &Wait{*decision.Reason, decision.Trigger}
		}
		return wait, since, &decision, nil
	}
	if present(row.BuildingRequestedAt) && !boolValue(row.WriterBuilding) && row.BlockedCount == 0 &&
		(row.Status == "running" || row.Status == "queued" || row.Status == "waiting_for_external") {
		reason := "Building capacity available; retry acquisition"
		if wait := buildingReason(current.Pickup.Capacity, current.Pickup.Rework, textValue(row.WriterID)); wait != nil {
			reason = *wait
		}
		return &Wait{reason, "The same writer retries `work capacity building` before any rework"}, since, nil, nil
	}
	if row.Stage == "delivery" && row.Status == "waiting_for_external" {
		if boolValue(row.WriterBuilding) {
			return &Wait{"Writer is building or reviewing PR rework", "The writer commits reviewed work and releases Building for publication"}, since, nil, nil
		}
		if present(row.HandoffHead) && !nullableTextEqual(row.PRHeadSHA, row.HandoffHead) {
			return &Wait{"Awaiting publication handoff " + *row.HandoffHead,
				"Publish the reviewed head, review a superseding head, or stop and withdraw the writer"}, since, nil, nil
		}
	}
	if state.Wait != nil {
		return state.Wait, since, nil, nil
	}
	if row.BlockedCount > 0 {
		noun := "prerequisites"
		if row.BlockedCount == 1 {
			noun = "prerequisite"
		}
		return &Wait{fmt.Sprintf("Waiting for %d %s", row.BlockedCount, noun), "Prerequisite milestone"}, since, nil, nil
	}
	if row.Status == "queued" {
		if current.Pickup.Capacity.Paused {
			return &Wait{"Dispatch is paused", "The owner resumes dispatch"}, since, nil, nil
		}
		if row.RetryAt != nil && *row.RetryAt > now.UnixMicro()/1000 {
			at := time.UnixMilli(*row.RetryAt).UTC()
			if at.Year() < 1 || at.Year() > 9999 {
				return nil, nil, nil, errors.New("Retry time is outside datetime range")
			}
			return &Wait{"Retry scheduled for " + automaticISO(at), "The retry delay elapses"}, since, nil, nil
		}
		if hold := current.Pickup.Holds[row.ID]; hold != "" {
			return &Wait{hold, "A Captain launches it or the hold expires"}, since, nil, nil
		}
	}
	if row.Status == "running" && state.Stopping == nil && present(row.WriterID) &&
		row.WriterEndedAt == nil && row.WriterConfirmedAt == nil {
		if row.WriterNonce == nil || row.WriterCreatedAt == nil {
			return nil, nil, nil, errors.New("Unconfirmed writer has no nonce or creation time")
		}
		wait, err := launchWait(*row.WriterID, *row.WriterNonce, *row.WriterCreatedAt, now)
		return wait, row.WriterCreatedAt, nil, err
	}
	return nil, since, nil, nil
}

func textValue(value *string) string {
	if value == nil {
		return ""
	}
	return *value
}

func operatorPull(row sqlite.OperatorRow) any {
	if row.PRURL == nil {
		return nil
	}
	state := any(row.PRState)
	if boolValue(row.PRMerged) {
		state = "merged"
	}
	var ci *string
	if nullableTextEqual(row.PRCIHeadSHA, row.PRHeadSHA) {
		ci = row.PRCI
	}
	return map[string]any{"url": row.PRURL, "number": row.PRNumber, "state": state, "draft": row.PRDraft,
		"head_sha": row.PRHeadSHA, "head_ci": ci, "additions": row.PRAdditions, "deletions": row.PRDeletions}
}

func operatorSummary(row sqlite.OperatorRow, now time.Time, current operatorContext, allowMissingMode bool) (map[string]any, error) {
	value, _, err := contract.Decode([]byte(row.StateJSON))
	if err != nil {
		return nil, err
	}
	fields, ok := contract.ObjectValues(value)
	if !ok {
		return nil, errors.New("Stored execution state is not an object")
	}
	if _, present := fields["publication_mode"]; !present && !allowMissingMode {
		return nil, errors.New("Stored publication mode is missing outside the classified current-item case")
	}
	state, err := readState([]byte(row.StateJSON))
	if err != nil {
		return nil, err
	}
	wait, waitSince, decision, err := operatorWait(row, state, now, current)
	if err != nil {
		return nil, err
	}
	var outcome, output, revision, delivery any
	if state.FinalOutput != nil {
		if state.FinalOutput.Report != nil {
			output = map[string]any{"kind": "report", "url": state.FinalOutput.Report.Reference}
			outcome = "report_stored"
		} else {
			pr := state.FinalOutput.PR
			output, revision, outcome = map[string]any{"kind": "pr", "url": pr.URL}, pr.Revision, "ready_for_check_in"
			if pr.Draft {
				outcome = "draft_published"
			}
			if present(pr.MergeCommit) {
				outcome = "checked_in"
				delivery = map[string]any{"merged": true, "merge_commit": pr.MergeCommit,
					"merge_method": pr.MergeMethod, "merged_at": pr.MergedAt}
			}
		}
		if row.Status != "completed" {
			outcome = row.Status
		}
	}
	var next *string
	if output == nil && wait == nil {
		value := ""
		switch row.Status {
		case "queued":
			value = "Next to run: a Captain dispatches a writer"
		case "running":
			value = "A worker is running the " + row.Stage + " stage"
			if state.Stopping != nil {
				value = "Stopping the worker"
			}
		default:
			status := strings.ReplaceAll(row.Status, "_", " ")
			if status != "" {
				status = strings.ToUpper(status[:1]) + status[1:]
			}
			value = status + "; the " + row.Stage + " stage resumes when it clears"
		}
		next = &value
	}
	if row.Goal == nil {
		return nil, errors.New("Stored item goal is missing")
	}
	title := shortTitle(*row.Goal, 80)
	if present(row.DisplayTitle) {
		title = *row.DisplayTitle
	}
	priority := row.QueuePriority
	if priority < 0 {
		priority += 3
	}
	if priority < 0 || priority >= 3 {
		return nil, errors.New("Stored queue priority is outside display range")
	}
	var activeAge, waitAge *int64
	var stageStarted *string
	if present(row.ActiveStartedAt) {
		started, err := time.Parse(time.RFC3339Nano, *row.ActiveStartedAt)
		if err != nil {
			return nil, err
		}
		stop := now
		if output != nil {
			stop, err = time.Parse(time.RFC3339Nano, row.UpdatedAt)
			if err != nil {
				return nil, err
			}
		}
		age := max(int64(0), int64(elapsedSeconds(stop, started)))
		activeAge = &age
		stage := max(row.LastAdvancedAt, *row.ActiveStartedAt)
		stageStarted = &stage
	}
	if present(waitSince) {
		started, err := time.Parse(time.RFC3339Nano, *waitSince)
		if err != nil {
			return nil, err
		}
		age := max(int64(0), int64(elapsedSeconds(now, started)))
		waitAge = &age
	}
	var waitReason, trigger *string
	if wait != nil {
		waitReason, trigger = &wait.Reason, &wait.Trigger
	}
	var closed *string
	if output != nil {
		closed = &row.UpdatedAt
	}
	var deployment any
	if present(row.DeploymentJSON) {
		deployment, _, err = contract.Decode([]byte(*row.DeploymentJSON))
		if err != nil {
			return nil, err
		}
	}
	var writer any
	if row.WriterID != nil {
		state := row.WriterState
		if present(row.WriterEndedAt) {
			value := "ended"
			state = &value
		}
		availability := current.Sessions[recordedSessionKey(textValue(row.WriterSessionID), textValue(row.WriterRuntime))]
		if availability == "" {
			availability = "unverified"
		}
		writer = map[string]any{"id": row.WriterID, "state": state, "runtime": row.WriterRuntime, "ended_at": row.WriterEndedAt,
			"captain_id": row.WriterCaptainID, "session_id": row.WriterSessionID, "session_url": row.WriterSessionURL,
			"last_progress_at": row.WriterProgressAt, "session_link": sessionTarget(textValue(row.WriterSessionURL),
				textValue(row.WriterRuntime), textValue(row.WriterSessionID), availability, present(row.WriterSessionURL)),
			"progress_note": row.WriterProgressNote, "nonce": row.WriterNonce, "created_at": row.WriterCreatedAt,
			"confirmed_at": row.WriterConfirmedAt, "abandoned_at": row.WriterAbandonedAt, "building": boolValue(row.WriterBuilding),
			"building_requested_at": row.BuildingRequestedAt, "session_availability": availability}
	}
	return map[string]any{
		"id": row.ID, "item_id": row.ItemID, "version": row.Version, "title": title, "goal": row.Goal,
		"priority": []string{"low", "normal", "urgent"}[priority], "label": row.Label, "target": state.Target,
		"target_branch": row.TargetBranch, "publication_mode": state.PublicationMode, "repository_id": row.RepositoryID,
		"created_at": row.CreatedAt, "active_started_at": row.ActiveStartedAt, "active_age_seconds": activeAge,
		"stage_started_at": stageStarted, "stage": row.Stage, "status": row.Status, "revision_round": row.RevisionRound,
		"last_advanced_at": row.LastAdvancedAt, "last_progress_at": row.UpdatedAt, "wait_reason": waitReason, "pickup": decision,
		"blocked_prerequisite_count": row.BlockedCount, "resume_trigger": trigger, "next_action": next,
		"retry_at_ms": row.RetryAt, "stage_owner": row.StageOwner, "wait_since": waitSince, "wait_age_seconds": waitAge,
		"primary_pr_url": state.PrimaryPR, "pr": operatorPull(row), "duplicate_count": row.DuplicateCount, "closed_at": closed,
		"outcome": outcome, "output": output, "output_revision": revision, "delivery": delivery, "deployment": deployment, "writer": writer,
	}, nil
}

func scoutingRepr(view map[string]any) (reprDict, error) {
	settings, ok := view["settings"].(map[string]any)
	if !ok {
		return nil, errors.New("Scout status settings are not an object")
	}
	fields := reprDict{}
	for _, name := range []string{"id", "revision", "enabled", "cadence_minutes", "author_login", "author_id",
		"due_at", "changed_by", "updated_at", "repositories"} {
		fields = append(fields, reprEntry{name, settings[name]})
	}
	return reprDict{{"settings", fields}, {"state", view["state"]}, {"last_pass", view["last_pass"]},
		{"counts", view["counts"]}, {"resume_trigger", view["resume_trigger"]}}, nil
}

func operatorValidator(tx *sqlite.Tx, now time.Time, current operatorContext, request ConditionalRead, scouting map[string]any) (string, bool, error) {
	token, err := tx.OperatorChangeToken()
	if err != nil {
		return "", false, err
	}
	deadlines, err := tx.OperatorDeadlines(now.UnixMicro()/1000, pythonStamp(now.Add(-10*time.Minute)), pythonStamp(now.Add(-24*time.Hour)))
	if err != nil {
		return "", false, err
	}
	holds := make([]string, 0, len(current.Pickup.Holds))
	for id := range current.Pickup.Holds {
		holds = append(holds, id)
	}
	sort.Strings(holds)
	holdTuple := reprTuple{}
	for _, id := range holds {
		holdTuple = append(holdTuple, id)
	}
	type sessionEntry struct {
		Repr  string
		Value reprTuple
	}
	sessions := []sessionEntry{}
	for key, availability := range current.Sessions {
		var runtime any
		if key.runtime != "" {
			runtime = key.runtime
		}
		entry := reprTuple{reprTuple{runtime, key.id}, availability}
		text, err := pythonRepr(entry)
		if err != nil {
			return "", false, err
		}
		sessions = append(sessions, sessionEntry{text, entry})
	}
	sort.Slice(sessions, func(i, j int) bool { return sessions[i].Repr < sessions[j].Repr })
	sessionTuple := reprTuple{}
	for _, entry := range sessions {
		sessionTuple = append(sessionTuple, entry.Value)
	}
	captains := []any{}
	for _, captain := range current.Pickup.Captains {
		if body, ok := contract.ObjectValues(captain.RawPickup); ok {
			entry := reprDict{{"id", captain.ID}}
			for _, key := range contract.ObjectKeys(captain.RawPickup) {
				if key == "id" {
					entry[0].Value = body[key]
				} else {
					entry = append(entry, reprEntry{key, body[key]})
				}
			}
			captains = append(captains, entry)
			continue
		}
		scope := reprDict{}
		keys := make([]string, 0, len(captain.Repositories))
		for key := range captain.Repositories {
			keys = append(keys, key)
		}
		sort.Strings(keys)
		for _, key := range keys {
			var reason any
			if captain.Repositories[key] != nil {
				reason = *captain.Repositories[key]
			}
			scope = append(scope, reprEntry{key, reason})
		}
		captains = append(captains, reprDict{{"id", captain.ID}, {"repositories", scope}})
	}
	var scoutingValue any
	if scouting != nil {
		scoutingValue, err = scoutingRepr(scouting)
		if err != nil {
			return "", false, err
		}
	}
	values := []any{reprTuple(deadlines), holdTuple, sessionTuple, captains, scoutingValue}
	texts := []string{}
	for _, value := range values {
		text, err := pythonRepr(value)
		if err != nil {
			return "", false, err
		}
		texts = append(texts, text)
	}
	key := token + ":" + request.Path + "?" + request.Query + ":" + strings.Join(texts, ":")
	tag := `W/"` + contract.Digest(key) + `"`
	expected := strings.TrimPrefix(tag, "W/")
	for _, value := range strings.Split(request.IfNoneMatch, ",") {
		value = strings.TrimPrefix(strings.TrimFunc(value, pythonWhitespace), "W/")
		if value == "*" || value == expected {
			return tag, true, nil
		}
	}
	return tag, false, nil
}

func operatorFilters(request contract.OperatorQuery) (map[string]string, string, []any, error) {
	if request.Kind == "open" && (request.Outcome != nil || request.ClosedAfter != nil) {
		return nil, "", nil, &BadRequest{"outcome and closed_after apply only to closed executions"}
	}
	if request.Status != nil && Terminal(Status(*request.Status)) != (request.Kind == "closed") {
		return nil, "", nil, &BadRequest{"Status " + *request.Status + " is not a " + request.Kind + " status"}
	}
	filters := map[string]string{}
	names := []string{"repository", "label", "status", "stage", "outcome", "q"}
	values := []*string{request.Repository, request.Label, request.Status, request.Stage, request.Outcome, request.Query}
	for index, value := range values {
		if value != nil {
			filters[names[index]] = *value
		}
	}
	if request.ClosedAfter != nil {
		filters["closed_after"] = pythonStamp(request.ClosedAfter.UTC())
	}
	where, parameters := "", []any{}
	for _, entry := range [][2]string{{"repository", "item.repository_id"}, {"label", "item.label"},
		{"status", "execution.status"}, {"stage", "execution.stage"}, {"outcome", "execution.outcome"}} {
		if value, exists := filters[entry[0]]; exists {
			where += " AND " + entry[1] + "=?"
			parameters = append(parameters, value)
		}
	}
	if value, exists := filters["closed_after"]; exists {
		where += " AND execution.updated_at>?"
		parameters = append(parameters, value)
	}
	if query, exists := filters["q"]; exists {
		number := any(nil)
		digits := strings.TrimPrefix(query, "#")
		if len(digits) > 0 && len(digits) <= 18 {
			valid := true
			for _, digit := range digits {
				valid = valid && digit >= '0' && digit <= '9'
			}
			if valid {
				value, err := strconv.ParseInt(digits, 10, 64)
				if err == nil {
					number = value
				}
			}
		}
		where += ` AND(instr(lower(json_extract(item.request_json,'$.goal')),lower(?))>0
			OR instr(lower(title.title),lower(?))>0 OR execution.item_id=? OR execution.id=? OR pr.url=? OR pr.number=?)`
		parameters = append(parameters, query, query, query, query, query, number)
	}
	return filters, where, parameters, nil
}

func openOperatorKey(row sqlite.OperatorRow) []any {
	rank := int64(1)
	if row.Status == "waiting_for_input" {
		rank = 0
	}
	return []any{rank, row.CreatedAt, row.ID}
}

func closedOperatorKey(row sqlite.OperatorRow) []any { return []any{row.UpdatedAt, row.ID} }

func (a *App) OperatorList(ctx context.Context, request contract.OperatorQuery, condition ConditionalRead) (OperatorResponse, error) {
	var response OperatorResponse
	filters, where, parameters, err := operatorFilters(request)
	if err != nil {
		return response, err
	}
	scope := request.Kind
	if len(filters) > 0 {
		value, err := contract.PythonJSON(filters)
		if err != nil {
			return response, err
		}
		scope += ":" + contract.Digest(value)[:16]
	}
	shape := []string{"stamp", "id"}
	if request.Kind == "open" {
		shape = []string{"rank", "stamp", "id"}
	}
	after, err := contract.DecodeCursor(request.Cursor, scope, shape)
	if err != nil {
		return response, &BadRequest{"Invalid cursor"}
	}
	err = a.Store.Read(ctx, func(tx *sqlite.Tx) error {
		now := a.Clock().UTC()
		current, err := operatorSnapshot(tx, now)
		if err != nil {
			return err
		}
		response.ETag, response.NotModified, err = operatorValidator(tx, now, current, condition, nil)
		if err != nil || response.NotModified {
			return err
		}
		var rows []sqlite.OperatorRow
		var next []any
		if request.Kind == "open" {
			if len(filters) == 0 {
				rows, err = tx.OperatorOpenPage(after, nil, request.Limit+1, "", nil)
				if len(rows) > request.Limit {
					next, rows = openOperatorKey(rows[request.Limit-1]), rows[:request.Limit]
				}
			} else {
				keys, failure := tx.OperatorOpenCandidates(after, 2000)
				if failure != nil {
					return failure
				}
				if len(keys) > 0 {
					last := keys[len(keys)-1]
					rows, err = tx.OperatorOpenPage(after, last, request.Limit+1, where, parameters)
					if len(rows) > request.Limit {
						next, rows = openOperatorKey(rows[request.Limit-1]), rows[:request.Limit]
					} else if len(keys) == 2000 {
						next = last
					}
				}
			}
		} else if len(filters) == 0 {
			rows, err = tx.OperatorClosedPage(after, nil, request.Limit+1, "", nil)
			if len(rows) > request.Limit {
				next, rows = closedOperatorKey(rows[request.Limit-1]), rows[:request.Limit]
			}
		} else {
			var floor *string
			if value, exists := filters["closed_after"]; exists {
				floor = &value
			}
			keys, failure := tx.OperatorClosedCandidates(after, floor, 2000)
			if failure != nil {
				return failure
			}
			if len(keys) > 0 {
				last := keys[len(keys)-1]
				rows, err = tx.OperatorClosedPage(after, last, request.Limit+1, where, parameters)
				if len(rows) > request.Limit {
					next, rows = closedOperatorKey(rows[request.Limit-1]), rows[:request.Limit]
				} else if len(keys) == 2000 {
					next = last
				}
			}
		}
		if err != nil {
			return err
		}
		items := []map[string]any{}
		for _, row := range rows {
			item, err := operatorSummary(row, now, current, false)
			if err != nil {
				return err
			}
			items = append(items, item)
		}
		var cursor *string
		if next != nil {
			value, err := contract.EncodeCursor(scope, next)
			if err != nil {
				return err
			}
			cursor = &value
		}
		response.Body, err = encoded(map[string]any{"items": items, "next_cursor": cursor})
		return err
	})
	return response, err
}

func operatorBrief(tx *sqlite.Tx, itemID string) (map[string]any, error) {
	criteria, links, err := tx.OperatorBrief(itemID)
	if err != nil {
		return nil, err
	}
	result := map[string]any{}
	for name, value := range map[string]*string{"acceptance_criteria": criteria, "context_links": links} {
		var parsed any = []any{}
		if present(value) {
			parsed, _, err = contract.Decode([]byte(*value))
			if err != nil {
				return nil, err
			}
		}
		result[name] = parsed
	}
	return result, nil
}

func (a *App) OperatorItem(ctx context.Context, itemID string, condition ConditionalRead) (OperatorResponse, error) {
	var response OperatorResponse
	err := a.Store.Read(ctx, func(tx *sqlite.Tx) error {
		if _, err := tx.Item(itemID); err == sql.ErrNoRows {
			return &Missing{"Item not found"}
		} else if err != nil {
			return err
		}
		now := a.Clock().UTC()
		current, err := operatorSnapshot(tx, now)
		if err != nil {
			return err
		}
		response.ETag, response.NotModified, err = operatorValidator(tx, now, current, condition, nil)
		if err != nil || response.NotModified {
			return err
		}
		row, err := tx.OperatorCurrentItem(itemID)
		if err != nil {
			return err
		}
		item, err := operatorSummary(row, now, current, true)
		if err != nil {
			return err
		}
		brief, err := operatorBrief(tx, itemID)
		if err != nil {
			return err
		}
		for name, value := range brief {
			item[name] = value
		}
		response.Body, err = encoded(map[string]any{"item": item})
		return err
	})
	return response, err
}

func outcomeCounts() map[string]int64 {
	return map[string]int64{"report_stored": 0, "draft_published": 0, "ready_for_check_in": 0, "checked_in": 0, "failed": 0, "cancelled": 0}
}

func (a *App) OperatorOverview(ctx context.Context, condition ConditionalRead) (OperatorResponse, error) {
	var response OperatorResponse
	err := a.Store.Read(ctx, func(tx *sqlite.Tx) error {
		now := a.Clock().UTC()
		current, err := operatorSnapshot(tx, now)
		if err != nil {
			return err
		}
		scouting, err := scoutStatus(tx, now)
		if err != nil {
			return err
		}
		response.ETag, response.NotModified, err = operatorValidator(tx, now, current, condition, scouting)
		if err != nil || response.NotModified {
			return err
		}
		open, err := tx.OperatorOpenCounts()
		if err != nil {
			return err
		}
		closures, err := tx.OperatorClosedCounts(pythonStamp(now.Add(-24 * time.Hour)))
		if err != nil {
			return err
		}
		oldest, err := tx.OperatorOldestQueue(now.UnixMicro() / 1000)
		if err != nil {
			return err
		}
		configuration, _, err := scoutSettings(tx)
		if err != nil {
			return err
		}
		needs, err := tx.ScoutNeedsOwner(configuration.RepositoriesJSON, 100)
		if err != nil {
			return err
		}
		sections := map[string]int64{"needs_you": 0, "running": 0, "waiting_on_checks": 0, "queued": 0, "blocked": 0}
		for _, row := range open {
			name := ""
			switch row.Status {
			case "queued":
				name = "queued"
				if row.Blocked {
					name = "blocked"
				}
			case "running":
				name = "running"
			case "waiting_for_input":
				name = "needs_you"
			case "waiting_for_external":
				name = "waiting_on_checks"
			default:
				return errors.New("Unsupported open execution status")
			}
			sections[name] += row.Count
		}
		closed := outcomeCounts()
		for name, count := range closures {
			closed[name] = count
		}
		for _, name := range []string{"report_stored", "draft_published", "ready_for_check_in", "checked_in"} {
			sections["delivered_24h"] += closed[name]
		}
		counts, ok := scouting["counts"].(map[string]int64)
		if !ok {
			return errors.New("Scout counts are not numeric")
		}
		sections["needs_you"] += counts["needs_owner"]
		var oldestStamp *string
		var age *int64
		if oldest != nil {
			moment := time.UnixMilli(*oldest).UTC()
			if moment.Year() < 1 || moment.Year() > 9999 {
				return errors.New("Queued time is outside datetime range")
			}
			stamp := pythonStamp(moment)
			oldestStamp = &stamp
			value := max(int64(0), (now.UnixMicro()/1000-*oldest)/1000)
			age = &value
		}
		response.Body, err = encoded(map[string]any{"issue_scout": scouting, "issue_scout_needs_owner": needs,
			"generated_at": pythonStamp(now), "sections": sections, "closed_24h": closed,
			"dispatch":            map[string]bool{"paused": current.Pickup.Capacity.Paused},
			"oldest_queued_since": oldestStamp, "oldest_queued_age_seconds": age})
		return err
	})
	return response, err
}

func knownOperatorSum(rows []sqlite.OperatorRow, additions bool) (any, error) {
	total := new(big.Int)
	floating, floatTotal, known := false, float64(0), false
	for _, row := range rows {
		value := row.PRAdditions
		if !additions {
			value = row.PRDeletions
		}
		if value == nil {
			continue
		}
		known = true
		switch value := value.(type) {
		case int64:
			if floating {
				floatTotal += float64(value)
			} else {
				total.Add(total, big.NewInt(value))
			}
		case float64:
			if !floating {
				floatTotal, _ = new(big.Float).SetInt(total).Float64()
				floating = true
			}
			floatTotal += value
		default:
			return nil, errors.New("Stored PR size is not numeric")
		}
	}
	if !known {
		return nil, nil
	}
	if floating {
		return floatTotal, nil
	}
	return json.Number(total.String()), nil
}

func (a *App) OperatorGroup(ctx context.Context, label string, request contract.OperatorQuery, condition ConditionalRead) (OperatorResponse, error) {
	var response OperatorResponse
	scope := "group:" + label
	after, err := contract.DecodeCursor(request.Cursor, scope, []string{"stamp", "id"})
	if err != nil {
		return response, &BadRequest{"Invalid cursor"}
	}
	err = a.Store.Read(ctx, func(tx *sqlite.Tx) error {
		exists, err := tx.OperatorGroupExists(label)
		if err != nil {
			return err
		}
		if !exists {
			return &Missing{"No work items have this label"}
		}
		now := a.Clock().UTC()
		current, err := operatorSnapshot(tx, now)
		if err != nil {
			return err
		}
		response.ETag, response.NotModified, err = operatorValidator(tx, now, current, condition, nil)
		if err != nil || response.NotModified {
			return err
		}
		rows, err := tx.OperatorGroup(label, after, request.Limit+1)
		if err != nil {
			return err
		}
		var cursor *string
		if len(rows) > request.Limit {
			row := rows[request.Limit-1]
			value, err := contract.EncodeCursor(scope, []any{row.ItemCreatedAt, row.ItemID})
			if err != nil {
				return err
			}
			cursor, rows = &value, rows[:request.Limit]
		}
		closed, open, unknown := outcomeCounts(), int64(0), int64(0)
		var first, lastClosed, oldestOpen *string
		items := []map[string]any{}
		for _, row := range rows {
			item, err := operatorSummary(row, now, current, false)
			if err != nil {
				return err
			}
			items = append(items, item)
			if outcome, ok := item["outcome"].(string); ok {
				closed[outcome]++
			}
			if first == nil || row.ItemCreatedAt < *first {
				value := row.ItemCreatedAt
				first = &value
			}
			if Terminal(Status(row.Status)) {
				if lastClosed == nil || row.UpdatedAt > *lastClosed {
					value := row.UpdatedAt
					lastClosed = &value
				}
			} else {
				open++
				if oldestOpen == nil || row.CreatedAt < *oldestOpen {
					value := row.CreatedAt
					oldestOpen = &value
				}
			}
			if row.PRURL != nil && (row.PRAdditions == nil || row.PRDeletions == nil) {
				unknown++
			}
		}
		additions, err := knownOperatorSum(rows, true)
		if err != nil {
			return err
		}
		deletions, err := knownOperatorSum(rows, false)
		if err != nil {
			return err
		}
		response.Body, err = encoded(map[string]any{"label": label, "items": items, "next_cursor": cursor,
			"rollup": map[string]any{"scope": "page", "items": len(rows), "open": open, "additions": additions, "deletions": deletions,
				"size_unknown_count": unknown, "first_created_at": first, "last_closed_at": lastClosed,
				"oldest_open_since": oldestOpen, "outcomes": closed}})
		return err
	})
	return response, err
}

var displayedSHA = regexp.MustCompile(`^[0-9a-f]{7,64}$`)

func appliedSHA(details map[string]any) *string {
	values := []any{details["candidate"], details["prevalidated_candidate"], details["reviewed_candidate"]}
	if pull, ok := contract.ObjectValues(details["pull"]); ok {
		values = append(values, pull["head_sha"])
	}
	for _, value := range values {
		if text, ok := value.(string); ok && displayedSHA.MatchString(text) {
			return &text
		}
	}
	return nil
}

func resultReason(details map[string]any) (*string, error) {
	parts := []string{}
	for _, field := range []string{"outcome", "issue"} {
		if value, exists := details[field]; exists && value != nil && value != "" {
			text, ok := value.(string)
			if !ok {
				return nil, errors.New("Result cause is not text")
			}
			parts = append(parts, reasonText(text, 40))
		}
	}
	checks := []string{}
	if value := details["local_checks"]; value != nil {
		entries, ok := value.([]any)
		if !ok {
			return nil, errors.New("Result local checks are not a list")
		}
		for _, entry := range entries {
			check, ok := contract.ObjectValues(entry)
			if !ok {
				return nil, errors.New("Result local check is not an object")
			}
			if check["outcome"] == "failed" {
				name, ok := check["name"].(string)
				if !ok {
					return nil, errors.New("Result local-check name is not text")
				}
				cause := ""
				if value := check["reason"]; value != nil && value != "" {
					reason, ok := value.(string)
					if !ok {
						return nil, errors.New("Result local-check reason is not text")
					}
					cause = ": " + reasonText(reason, 160)
				}
				checks = append(checks, "local check "+reasonText(name, 80)+" failed"+cause)
			}
		}
	}
	if len(checks) > 0 {
		parts = append(parts, reasonText(strings.Join(checks, "; "), 180))
	}
	if value := details["failure_reason"]; value != nil && value != "" {
		text, ok := value.(string)
		if !ok {
			return nil, errors.New("Result failure reason is not text")
		}
		parts = append(parts, reasonText(text, 140))
	}
	suffix := ""
	if sha := appliedSHA(details); sha != nil {
		if len(parts) > 0 {
			suffix = "; "
		}
		suffix += "at " + firstRunes(*sha, 12)
	}
	text := reasonText(strings.Join(parts, "; "), 400-len(suffix))
	if text == "" && suffix == "" {
		return nil, nil
	}
	result := text + suffix
	return &result, nil
}

func operatorTimelineEvent(row sqlite.OperatorEvent) (map[string]any, error) {
	var details map[string]any
	if present(row.Details) {
		value, _, err := contract.Decode([]byte(*row.Details))
		if err != nil {
			return nil, err
		}
		var ok bool
		details, ok = contract.ObjectValues(value)
		if !ok {
			return nil, errors.New("Timeline details are not an object")
		}
	}
	reason := row.Reason
	if row.Event == "result" && reason == nil {
		var err error
		reason, err = resultReason(details)
		if err != nil {
			return nil, err
		}
	}
	return map[string]any{"version": row.Version, "event": row.Event, "created_at": row.CreatedAt,
		"attempt_id": row.AttemptID, "stage": row.Stage, "status": row.Status, "from_stage": row.FromStage,
		"reason": reason, "answered_by": row.AnsweredBy, "candidate": appliedSHA(details), "pr_url": details["pr_url"]}, nil
}

func (a *App) OperatorTimeline(ctx context.Context, executionID string, request contract.OperatorQuery) (json.RawMessage, error) {
	scope := "timeline:" + executionID
	after, err := contract.DecodeCursor(request.Cursor, scope, []string{"version", "stamp"})
	if err != nil {
		return nil, &BadRequest{"Invalid cursor"}
	}
	var result json.RawMessage
	err = a.Store.Read(ctx, func(tx *sqlite.Tx) error {
		execution, err := tx.OperatorExecution(executionID)
		if err == sql.ErrNoRows {
			return &Missing{"Execution not found"}
		}
		if err != nil {
			return err
		}
		previous, next, err := tx.OperatorRelatives(executionID)
		if err != nil {
			return err
		}
		links, err := tx.OperatorPrerequisites(executionID)
		if err != nil {
			return err
		}
		prerequisites := []map[string]any{}
		for _, link := range links {
			if link.Title == nil {
				return errors.New("Prerequisite title is missing")
			}
			prerequisites = append(prerequisites, map[string]any{"item_id": link.ItemID, "title": shortTitle(*link.Title, 80),
				"milestone": link.Milestone, "reason": link.Reason})
		}
		var question any
		if execution.Status == "waiting_for_input" {
			text, err := tx.OperatorQuestion(executionID, execution.Version)
			if err != nil {
				return err
			}
			if present(text) {
				question, _, err = contract.Decode([]byte(*text))
				if err != nil {
					return err
				}
			}
		}
		rows, err := tx.OperatorEvents(executionID, after, request.Limit+1)
		if err != nil {
			return err
		}
		var cursor *string
		if len(rows) > request.Limit {
			row := rows[request.Limit-1]
			value, err := contract.EncodeCursor(scope, []any{row.Version, row.CreatedAt})
			if err != nil {
				return err
			}
			cursor, rows = &value, rows[:request.Limit]
		}
		events := []map[string]any{}
		for _, row := range rows {
			event, err := operatorTimelineEvent(row)
			if err != nil {
				return err
			}
			events = append(events, event)
		}
		usage, err := usageProjection(tx, execution.ItemID, executionID)
		if err != nil {
			return err
		}
		now := a.Clock().UTC()
		current, err := operatorSnapshot(tx, now)
		if err != nil {
			return err
		}
		item, err := operatorSummary(execution, now, current, false)
		if err != nil {
			return err
		}
		brief, err := operatorBrief(tx, execution.ItemID)
		if err != nil {
			return err
		}
		for name, value := range brief {
			item[name] = value
		}
		result, err = encoded(map[string]any{"item": item, "usage": usage, "attempt": execution.StageOwner,
			"previous_execution_id": previous, "next_execution_id": next, "prerequisites": prerequisites,
			"question": question, "events": events, "next_cursor": cursor})
		return err
	})
	return result, err
}

func (a *App) MusterStatus(ctx context.Context, home *string) (json.RawMessage, error) {
	var result json.RawMessage
	now := a.Clock().UTC()
	err := a.Store.Read(ctx, func(tx *sqlite.Tx) error {
		_, live, err := liveSnapshot(tx, now)
		if err != nil {
			return err
		}
		ids := make([]string, 0, len(live))
		for id := range live {
			ids = append(ids, id)
		}
		sort.Strings(ids)
		captains := []map[string]any{}
		for _, id := range ids {
			row := live[id]
			captains = append(captains, map[string]any{"id": row.ID, "session_ref": row.SessionRef,
				"state": row.State, "heartbeat_at": row.HeartbeatAt})
		}
		writers, err := tx.MusterWriters()
		if err != nil {
			return err
		}
		rows, err := tx.ReconcilerHeartbeats()
		if err != nil {
			return err
		}
		reconcilers := []map[string]any{}
		for _, row := range rows {
			stamp, err := time.Parse(time.RFC3339Nano, row.LastSeen)
			if err != nil {
				return err
			}
			seconds := elapsedSeconds(now, stamp)
			var age any = seconds
			if seconds <= 0 {
				age = int64(0)
			}
			reconcilers = append(reconcilers, map[string]any{"runner_id": row.RunnerID, "last_seen": row.LastSeen,
				"age_seconds": age, "stale": seconds >= 900})
		}
		settings, err := tx.DispatchSettings()
		if err != nil {
			return err
		}
		result, err = encoded(map[string]any{"status": "ok", "identity": map[string]any{"home": home},
			"captains": captains, "writers": writers, "reconcilers": reconcilers,
			"dispatch_paused": settings.Paused, "observed_at": automaticISO(now)})
		return err
	})
	return result, err
}

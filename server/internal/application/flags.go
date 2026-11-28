package application

import (
	"context"
	"errors"
	"fmt"
	"sort"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/caltseng/phalanx/server/internal/contract"
	"github.com/caltseng/phalanx/server/internal/sqlite"
)

const (
	derivedLimit       = 200
	queueBacklogDelay  = 30 * time.Minute
	activeOverdueDelay = 90 * time.Minute
)

type FlagView struct {
	contract.FlagKey
	Reason      string `json:"reason"`
	Recovery    string `json:"recovery"`
	FirstSeen   string `json:"first_seen"`
	NotifyOwner bool   `json:"notify_owner"`
	Source      string `json:"source"`
}

func elapsedSeconds(now, then time.Time) float64 {
	return float64(now.UnixMicro()-then.UnixMicro()) / 1000000
}

func uniqueStrings(values []string) []string {
	result, seen := []string{}, map[string]bool{}
	for _, value := range values {
		if !seen[value] {
			result, seen[value] = append(result, value), true
		}
	}
	return result
}

func serverFlag(kind, reason, recovery, stamp string, subject contract.FlagKey, notify bool) FlagView {
	subject.Kind = kind
	return FlagView{FlagKey: subject, Reason: reason, Recovery: recovery, FirstSeen: stamp,
		NotifyOwner: notify, Source: "server"}
}

func storedFlag(row sqlite.FlagRecord) FlagView {
	return FlagView{FlagKey: contract.FlagKey{Kind: row.Kind, ItemID: row.ItemID, ExecutionID: row.ExecutionID, CaptainID: row.CaptainID},
		Reason: row.Reason, Recovery: row.Recovery, FirstSeen: row.FirstSeen, NotifyOwner: row.NotifyOwner, Source: "stored"}
}

func (a *App) RaiseFlag(ctx context.Context, request contract.Flag) (any, error) {
	var result any
	err := a.Store.Write(ctx, func(tx *sqlite.Tx) error {
		now := timestamp(a.Clock())
		row, err := tx.RaiseFlag(sqlite.FlagRecord{Kind: request.Kind, ItemID: request.ItemID, ExecutionID: request.ExecutionID,
			CaptainID: request.CaptainID, Reason: request.Reason, Recovery: request.Recovery, NotifyOwner: request.NotifyOwner,
			FirstSeen: now, UpdatedAt: now})
		if err != nil {
			return err
		}
		result, err = responseValue(storedFlag(row))
		return err
	})
	return result, err
}

func (a *App) ClearFlag(ctx context.Context, request contract.FlagKey) (any, error) {
	var result any
	err := a.Store.Write(ctx, func(tx *sqlite.Tx) error {
		cleared, err := tx.ClearFlag(request.Kind, request.ItemID, request.ExecutionID, request.CaptainID)
		if err != nil {
			return err
		}
		result, err = responseValue(map[string]bool{"cleared": cleared})
		return err
	})
	return result, err
}

func captainStaleFlags(current pickupContext) ([]FlagView, error) {
	result := []FlagView{}
	for _, row := range current.AllCaptains {
		if row.State == "ended" {
			continue
		}
		if _, live := current.Live[row.ID]; live {
			continue
		}
		if len(result) >= derivedLimit {
			break
		}
		gone, err := captainGone(row)
		if err != nil {
			return nil, err
		}
		heartbeat, err := time.Parse(time.RFC3339Nano, row.HeartbeatAt)
		if err != nil {
			return nil, err
		}
		reason := "No heartbeat since " + row.HeartbeatAt
		if row.EndingAt != nil && gone.Before(heartbeat.Add(captainHeartbeatTimeout)) {
			reason = "Ending since " + *row.EndingAt + " without finishing its handoff"
		}
		result = append(result, serverFlag("captain_stale", reason,
			"Verify the exact native Captain and shared API availability; stale is not proof of native loss. "+
				"Resume the same available Captain; replace only for verified native loss/degradation, never an API outage or unknown status. "+
				"Reconcile pending successors and writer nonces before recovery. Its creator archives it only after "+
				"a clean handoff and local-work preservation checks.",
			timestamp(gone), contract.FlagKey{CaptainID: row.ID}, false))
	}
	return result, nil
}

func noLiveCaptainFlags(tx *sqlite.Tx, current pickupContext) ([]FlagView, error) {
	oldest, err := tx.OldestOpenWork()
	if err != nil || oldest == nil || len(current.Live) > 0 {
		return []FlagView{}, err
	}
	first, err := time.Parse(time.RFC3339Nano, *oldest)
	if err != nil {
		return nil, err
	}
	var last *sqlite.Captain
	for index := range current.AllCaptains {
		row := &current.AllCaptains[index]
		if last == nil || row.HeartbeatAt > last.HeartbeatAt || row.HeartbeatAt == last.HeartbeatAt && row.ID > last.ID {
			last = row
		}
	}
	for _, row := range current.AllCaptains {
		if row.State == "ended" && (last == nil || row.ID != last.ID) {
			continue
		}
		gone, err := captainGone(row)
		if err != nil {
			return nil, err
		}
		if gone.After(first) {
			first = gone
		}
	}
	reason, captainID := "No live Captain while open work exists", ""
	if last != nil {
		captainID = last.ID
		reason += ". Last Captain " + last.ID + " (" + last.SessionRef + ") heartbeat " + last.HeartbeatAt
		if present(last.HandoffNote) {
			reason += ". Handoff note: " + *last.HandoffNote
		}
	}
	return []FlagView{serverFlag("no_live_captain", firstRunes(reason, 16000),
		"The keeper verifies API and native availability before recovery; resume the same available Captain. "+
			"An API outage or unknown native status must not spawn a replacement. Start only for verified native loss/degradation "+
			"or authoritative empty/ended history, respecting pending successors, handoff grace and charter stop conditions.",
		timestamp(first), contract.FlagKey{CaptainID: captainID}, true)}, nil
}

func firstRunes(value string, maximum int) string {
	if utf8.RuneCountInString(value) <= maximum {
		return value
	}
	return string([]rune(value)[:maximum])
}

func idleCaptainFlags(tx *sqlite.Tx, now time.Time, current pickupContext) ([]FlagView, error) {
	result := []FlagView{}
	if len(current.Live) == 0 {
		return result, nil
	}
	rows, err := tx.OverdueQueue(now.Add(-queueBacklogDelay).UnixMicro() / 1000)
	if err != nil {
		return nil, err
	}
	for _, row := range rows {
		decision, err := pickupDecision(row, now, current)
		if err != nil {
			return nil, err
		}
		waiting := current.Admission[Stage(row.Stage)]
		kind, reason, recovery := "", "", ""
		if waiting != nil && (decision.Code == "capacity" || decision.Code == "paused" || decision.Code == "held") {
			kind = "queue_backlogged"
			reason = fmt.Sprintf("%s; Building %d/%d, Delivering %d, Total %d/%d", *waiting,
				current.Capacity.BuildingWriters, current.Capacity.MaxBuildingWriters, current.Capacity.DeliveringWriters,
				current.Capacity.ActiveWriters, current.Capacity.MaxWriters)
			recovery = "Wait for the affected capacity, resume dispatch or service eligible waiting PR fixes"
		} else if decision.Code == "held" || decision.Code == "retry" || decision.Code == "not_ready" || decision.Code == "closing" {
			continue
		} else if !decision.Eligible {
			kind, reason, recovery = "pickup_blocked", *decision.Reason, decision.Trigger
		} else {
			if *row.ReadySince > now.Add(-60*time.Minute).UnixMicro()/1000 {
				continue
			}
			kind = "captain_idle_with_work"
			reason = fmt.Sprintf("Dispatch overdue: runnable for 60 min with no registered writer; %d of %d writer slots occupied",
				current.Capacity.ActiveWriters, current.Capacity.MaxWriters)
			recovery = "A Captain launches it, or records a valid dependency, sequence or hold reason"
		}
		if hold := current.Holds[row.ID]; hold != "" {
			reason += ". Captain hold: " + hold
		}
		delay := queueBacklogDelay
		if kind == "captain_idle_with_work" {
			delay = 60 * time.Minute
		}
		result = append(result, serverFlag(kind, reason, recovery,
			timestamp(time.UnixMilli(*row.ReadySince).UTC().Add(delay)),
			contract.FlagKey{ItemID: row.ItemID, ExecutionID: row.ID}, kind == "pickup_blocked"))
		if len(result) >= derivedLimit {
			break
		}
	}
	return result, nil
}

func launchWait(id, nonce, created string, now time.Time) (*Wait, error) {
	stamp, err := time.Parse(time.RFC3339Nano, created)
	if err != nil {
		return nil, err
	}
	age := max(int64(0), int64(elapsedSeconds(now, stamp)))
	detail := " (handoff pending)"
	if elapsedSeconds(now, stamp) >= 600 {
		detail = " (recovery overdue)"
	}
	return &Wait{
		Reason: fmt.Sprintf("Native writer launch unconfirmed for %d s%s; reserved since %s. A reserved stage does not prove a native writer is running.",
			age, detail, created),
		Trigger: "Inspect a fresh full native inventory for full nonce " + nonce + ", writer " + id + ". " +
			"Confirm the exact matching session. Only if none exists and no create is in flight, the owning Captain " +
			"replays `shift launch` and creates/confirms once with the same nonce. If inventory or replay is inconclusive, " +
			"retain the reservation and report the recovery blocker.",
	}, nil
}

func writerFlags(rows []sqlite.FlagWriter, now time.Time, current pickupContext) ([]FlagView, error) {
	result := []FlagView{}
	for _, row := range rows[:min(len(rows), derivedLimit)] {
		subject := contract.FlagKey{ItemID: row.ItemID, ExecutionID: row.ExecutionID, CaptainID: row.CaptainID}
		created, err := time.Parse(time.RFC3339Nano, row.CreatedAt)
		if err != nil {
			return nil, err
		}
		if row.ConfirmedAt == nil && elapsedSeconds(now, created) >= 600 {
			wait, err := launchWait(row.ID, row.Nonce, row.CreatedAt, now)
			if err != nil {
				return nil, err
			}
			result = append(result, serverFlag("launch_unconfirmed", wait.Reason, wait.Trigger,
				timestamp(created.Add(10*time.Minute)), subject, true))
		}
		capacityWait := present(row.BuildingRequestedAt) && !row.Building &&
			buildingReason(current.Capacity, current.Rework, row.ID) != nil
		if row.State == "busy" && !capacityWait {
			moved, err := time.Parse(time.RFC3339Nano, row.LastAdvancedAt)
			if err != nil {
				return nil, err
			}
			for _, text := range []*string{row.LastProgressAt, row.ConfirmedAt} {
				if present(text) {
					stamp, err := time.Parse(time.RFC3339Nano, *text)
					if err != nil {
						return nil, err
					}
					if stamp.After(moved) {
						moved = stamp
					}
				}
			}
			if elapsedSeconds(now, moved) >= 7200 {
				layout := "2006-01-02T15:04:05"
				if moved.Nanosecond() != 0 {
					layout += ".000000"
				}
				reason := "Busy with no progress or stage advance since " + moved.Format(layout+"-07:00")
				result = append(result, serverFlag("no_progress", reason, "Check the session, then nudge or replace it",
					timestamp(moved.Add(2*time.Hour)), subject, false))
			}
		}
		if row.State == "stopped" || row.State == "error" {
			stamp := created
			if present(row.StateReportedAt) {
				stamp, err = time.Parse(time.RFC3339Nano, *row.StateReportedAt)
				if err != nil {
					return nil, err
				}
			}
			result = append(result, serverFlag("writer_stopped", "Writer reported "+row.State+" without a result",
				"Resume the same session or replace the writer", timestamp(stamp), subject, false))
		}
	}
	return result, nil
}

func sessionString(body map[string]any, key string) (string, error) {
	value, ok := body[key].(string)
	if !ok {
		return "", errors.New("Stored native session " + key + " is missing or not text")
	}
	return value, nil
}

func sessionFlags(tx *sqlite.Tx, rows []sqlite.FlagWriter, now time.Time, current pickupContext, scoped bool) ([]FlagView, error) {
	reported := map[string]map[string]any{}
	for _, captain := range current.AllCaptains {
		if _, live := current.Live[captain.ID]; !live {
			continue
		}
		value, _, err := contract.Decode([]byte(captain.SessionsJSON))
		if err != nil {
			return nil, err
		}
		sessions, ok := value.([]any)
		if !ok {
			return nil, errors.New("Stored native sessions are not a list")
		}
		for _, value := range sessions {
			session, ok := contract.ObjectValues(value)
			if !ok {
				return nil, errors.New("Stored native session is not an object")
			}
			id, err := sessionString(session, "id")
			if err != nil {
				return nil, err
			}
			first, err := sessionString(session, "first_seen")
			if err != nil {
				return nil, err
			}
			merged := map[string]any{}
			if known := reported[id]; known != nil {
				for key, value := range known {
					merged[key] = value
				}
				previous, err := sessionString(known, "first_seen")
				if err != nil {
					return nil, err
				}
				first = min(first, previous)
			}
			for key, value := range session {
				merged[key] = value
			}
			merged["captain_id"], merged["first_seen"] = captain.ID, first
			reported[id] = merged
		}
	}
	if len(reported) == 0 {
		return []FlagView{}, nil
	}
	references, err := tx.RegisteredSessionReferences()
	if err != nil {
		return nil, err
	}
	registered, successors := map[string]bool{}, map[string]bool{}
	for _, reference := range references {
		registered[sessionIdentity(reference)] = true
	}
	for _, captain := range current.AllCaptains {
		if captain.State == "ending" {
			successors["phx captain "+firstRunes(captain.ID, 8)] = true
		}
	}
	byNonce := map[string]sqlite.FlagWriter{}
	for _, row := range rows {
		byNonce[row.Nonce] = row
	}
	archived, err := tx.AllArchivedSessions()
	if err != nil {
		return nil, err
	}
	sessions := make([]map[string]any, 0, len(reported))
	for _, session := range reported {
		sessions = append(sessions, session)
	}
	sort.SliceStable(sessions, func(i, j int) bool {
		a, b := sessions[i]["first_seen"].(string), sessions[j]["first_seen"].(string)
		return a < b || a == b && sessions[i]["id"].(string) < sessions[j]["id"].(string)
	})
	result := []FlagView{}
	for _, session := range sessions {
		availability, _ := session["availability"].(string)
		if availability == "missing" || availability == "deleted" {
			continue
		}
		id, err := sessionString(session, "id")
		if err != nil {
			return nil, err
		}
		name, err := sessionString(session, "name")
		if err != nil {
			return nil, err
		}
		if archived[id] || registered[id] || successors[name] {
			continue
		}
		nonce, _ := session["writer_nonce"].(string)
		writer, bound := byNonce[nonce]
		if scoped && !bound {
			continue
		}
		first, err := sessionString(session, "first_seen")
		if err != nil {
			return nil, err
		}
		stamp, err := time.Parse(time.RFC3339Nano, first)
		if err != nil {
			return nil, err
		}
		captainID, err := sessionString(session, "captain_id")
		if err != nil {
			return nil, err
		}
		label := "Session " + id + " (" + name + ")"
		if !bound {
			result = append(result, serverFlag("orphan_session", label+" has no registered writer or Captain",
				"A Captain archives it if its work is done or abandoned, or stops it and reports it to the owner",
				timestamp(stamp), contract.FlagKey{CaptainID: captainID}, false))
		} else if writer.SessionID != nil {
			reference := writer.Nonce
			if present(writer.SessionID) {
				reference = *writer.SessionID
			}
			result = append(result, serverFlag("duplicate_writer", label+" carries item "+writer.ItemID+", whose writer is "+reference,
				"A Captain stops and archives the unregistered session", timestamp(stamp),
				contract.FlagKey{ItemID: writer.ItemID, ExecutionID: writer.ExecutionID, CaptainID: captainID}, false))
		}
		if len(result) >= derivedLimit {
			break
		}
	}
	return result, nil
}

func archiveFlags(tx *sqlite.Tx) ([]FlagView, error) {
	rows, err := tx.UnresolvedArchives(derivedLimit)
	if err != nil {
		return nil, err
	}
	result := []FlagView{}
	for _, row := range rows {
		stamp, err := time.Parse(time.RFC3339Nano, row.UpdatedAt)
		if err != nil {
			return nil, err
		}
		result = append(result, serverFlag("orphan_session", "Session "+row.SessionID+" archive "+row.Outcome+": "+row.Reason,
			"Preserve local work before archiving; for a runtime refusal, the owner must resolve it. Do not retry a refused archive.",
			timestamp(stamp), contract.FlagKey{}, true))
	}
	return result, nil
}

func activeOverdueFlags(tx *sqlite.Tx, now time.Time, known []FlagView, executionID string) ([]FlagView, error) {
	byExecution := map[string][]FlagView{}
	for _, flag := range known {
		byExecution[flag.ExecutionID] = append(byExecution[flag.ExecutionID], flag)
	}
	rows, err := tx.ActiveOverdue(timestamp(now.Add(-activeOverdueDelay)), executionID, derivedLimit)
	if err != nil {
		return nil, err
	}
	result := []FlagView{}
	for _, row := range rows {
		started, err := time.Parse(time.RFC3339Nano, row.StartedAt)
		if err != nil {
			return nil, err
		}
		state, err := readState([]byte(row.StateJSON))
		if err != nil {
			return nil, err
		}
		evidence, recovery := []string{}, []string{}
		if state.Wait != nil {
			evidence = append(evidence, "Recorded wait: "+state.Wait.Reason)
			recovery = append(recovery, "Recorded resume trigger: "+state.Wait.Trigger)
		}
		for _, flag := range byExecution[row.ID] {
			evidence = append(evidence, flag.Kind+": "+flag.Reason)
			recovery = append(recovery, flag.Recovery)
		}
		text := func(value *string) string {
			if value == nil {
				return "None"
			}
			return *value
		}
		if present(row.ProgressNote) {
			evidence = append(evidence, "Writer report at "+text(row.LastProgressAt)+": "+*row.ProgressNote)
		}
		if row.Action != nil {
			evidence = append(evidence, "Captain "+text(row.ActionCaptainID)+" recorded "+*row.Action+" at "+
				text(row.ActionCreatedAt)+": "+text(row.ActionReason))
		}
		actor := "An in-scope Captain (no registered writer)"
		if present(row.WriterID) {
			actor = "Writer " + *row.WriterID
			if present(row.SessionID) {
				actor += " (session " + *row.SessionID + ")"
			}
			actor += "; Captain " + text(row.CaptainID) + " coordinates"
		}
		detail := "No concrete blocker or resume trigger is recorded; assessment needed."
		if len(evidence) > 0 {
			detail = strings.Join(evidence, "; ")
		}
		age := elapsedSeconds(now, started)
		reason := fmt.Sprintf("Active work overdue: %.2f elapsed minutes (%s s) since %s; stage %s, status %s. ",
			age/60, strconv.FormatFloat(age, 'g', 6, 64), row.StartedAt, row.Stage, row.Status) + detail
		action := "Assessment/action actor: " + actor + ". " + strings.Join(uniqueStrings(recovery), "; ") +
			". Inspect current delivery evidence, take the next authorized unblocking action, and record the concrete blocker, " +
			"responsible actor and resume trigger if still waiting. Age alone authorizes no cancellation, replacement or merge."
		captainID := ""
		if row.CaptainID != nil {
			captainID = *row.CaptainID
		}
		result = append(result, serverFlag("active_overdue", firstRunes(reason, 16000), firstRunes(action, 16000),
			timestamp(started.Add(activeOverdueDelay)), contract.FlagKey{ItemID: row.ItemID, ExecutionID: row.ID, CaptainID: captainID}, false))
	}
	return result, nil
}

func scopedFlagContext(tx *sqlite.Tx, now time.Time) (pickupContext, error) {
	var current pickupContext
	var err error
	current.AllCaptains, current.Live, err = liveSnapshot(tx, now)
	if err != nil {
		return current, err
	}
	current.admissionContext, err = admissionSnapshot(tx, nil, now)
	return current, err
}

func executionFlags(tx *sqlite.Tx, now time.Time, executionID string, snapshot *pickupContext) ([]FlagView, error) {
	stored, err := tx.StoredFlags(executionID)
	if err != nil {
		return nil, err
	}
	flags := []FlagView{}
	for _, row := range stored {
		flags = append(flags, storedFlag(row))
	}
	var current pickupContext
	if snapshot != nil {
		current = *snapshot
	} else if executionID == "" {
		current, err = pickupSnapshot(tx, now)
	} else {
		current, err = scopedFlagContext(tx, now)
	}
	if err != nil {
		return nil, err
	}
	appendFlags := func(values []FlagView, failure error) error {
		flags = append(flags, values...)
		return failure
	}
	if executionID == "" {
		if err := appendFlags(captainStaleFlags(current)); err != nil {
			return nil, err
		}
		if err := appendFlags(noLiveCaptainFlags(tx, current)); err != nil {
			return nil, err
		}
		if err := appendFlags(idleCaptainFlags(tx, now, current)); err != nil {
			return nil, err
		}
	}
	writers, err := tx.FlagWriters(executionID)
	if err != nil {
		return nil, err
	}
	if err := appendFlags(writerFlags(writers, now, current)); err != nil {
		return nil, err
	}
	if err := appendFlags(sessionFlags(tx, writers, now, current, executionID != "")); err != nil {
		return nil, err
	}
	if executionID == "" {
		if err := appendFlags(archiveFlags(tx)); err != nil {
			return nil, err
		}
	}
	if err := appendFlags(observationFlags(tx, now, executionID)); err != nil {
		return nil, err
	}
	if executionID == "" {
		if err := appendFlags(reconcilerStaleFlags(tx, now)); err != nil {
			return nil, err
		}
	}
	if err := appendFlags(activeOverdueFlags(tx, now, flags, executionID)); err != nil {
		return nil, err
	}
	sort.SliceStable(flags, func(i, j int) bool {
		return flags[i].FirstSeen < flags[j].FirstSeen || flags[i].FirstSeen == flags[j].FirstSeen && flags[i].Kind < flags[j].Kind
	})
	return flags, nil
}

func (a *App) Flags(ctx context.Context) (any, error) {
	var result any
	err := a.Store.Read(ctx, func(tx *sqlite.Tx) error {
		flags, err := executionFlags(tx, a.Clock().UTC(), "", nil)
		if err != nil {
			return err
		}
		result, err = responseValue(map[string]any{"flags": flags})
		return err
	})
	return result, err
}

func (a *App) CurrentWriter(ctx context.Context, writerID string) (any, error) {
	var result any
	err := a.Store.Read(ctx, func(tx *sqlite.Tx) error {
		now := a.Clock().UTC()
		current, err := scopedFlagContext(tx, now)
		if err != nil {
			return err
		}
		writer, err := writerRecord(tx, writerID)
		if err != nil {
			return err
		}
		view, err := a.bindingWithContext(tx, writer, &current.admissionContext)
		if err != nil {
			return err
		}
		body, ok := contract.ObjectValues(view)
		if !ok {
			return errors.New("Writer binding is not an object")
		}
		flags, err := executionFlags(tx, now, writer.ExecutionID, &current)
		if err != nil {
			return err
		}
		body["flags"] = flags
		result, err = responseValue(body)
		return err
	})
	return result, err
}

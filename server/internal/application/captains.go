package application

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"strings"
	"time"
	"unicode"

	"github.com/caltseng/phalanx/server/internal/contract"
	"github.com/caltseng/phalanx/server/internal/sqlite"
)

func captainRow(tx *sqlite.Tx, id string) (sqlite.Captain, error) {
	row, err := tx.Captain(id)
	if err == sql.ErrNoRows {
		return row, &Missing{"Captain not found"}
	}
	return row, err
}

func captainGone(row sqlite.Captain) (time.Time, error) {
	heartbeat, err := time.Parse(time.RFC3339Nano, row.HeartbeatAt)
	if err != nil {
		return time.Time{}, err
	}
	if row.State == "ended" {
		return heartbeat, nil
	}
	gone := heartbeat.Add(15 * time.Minute)
	if row.EndingAt != nil {
		ending, err := time.Parse(time.RFC3339Nano, *row.EndingAt)
		if err != nil {
			return time.Time{}, err
		}
		if ending.Add(30 * time.Minute).Before(gone) {
			gone = ending.Add(30 * time.Minute)
		}
	}
	return gone, nil
}

func liveCaptainRows(tx *sqlite.Tx, now time.Time) ([]sqlite.Captain, error) {
	rows, err := tx.AllCaptains()
	if err != nil {
		return nil, err
	}
	result := []sqlite.Captain{}
	for _, row := range rows {
		if row.State == "ended" {
			continue
		}
		gone, err := captainGone(row)
		if err != nil {
			return nil, err
		}
		if now.Before(gone) {
			result = append(result, row)
		}
	}
	return result, nil
}

func reinforcement(row sqlite.Captain, now time.Time) (map[string]any, error) {
	created, err := time.Parse(time.RFC3339Nano, row.CreatedAt)
	if err != nil {
		return nil, err
	}
	interval := max(int64(0), (now.UnixMicro()-created.UnixMicro())/int64((90*time.Minute)/time.Microsecond))
	return map[string]any{"interval_minutes": 90, "interval": interval,
		"acknowledged_interval": row.ReinforcedInterval, "due": row.State == "active" && interval > row.ReinforcedInterval}, nil
}

func captainView(tx *sqlite.Tx, row sqlite.Captain, now time.Time) (json.RawMessage, error) {
	views, err := captainViews(tx, []sqlite.Captain{row}, now)
	if err != nil {
		return nil, err
	}
	return views[0], nil
}

func captainViews(tx *sqlite.Tx, rows []sqlite.Captain, now time.Time) ([]json.RawMessage, error) {
	current, err := liveCaptainRows(tx, now)
	if err != nil {
		return nil, err
	}
	live := make(map[string]bool, len(current))
	for _, row := range current {
		live[row.ID] = true
	}
	reports, err := sessionReportsSnapshot(tx)
	if err != nil {
		return nil, err
	}
	ids := make([]string, 0, len(rows))
	for _, row := range rows {
		ids = append(ids, row.ID)
	}
	details, err := tx.CaptainDetailsBatch(ids)
	if err != nil {
		return nil, err
	}
	views := make([]json.RawMessage, 0, len(rows))
	for _, row := range rows {
		view, err := captainViewWithContext(row, now, live[row.ID], reports, details[row.ID])
		if err != nil {
			return nil, err
		}
		views = append(views, view)
	}
	return views, nil
}

func sessionReportsSnapshot(tx *sqlite.Tx) (map[sessionKey]string, error) {
	availability, err := tx.CaptainAvailability()
	if err != nil {
		return nil, err
	}
	reports := map[sessionKey]string{}
	for _, report := range availability {
		id, runtime := "", ""
		if report.ID != nil {
			id = *report.ID
		}
		if present(report.Runtime) {
			runtime = *report.Runtime
		} else {
			runtime = recordedSessionKey(report.Session, "").runtime
		}
		reports[recordedSessionKey(id, runtime)] = report.Availability
	}
	return reports, nil
}

func captainViewWithContext(row sqlite.Captain, now time.Time, live bool, reports map[sessionKey]string, details sqlite.CaptainDetails) (json.RawMessage, error) {
	observed := reports[recordedSessionKey(row.SessionRef, "")]
	if observed == "" {
		observed = "unverified"
	}
	pickup, _, err := contract.Decode([]byte(row.PickupJSON))
	if err != nil {
		return nil, err
	}
	focus, _, err := contract.Decode([]byte(row.FocusJSON))
	if err != nil {
		return nil, err
	}
	sessions, _, err := contract.Decode([]byte(row.SessionsJSON))
	if err != nil {
		return nil, err
	}
	reinforced, err := reinforcement(row, now)
	if err != nil {
		return nil, err
	}
	short := row.ID
	if len(short) > 8 {
		short = short[:8]
	}
	return encoded(map[string]any{
		"id": row.ID, "session_ref": row.SessionRef, "charter_revision": row.CharterRevision, "pickup": pickup,
		"session_availability": observed, "session_link": sessionTarget(row.SessionRef, "", "", observed, false),
		"parent_id": row.ParentID, "state": row.State, "wake_count": row.WakeCount, "compaction_count": row.CompactionCount,
		"reinforcement": reinforced, "focus": focus, "sessions": sessions, "handoff_note": row.HandoffNote,
		"created_at": row.CreatedAt, "heartbeat_at": row.HeartbeatAt, "ending_at": row.EndingAt,
		"live": live, "stale": row.State != "ended" && !live, "open_intents": details.OpenIntents,
		"successors": details.Successors, "cleanup_pending": details.CleanupPending, "ended_writers": details.EndedWriters,
		"successor_name": "phx captain " + short,
	})
}

func (a *App) Captain(ctx context.Context, id string) (json.RawMessage, error) {
	var result json.RawMessage
	err := a.Store.Read(ctx, func(tx *sqlite.Tx) error {
		row, err := captainRow(tx, id)
		if err != nil {
			return err
		}
		result, err = captainView(tx, row, a.Clock().UTC())
		return err
	})
	return result, err
}

func (a *App) Captains(ctx context.Context, limit int) (json.RawMessage, error) {
	var result json.RawMessage
	err := a.Store.Read(ctx, func(tx *sqlite.Tx) error {
		rows, err := tx.Captains(limit)
		if err != nil {
			return err
		}
		views, err := captainViews(tx, rows, a.Clock().UTC())
		if err != nil {
			return err
		}
		result, err = encoded(map[string]any{"captains": views})
		return err
	})
	return result, err
}

func (a *App) RegisterCaptain(ctx context.Context, request contract.CaptainRegistration, key *string) (json.RawMessage, error) {
	var result json.RawMessage
	err := a.Store.Write(ctx, func(tx *sqlite.Tx) error {
		var err error
		result, err = replay(tx, "captain", key, request, func() (json.RawMessage, error) {
			if request.ParentID != nil {
				if _, err := captainRow(tx, *request.ParentID); err != nil {
					return nil, err
				}
			}
			id, err := a.ID()
			if err != nil {
				return nil, err
			}
			pickup, err := contract.Canonical(request.Pickup)
			if err != nil {
				return nil, err
			}
			if err := tx.InsertCaptain(id, request.SessionRef, request.CharterRevision, pickup, timestamp(a.Clock()), request.ParentID); err != nil {
				return nil, err
			}
			row, err := captainRow(tx, id)
			if err != nil {
				return nil, err
			}
			return captainView(tx, row, a.Clock().UTC())
		})
		return err
	})
	return result, err
}

func (a *App) CaptainHeartbeat(ctx context.Context, id string, request contract.CaptainHeartbeat) (json.RawMessage, error) {
	var result json.RawMessage
	err := a.Store.Write(ctx, func(tx *sqlite.Tx) error {
		row, err := captainRow(tx, id)
		if err != nil {
			return err
		}
		order := map[string]int{"active": 0, "ending": 1, "ended": 2}
		if order[request.State] < order[row.State] {
			return &Conflict{"A Captain that is " + row.State + " cannot become " + request.State + ". Register a new Captain"}
		}
		if request.Pickup != nil || request.CharterRevision != row.CharterRevision {
			pickup := contract.Pickup{Repositories: map[string]*string{}}
			if request.Pickup != nil {
				pickup = *request.Pickup
			}
			text, err := contract.Canonical(pickup)
			if err != nil {
				return err
			}
			if err := tx.CaptainPickup(id, text); err != nil {
				return err
			}
		}
		old, _, err := contract.Decode([]byte(row.SessionsJSON))
		if err != nil {
			return err
		}
		oldSessions, ok := old.([]any)
		if !ok {
			return errors.New("Stored Captain sessions are not a list")
		}
		seen := map[string]any{}
		for _, session := range oldSessions {
			body, ok := contract.ObjectValues(session)
			if !ok {
				return errors.New("Stored Captain session is not an object")
			}
			id, exists := body["id"]
			if !exists {
				return errors.New("Stored Captain session has no id")
			}
			first, exists := body["first_seen"]
			if !exists {
				return errors.New("Stored Captain session has no first_seen")
			}
			if id == nil {
				continue
			}
			text, ok := id.(string)
			if !ok {
				return errors.New("Stored Captain session id is not a string")
			}
			seen[text] = first
		}
		now := timestamp(a.Clock())
		sessions := []map[string]any{}
		for _, session := range request.Sessions {
			raw, err := encoded(session)
			if err != nil {
				return err
			}
			value, _, err := contract.Decode(raw)
			if err != nil {
				return err
			}
			body, ok := contract.ObjectValues(value)
			if !ok {
				return errors.New("Native session is not an object")
			}
			first, exists := seen[session.ID]
			if !exists {
				first = now
			}
			body["first_seen"] = first
			sessions = append(sessions, body)
		}
		sessionJSON, err := contract.Canonical(sessions)
		if err != nil {
			return err
		}
		focus, err := contract.Canonical(request.Focus)
		if err != nil {
			return err
		}
		ending := row.EndingAt
		if !present(ending) && request.State == "ending" {
			ending = &now
		}
		compaction := row.CompactionCount
		if request.CompactionCount != nil {
			count, err := request.CompactionCount.Int64()
			if err != nil {
				return err
			}
			if compaction == nil || count > *compaction {
				compaction = &count
			}
		}
		wake, err := request.WakeCount.Int64()
		if err != nil {
			return err
		}
		if err := tx.CaptainHeartbeat(id, request.CharterRevision, request.State, focus, sessionJSON, now, wake,
			request.HandoffNote, ending, compaction); err != nil {
			return err
		}
		row, err = captainRow(tx, id)
		if err != nil {
			return err
		}
		result, err = captainView(tx, row, a.Clock().UTC())
		return err
	})
	return result, err
}

func (a *App) CaptainReinforcement(ctx context.Context, id string, request contract.Reinforcement) (json.RawMessage, error) {
	var result json.RawMessage
	err := a.Store.Write(ctx, func(tx *sqlite.Tx) error {
		row, err := captainRow(tx, id)
		if err != nil {
			return err
		}
		now := a.Clock().UTC()
		current, err := reinforcement(row, now)
		if err != nil {
			return err
		}
		interval, err := request.Interval.Int64()
		if err != nil {
			return err
		}
		due, ok := current["interval"].(int64)
		if !ok {
			return errors.New("Reinforcement interval is not an integer")
		}
		if interval > max(due, row.ReinforcedInterval) {
			return &Conflict{"Cannot acknowledge a future reinforcement interval"}
		}
		if err := tx.CaptainReinforcement(id, interval); err != nil {
			return err
		}
		row, err = captainRow(tx, id)
		if err != nil {
			return err
		}
		current, err = reinforcement(row, now)
		if err != nil {
			return err
		}
		result, err = encoded(current)
		return err
	})
	return result, err
}

func (a *App) CurrentCaptain(ctx context.Context) (json.RawMessage, error) {
	var result json.RawMessage
	err := a.Store.Read(ctx, func(tx *sqlite.Tx) error {
		now := a.Clock().UTC()
		live, err := liveCaptainRows(tx, now)
		if err != nil {
			return err
		}
		sort.Slice(live, func(i, j int) bool {
			if live[i].CreatedAt != live[j].CreatedAt {
				return live[i].CreatedAt < live[j].CreatedAt
			}
			return live[i].ID < live[j].ID
		})
		candidates := []map[string]any{}
		for _, row := range live {
			if row.State == "active" {
				candidates = append(candidates, map[string]any{"id": row.ID, "session_ref": row.SessionRef})
			}
		}
		var captain map[string]any
		reason := "No live active Captain; ending, ended and stale registrations are not routing destinations"
		if len(candidates) > 1 {
			reason = fmt.Sprintf("%d live active Captains; no unique routing destination", len(candidates))
		} else if len(candidates) == 1 {
			reference, ok := candidates[0]["session_ref"].(string)
			if !ok {
				return errors.New("Captain session reference is not text")
			}
			if strings.TrimFunc(reference, func(r rune) bool { return unicode.IsSpace(r) || r >= 0x1c && r <= 0x1f }) == "" {
				reason = "The live active Captain has no native session binding"
			} else {
				captain = candidates[0]
				reason = "Unique live active registration; verify its session_ref and conversation link in the runtime"
			}
		}
		result, err = encoded(map[string]any{"captain": captain, "candidates": candidates, "reason": reason, "observed_at": timestamp(now)})
		return err
	})
	return result, err
}

func (a *App) CaptainAction(ctx context.Context, itemID string, request contract.CaptainAction, key *string) (json.RawMessage, error) {
	var result json.RawMessage
	err := a.Store.Write(ctx, func(tx *sqlite.Tx) error {
		var err error
		result, err = replay(tx, "captain-action:"+itemID, key, request, func() (json.RawMessage, error) {
			if _, err := requireActingCaptain(tx, request.CaptainID, "An ended Captain cannot act"); err != nil {
				return nil, err
			}
			row, err := tx.CurrentExecution(itemID)
			if err == sql.ErrNoRows {
				return nil, &Missing{"Work item not found"}
			}
			if err != nil {
				return nil, err
			}
			id, err := a.ID()
			if err != nil {
				return nil, err
			}
			if err := tx.InsertCaptainAction(id, row.ID, request.CaptainID, row.Version, request.Action, request.Reason, timestamp(a.Clock())); err != nil {
				return nil, err
			}
			return encoded(map[string]any{"id": id, "item_id": itemID, "execution_id": row.ID, "version": row.Version,
				"captain_id": request.CaptainID, "action": request.Action, "reason": request.Reason})
		})
		return err
	})
	return result, err
}

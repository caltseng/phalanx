package application

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"sort"
	"strings"
	"time"
	"unicode"

	"github.com/caltseng/phalanx/server/internal/contract"
	"github.com/caltseng/phalanx/server/internal/sqlite"
)

const (
	captainHeartbeatTimeout      = 15 * time.Minute
	captainHandoffGrace          = 30 * time.Minute
	captainReinforcementInterval = 90 * time.Minute
	captainHoldDuration          = 30 * time.Minute
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
	gone := heartbeat.Add(captainHeartbeatTimeout)
	if row.EndingAt != nil {
		ending, err := time.Parse(time.RFC3339Nano, *row.EndingAt)
		if err != nil {
			return time.Time{}, err
		}
		if ending.Add(captainHandoffGrace).Before(gone) {
			gone = ending.Add(captainHandoffGrace)
		}
	}
	return gone, nil
}

func liveSnapshot(tx *sqlite.Tx, now time.Time) ([]sqlite.Captain, map[string]sqlite.Captain, error) {
	rows, err := tx.AllCaptains()
	if err != nil {
		return nil, nil, err
	}
	live := map[string]sqlite.Captain{}
	for _, row := range rows {
		if row.State == "ended" {
			continue
		}
		gone, err := captainGone(row)
		if err != nil {
			return nil, nil, err
		}
		if now.Before(gone) {
			live[row.ID] = row
		}
	}
	return rows, live, nil
}

type ReinforcementView struct {
	IntervalMinutes      int64 `json:"interval_minutes"`
	Interval             int64 `json:"interval"`
	AcknowledgedInterval int64 `json:"acknowledged_interval"`
	Due                  bool  `json:"due"`
}

func reinforcement(row sqlite.Captain, now time.Time) (ReinforcementView, error) {
	created, err := time.Parse(time.RFC3339Nano, row.CreatedAt)
	if err != nil {
		return ReinforcementView{}, err
	}
	interval := max(int64(0), (now.UnixMicro()-created.UnixMicro())/int64(captainReinforcementInterval/time.Microsecond))
	return ReinforcementView{IntervalMinutes: int64(captainReinforcementInterval / time.Minute), Interval: interval,
		AcknowledgedInterval: row.ReinforcedInterval, Due: row.State == "active" && interval > row.ReinforcedInterval}, nil
}

func captainView(tx *sqlite.Tx, row sqlite.Captain, now time.Time) (any, error) {
	views, err := captainViews(tx, []sqlite.Captain{row}, now)
	if err != nil {
		return nil, err
	}
	return responseValue(views[0])
}

func captainViews(tx *sqlite.Tx, rows []sqlite.Captain, now time.Time) ([]CaptainView, error) {
	_, live, err := liveSnapshot(tx, now)
	if err != nil {
		return nil, err
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
	views := make([]CaptainView, 0, len(rows))
	for _, row := range rows {
		_, isLive := live[row.ID]
		view, err := captainViewWithContext(row, now, isLive, reports, details[row.ID])
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

type CaptainView struct {
	ID                  string            `json:"id"`
	SessionRef          string            `json:"session_ref"`
	CharterRevision     string            `json:"charter_revision"`
	Pickup              any               `json:"pickup"`
	SessionAvailability string            `json:"session_availability"`
	SessionLink         SessionLink       `json:"session_link"`
	ParentID            *string           `json:"parent_id"`
	State               string            `json:"state"`
	WakeCount           int64             `json:"wake_count"`
	CompactionCount     *int64            `json:"compaction_count"`
	Reinforcement       ReinforcementView `json:"reinforcement"`
	Focus               any               `json:"focus"`
	Sessions            any               `json:"sessions"`
	HandoffNote         *string           `json:"handoff_note"`
	CreatedAt           string            `json:"created_at"`
	HeartbeatAt         string            `json:"heartbeat_at"`
	EndingAt            *string           `json:"ending_at"`
	Live                bool              `json:"live"`
	Stale               bool              `json:"stale"`
	sqlite.CaptainDetails
	SuccessorName string `json:"successor_name"`
}

func captainViewWithContext(row sqlite.Captain, now time.Time, live bool, reports map[sessionKey]string, details sqlite.CaptainDetails) (CaptainView, error) {
	observed := reports[recordedSessionKey(row.SessionRef, "")]
	if observed == "" {
		observed = "unverified"
	}
	pickup, _, err := contract.Decode([]byte(row.PickupJSON))
	if err != nil {
		return CaptainView{}, err
	}
	focus, _, err := contract.Decode([]byte(row.FocusJSON))
	if err != nil {
		return CaptainView{}, err
	}
	sessions, _, err := contract.Decode([]byte(row.SessionsJSON))
	if err != nil {
		return CaptainView{}, err
	}
	reinforced, err := reinforcement(row, now)
	if err != nil {
		return CaptainView{}, err
	}
	short := row.ID
	if len(short) > 8 {
		short = short[:8]
	}
	return CaptainView{
		ID: row.ID, SessionRef: row.SessionRef, CharterRevision: row.CharterRevision, Pickup: pickup,
		SessionAvailability: observed, SessionLink: sessionTarget(row.SessionRef, "", "", observed, false),
		ParentID: row.ParentID, State: row.State, WakeCount: row.WakeCount, CompactionCount: row.CompactionCount,
		Reinforcement: reinforced, Focus: focus, Sessions: sessions, HandoffNote: row.HandoffNote,
		CreatedAt: row.CreatedAt, HeartbeatAt: row.HeartbeatAt, EndingAt: row.EndingAt,
		Live: live, Stale: row.State != "ended" && !live, CaptainDetails: details,
		SuccessorName: "phx captain " + short,
	}, nil
}

func (a *App) Captain(ctx context.Context, id string) (any, error) {
	var result any
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

func (a *App) Captains(ctx context.Context, limit int) (any, error) {
	var result any
	err := a.Store.Read(ctx, func(tx *sqlite.Tx) error {
		rows, err := tx.Captains(limit)
		if err != nil {
			return err
		}
		views, err := captainViews(tx, rows, a.Clock().UTC())
		if err != nil {
			return err
		}
		result, err = responseValue(map[string]any{"captains": views})
		return err
	})
	return result, err
}

func (a *App) RegisterCaptain(ctx context.Context, request contract.CaptainRegistration, key *string) (any, error) {
	var result any
	err := a.Store.Write(ctx, func(tx *sqlite.Tx) error {
		var err error
		result, err = replay(tx, "captain", key, request, func() (any, error) {
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

func (a *App) CaptainHeartbeat(ctx context.Context, id string, request contract.CaptainHeartbeat) (any, error) {
	var result any
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
			value, err := responseValue(session)
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

func (a *App) CaptainReinforcement(ctx context.Context, id string, request contract.Reinforcement) (any, error) {
	var result any
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
		if interval > max(current.Interval, row.ReinforcedInterval) {
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
		result, err = responseValue(current)
		return err
	})
	return result, err
}

func (a *App) CurrentCaptain(ctx context.Context) (any, error) {
	var result any
	err := a.Store.Read(ctx, func(tx *sqlite.Tx) error {
		now := a.Clock().UTC()
		rows, current, err := liveSnapshot(tx, now)
		if err != nil {
			return err
		}
		live := make([]sqlite.Captain, 0, len(current))
		for _, row := range rows {
			if _, ok := current[row.ID]; ok {
				live = append(live, row)
			}
		}
		sort.Slice(live, func(i, j int) bool {
			if live[i].CreatedAt != live[j].CreatedAt {
				return live[i].CreatedAt < live[j].CreatedAt
			}
			return live[i].ID < live[j].ID
		})
		type candidate struct {
			ID         string `json:"id"`
			SessionRef string `json:"session_ref"`
		}
		candidates := []candidate{}
		for _, row := range live {
			if row.State == "active" {
				candidates = append(candidates, candidate{ID: row.ID, SessionRef: row.SessionRef})
			}
		}
		var captain *candidate
		reason := "No live active Captain; ending, ended and stale registrations are not routing destinations"
		if len(candidates) > 1 {
			reason = fmt.Sprintf("%d live active Captains; no unique routing destination", len(candidates))
		} else if len(candidates) == 1 {
			reference := candidates[0].SessionRef
			if strings.TrimFunc(reference, func(r rune) bool { return unicode.IsSpace(r) || r >= 0x1c && r <= 0x1f }) == "" {
				reason = "The live active Captain has no native session binding"
			} else {
				captain = &candidates[0]
				reason = "Unique live active registration; verify its session_ref and conversation link in the runtime"
			}
		}
		result, err = responseValue(map[string]any{"captain": captain, "candidates": candidates, "reason": reason, "observed_at": timestamp(now)})
		return err
	})
	return result, err
}

func (a *App) CaptainAction(ctx context.Context, itemID string, request contract.CaptainAction, key *string) (any, error) {
	var result any
	err := a.Store.Write(ctx, func(tx *sqlite.Tx) error {
		var err error
		result, err = replay(tx, "captain-action:"+itemID, key, request, func() (any, error) {
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
			return responseValue(map[string]any{"id": id, "item_id": itemID, "execution_id": row.ID, "version": row.Version,
				"captain_id": request.CaptainID, "action": request.Action, "reason": request.Reason})
		})
		return err
	})
	return result, err
}

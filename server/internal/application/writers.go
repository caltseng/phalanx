package application

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"time"

	"github.com/caltseng/phalanx/server/internal/contract"
	"github.com/caltseng/phalanx/server/internal/sqlite"
)

func needsBuilding(stage Stage) bool { return stage != Publication && stage != Delivery }

func writerRecord(tx *sqlite.Tx, id string) (sqlite.WriterRecord, error) {
	writer, err := tx.WriterRecord(id)
	if err == sql.ErrNoRows {
		return writer, &Missing{"Writer not found"}
	}
	return writer, err
}

func buildingWait(tx *sqlite.Tx, writerID string) (*string, error) {
	capacity, err := tx.DispatchSettings()
	if err != nil {
		return nil, err
	}
	var reason string
	if capacity.Paused {
		reason = "Dispatch is paused"
	} else if capacity.BuildingWriters >= capacity.MaxBuildingWriters {
		reason = fmt.Sprintf("Building capacity exhausted: %d/%d", capacity.BuildingWriters, capacity.MaxBuildingWriters)
	} else {
		first, err := tx.FirstRework(time.Now().UnixMilli())
		if err != nil {
			return nil, err
		}
		if first != nil && *first != writerID {
			reason = "Building capacity reserved for waiting PR rework writer " + *first
		}
	}
	if reason == "" {
		return nil, nil
	}
	return &reason, nil
}

func admissionWait(tx *sqlite.Tx, stage Stage) (*string, error) {
	capacity, err := tx.DispatchSettings()
	if err != nil {
		return nil, err
	}
	var reason string
	if capacity.Paused {
		reason = "Dispatch is paused"
	} else if capacity.ActiveWriters >= capacity.MaxWriters {
		reason = fmt.Sprintf("Total capacity exhausted: max_writers=%d", capacity.MaxWriters)
	} else if needsBuilding(stage) {
		return buildingWait(tx, "")
	}
	if reason == "" {
		return nil, nil
	}
	return &reason, nil
}

func binding(tx *sqlite.Tx, writer sqlite.WriterRecord) (json.RawMessage, error) {
	execution, err := tx.Execution(writer.ItemID, writer.ExecutionID)
	if err != nil {
		return nil, err
	}
	view, err := encoded(execution)
	if err != nil {
		return nil, err
	}
	value, _, err := contract.Decode(view)
	if err != nil {
		return nil, err
	}
	body, ok := contract.ObjectValues(value)
	if !ok {
		return nil, errors.New("Execution view is not an object")
	}
	reply, err := tx.LatestReply(execution.ID)
	if err != nil {
		return nil, err
	}
	usage, err := tx.LatestUsage(writer.ID)
	if err != nil {
		return nil, err
	}
	handoff, err := tx.Handoff(execution.ID)
	if err != nil {
		return nil, err
	}
	var capacityWait *string
	if present(writer.BuildingRequestedAt) && !writer.Building {
		capacityWait, err = buildingWait(tx, writer.ID)
		if err != nil {
			return nil, err
		}
		if capacityWait == nil {
			reason := "Building capacity available; retry acquisition"
			capacityWait = &reason
		}
	}
	state := writer.State
	if present(writer.EndedAt) {
		state = "ended"
	}
	body["item_id"], body["last_reply"], body["usage"] = writer.ItemID, reply, usage
	body["writer"] = map[string]any{
		"id": writer.ID, "captain_id": writer.CaptainID, "runtime": writer.Runtime, "nonce": writer.Nonce,
		"state": state, "attempt_id": writer.AttemptID, "replaced_id": writer.ReplacedID,
		"session_id": writer.SessionID, "session_url": writer.SessionURL, "created_at": writer.CreatedAt,
		"confirmed_at": writer.ConfirmedAt, "abandoned_at": writer.AbandonedAt, "state_reported_at": writer.StateReportedAt,
		"last_progress_at": writer.LastProgressAt, "progress_note": writer.ProgressNote,
		"ended_at": writer.EndedAt, "end_reason": writer.EndReason,
		"building": writer.Building, "building_requested_at": writer.BuildingRequestedAt,
		"capacity_revision": writer.CapacityRevision, "handoff_head": handoff, "capacity_wait": capacityWait,
	}
	return encoded(body)
}

func (a *App) acquireBuilding(tx *sqlite.Tx, writer sqlite.WriterRecord) (bool, error) {
	if writer.Building {
		return true, nil
	}
	if writer.BuildingRequestedAt == nil {
		if err := tx.RequestBuilding(writer.ID, timestamp(a.Clock())); err != nil {
			return false, err
		}
	}
	wait, err := buildingWait(tx, writer.ID)
	if err != nil || wait != nil {
		return false, err
	}
	if err := tx.GrantBuilding(writer.ID); err != nil {
		return false, err
	}
	return true, nil
}

func (a *App) claimNext(tx *sqlite.Tx, writer sqlite.WriterRecord, continuedFrom *string) error {
	row, err := tx.Execution(writer.ItemID, writer.ExecutionID)
	if err != nil {
		return err
	}
	state, err := readState(row.State)
	if err != nil {
		return err
	}
	var attemptID *string
	nowMS := time.Now().UnixMilli()
	if state.Status == Queued && state.Stopping == nil && state.PendingOutput == nil &&
		row.ReadySince != nil && *row.ReadySince <= nowMS {
		acquired := true
		if needsBuilding(state.Stage) {
			acquired, err = a.acquireBuilding(tx, writer)
			if err != nil {
				return err
			}
		}
		if acquired {
			pins, unmet, err := resolveDependencies(tx, writer.ItemID)
			if err != nil {
				return err
			}
			if len(unmet) != 0 {
				if err := refreshWait(tx, writer.ItemID, writer.ExecutionID, nowMS); err != nil {
					return err
				}
			} else {
				id, err := a.ID()
				if err != nil {
					return err
				}
				if err := tx.InsertAttempt(id, row.ID, string(state.Stage), timestamp(a.Clock())); err != nil {
					return err
				}
				details := map[string]any{"writer_id": writer.ID, "ready_since_ms": row.ReadySince, "dependencies": pins}
				if continuedFrom != nil {
					details["continued_from"] = *continuedFrom
				}
				if _, err := a.apply(tx, writer.ItemID, row, state, Claim, details, DecisionOptions{}, &id, nil); err != nil {
					return err
				}
				attemptID = &id
			}
		}
	}
	return tx.SetWriterAttempt(writer.ID, attemptID)
}

func requireActingCaptain(tx *sqlite.Tx, captainID, message string) (sqlite.CaptainRecord, error) {
	captain, err := tx.CaptainRecord(captainID)
	if err == sql.ErrNoRows {
		return captain, &Missing{"Captain not found"}
	}
	if err != nil {
		return captain, err
	}
	if captain.State == "ended" {
		return captain, &Conflict{message}
	}
	return captain, nil
}

func (a *App) confirmStopped(tx *sqlite.Tx, row sqlite.Execution, state State, attemptID *string, details map[string]any) error {
	if state.Stopping == nil || (*state.Stopping != Cancelled && *state.Stopping != Failed) {
		return &Conflict{"No stop request is awaiting worker confirmation"}
	}
	original, err := tx.HistoryDetails(row.ID, row.Version)
	if err != nil {
		return err
	}
	value, _, err := contract.Decode([]byte(original))
	if err != nil {
		return err
	}
	stored, ok := contract.ObjectValues(value)
	if !ok {
		return errors.New("Stop intent details are not an object")
	}
	for key, value := range stored {
		details[key] = value
	}
	itemID, err := tx.ExecutionItem(row.ID)
	if err != nil {
		return err
	}
	_, err = a.apply(tx, itemID, row, state, StopConfirmed, details, DecisionOptions{}, attemptID, nil)
	return err
}

func (a *App) releaseWriter(tx *sqlite.Tx, writer sqlite.WriterRecord, reason string) error {
	row, err := tx.Execution(writer.ItemID, writer.ExecutionID)
	if err != nil {
		return err
	}
	state, err := readState(row.State)
	if err != nil {
		return err
	}
	attempt, err := tx.OpenAttempt(row.ID)
	if err != nil {
		return err
	}
	details := map[string]any{"reason": "Writer released: " + reason, "writer_id": writer.ID}
	if state.Stopping != nil && (attempt == nil || sameText(attempt, writer.AttemptID)) {
		if err := a.confirmStopped(tx, row, state, attempt, details); err != nil {
			return err
		}
	} else if attempt != nil && sameText(attempt, writer.AttemptID) {
		if _, err := a.apply(tx, writer.ItemID, row, state, Retry, details,
			DecisionOptions{RetryAllowed: true}, attempt, nil); err != nil {
			return err
		}
	}
	if err := tx.RelinquishBuilding(writer.ID); err != nil {
		return err
	}
	if err := tx.SetHandoff(writer.ExecutionID, nil); err != nil {
		return err
	}
	return tx.EndWriter(writer.ID, timestamp(a.Clock()), reason)
}

func (a *App) RegisterWriter(ctx context.Context, itemID, executionID string, request contract.WriterRegistration) (json.RawMessage, error) {
	var result json.RawMessage
	err := a.Store.Write(ctx, func(tx *sqlite.Tx) error {
		old, err := tx.WriterByNonce(request.Nonce)
		if err != nil {
			return err
		}
		if old != nil {
			if old.ItemID != itemID || old.ExecutionID != executionID || old.CaptainID != request.CaptainID ||
				old.Runtime != request.Runtime || old.TokenDigest != request.TokenDigest ||
				(old.ReplacedID == nil) != (request.Replaces == nil) ||
				(old.ReplacedID != nil && *old.ReplacedID != *request.Replaces) {
				return &Conflict{"This nonce was already used for a different registration"}
			}
			result, err = binding(tx, *old)
			return err
		}
		if _, err := requireActingCaptain(tx, request.CaptainID, "An ended Captain cannot act on writers"); err != nil {
			return err
		}
		settings, err := tx.DispatchSettings()
		if err != nil {
			return err
		}
		if settings.Paused {
			return &Conflict{"Dispatch is paused. No new writers are registered until it resumes"}
		}
		_, state, err := checkCurrent(tx, itemID, executionID, request.ExpectedVersion)
		if err != nil {
			return err
		}
		current, err := tx.OpenWriterRecord(executionID)
		if err != nil {
			return err
		}
		if Terminal(state.Status) || state.Stopping != nil || state.PendingOutput != nil {
			if current != nil && state.Stopping != nil {
				return &Conflict{"The execution is closing. Withdraw writer " + current.ID + " with stopped=true to confirm the stop"}
			}
			return &Conflict{"The execution is closed or closing"}
		}
		if current != nil {
			if request.Replaces == nil || *request.Replaces != current.ID {
				return &Conflict{"Execution already has writer " + current.ID + ". Name it in replaces to replace it"}
			}
			if !request.ReplacedStopped {
				return &Conflict{"Replacing a writer requires attesting that its session is stopped"}
			}
			if err := a.releaseWriter(tx, *current, "replaced"); err != nil {
				return err
			}
		} else if request.Replaces != nil {
			return &Conflict{"The writer to replace is no longer registered"}
		}
		if current == nil {
			wait, err := admissionWait(tx, state.Stage)
			if err != nil {
				return err
			}
			if wait != nil {
				active, err := tx.ActiveWriters()
				if err != nil {
					return err
				}
				names := []string{}
				for _, row := range active {
					session := "unconfirmed"
					if present(row.SessionID) {
						session = *row.SessionID
					}
					names = append(names, row.ID+" (item "+row.ItemID+", session "+session+")")
				}
				return &Conflict{"Wait: dispatch " + *wait + "; active writers: " + strings.Join(names, ", ") +
					". Retry when a writer ends; no escalation needed"}
			}
		}
		row, err := tx.Execution(itemID, executionID)
		if err != nil {
			return err
		}
		state, err = readState(row.State)
		if err != nil {
			return err
		}
		if state.Status == Running {
			return &Conflict{"An active attempt holds this execution"}
		}
		id, err := a.ID()
		if err != nil {
			return err
		}
		writer := sqlite.WriterRecord{ID: id, ExecutionID: executionID, ItemID: itemID, CaptainID: request.CaptainID,
			Runtime: request.Runtime, Nonce: request.Nonce, TokenDigest: request.TokenDigest, ReplacedID: request.Replaces,
			CreatedAt: timestamp(a.Clock()), Building: needsBuilding(state.Stage)}
		if current != nil {
			writer.Building, writer.BuildingRequestedAt = current.Building, current.BuildingRequestedAt
		}
		if err := tx.InsertWriter(writer); err != nil {
			return err
		}
		writer, err = writerRecord(tx, id)
		if err != nil {
			return err
		}
		if err := a.claimNext(tx, writer, nil); err != nil {
			return err
		}
		writer, err = writerRecord(tx, id)
		if err != nil {
			return err
		}
		result, err = binding(tx, writer)
		return err
	})
	return result, err
}

func (a *App) WriterCapacity(ctx context.Context, writerID string, request contract.WriterCapacity) (json.RawMessage, error) {
	var result json.RawMessage
	err := a.Store.Write(ctx, func(tx *sqlite.Tx) error {
		writer, err := writerRecord(tx, writerID)
		if err != nil {
			return err
		}
		if writer.EndedAt != nil {
			return &Conflict{"The writer has ended"}
		}
		if strconv.FormatInt(writer.CapacityRevision, 10) != string(request.ExpectedRevision) {
			return &Conflict{"Writer capacity changed; read status before retrying"}
		}
		execution, err := tx.Execution(writer.ItemID, writer.ExecutionID)
		if err != nil {
			return err
		}
		state, err := readState(execution.State)
		if err != nil {
			return err
		}
		if Terminal(state.Status) || state.Stopping != nil || state.PendingOutput != nil {
			return &Conflict{"The execution is closed or closing"}
		}
		if request.Phase == "delivering" {
			if needsBuilding(state.Stage) {
				return &Conflict{"Development, self-review and report work must retain Building capacity"}
			}
			if writer.Building {
				if request.HandoffHead == nil {
					return &Conflict{"Returning to delivery requires the reviewed commit's handoff_head"}
				}
				if err := tx.SetHandoff(execution.ID, request.HandoffHead); err != nil {
					return err
				}
			}
			if err := tx.RelinquishBuilding(writerID); err != nil {
				return err
			}
		} else {
			now := time.Now().UnixMilli()
			dependencyWait, err := tx.DependencyWaitPresent(execution.ID)
			if err != nil {
				return err
			}
			if state.Status == WaitingForInput || dependencyWait ||
				(execution.RetryAt != nil && *execution.RetryAt > now) ||
				(state.Status == Queued && (execution.ReadySince == nil || *execution.ReadySince > now)) {
				return &Conflict{"Resolve input, dependencies or scheduling wait before starting building work"}
			}
			if _, err := a.acquireBuilding(tx, writer); err != nil {
				return err
			}
		}
		writer, err = writerRecord(tx, writerID)
		if err != nil {
			return err
		}
		result, err = binding(tx, writer)
		return err
	})
	return result, err
}

func (a *App) WriterNext(ctx context.Context, writerID string) (json.RawMessage, error) {
	var result json.RawMessage
	err := a.Store.Write(ctx, func(tx *sqlite.Tx) error {
		writer, err := writerRecord(tx, writerID)
		if err != nil {
			return err
		}
		if writer.EndedAt != nil {
			return &Conflict{"The writer has ended"}
		}
		active := false
		if writer.AttemptID != nil {
			active, err = tx.AttemptActive(*writer.AttemptID)
			if err != nil {
				return err
			}
		}
		if !active {
			if err := a.claimNext(tx, writer, nil); err != nil {
				return err
			}
		}
		writer, err = writerRecord(tx, writerID)
		if err != nil {
			return err
		}
		result, err = binding(tx, writer)
		return err
	})
	return result, err
}

func (a *App) WriterProgress(ctx context.Context, writerID string, request contract.WriterProgress) (json.RawMessage, error) {
	var result json.RawMessage
	err := a.Store.Write(ctx, func(tx *sqlite.Tx) error {
		if err := tx.Progress(writerID, timestamp(a.Clock()), request.Note); err != nil {
			return err
		}
		writer, err := writerRecord(tx, writerID)
		if err != nil {
			return err
		}
		result, err = binding(tx, writer)
		return err
	})
	return result, err
}

func boundWriter(tx *sqlite.Tx, itemID, executionID, writerID string) (sqlite.WriterRecord, error) {
	writer, err := writerRecord(tx, writerID)
	if err != nil {
		return writer, err
	}
	if writer.ItemID != itemID || writer.ExecutionID != executionID {
		return writer, &Missing{"Writer not found"}
	}
	return writer, nil
}

func authorizeWriter(tx *sqlite.Tx, writer sqlite.WriterRecord, captainID string) (string, error) {
	if _, err := requireActingCaptain(tx, captainID, "An ended Captain cannot act on writers"); err != nil {
		return "", err
	}
	if writer.CaptainID == captainID || writer.ConfirmedAt != nil {
		return writer.CaptainID, nil
	}
	owner, err := tx.CaptainRecord(writer.CaptainID)
	if err == sql.ErrNoRows {
		return "", &Missing{"Captain not found"}
	}
	if err != nil {
		return "", err
	}
	if writer.AbandonedAt == nil && owner.State != "ended" {
		heartbeat, err := time.Parse(time.RFC3339Nano, owner.HeartbeatAt)
		if err != nil {
			return "", err
		}
		gone := heartbeat.Add(15 * time.Minute)
		if owner.EndingAt != nil {
			ending, err := time.Parse(time.RFC3339Nano, *owner.EndingAt)
			if err != nil {
				return "", err
			}
			if ending.Add(30 * time.Minute).Before(gone) {
				gone = ending.Add(30 * time.Minute)
			}
		}
		if time.Now().Before(gone) {
			return "", &Conflict{"Captain " + owner.ID + " owns this unconfirmed intent"}
		}
	}
	return captainID, nil
}

func (a *App) UpdateWriter(ctx context.Context, itemID, executionID string, request contract.WriterUpdate) (json.RawMessage, error) {
	var result json.RawMessage
	err := a.Store.Write(ctx, func(tx *sqlite.Tx) error {
		writer, err := boundWriter(tx, itemID, executionID, request.WriterID)
		if err != nil {
			return err
		}
		if writer.EndedAt != nil {
			return &Conflict{"The writer has ended"}
		}
		owner, err := authorizeWriter(tx, writer, request.CaptainID)
		if err != nil {
			return err
		}
		if request.Abandon && request.CaptainID != writer.CaptainID {
			return &Conflict{"Only the owning Captain abandons its intent"}
		}
		if request.SessionID == nil && request.SessionURL != nil && writer.SessionID == nil {
			return &Conflict{"Confirm the native session ID with its link"}
		}
		if request.SessionID != nil && writer.SessionID != nil && *writer.SessionID != *request.SessionID {
			return &Conflict{"The writer is already confirmed with another native session"}
		}
		confirming := request.SessionID != nil && writer.SessionID == nil
		state := writer.State
		if request.State != nil {
			state = *request.State
		} else if confirming {
			state = "busy"
		}
		if err := tx.ConfirmWriter(writer.ID, owner, state, timestamp(a.Clock()), request.SessionID, request.SessionURL,
			confirming, request.Abandon, request.State != nil); err != nil {
			return err
		}
		writer, err = writerRecord(tx, writer.ID)
		if err != nil {
			return err
		}
		result, err = binding(tx, writer)
		return err
	})
	return result, err
}

func (a *App) WithdrawWriter(ctx context.Context, itemID, executionID string, request contract.WriterWithdrawal) (json.RawMessage, error) {
	var result json.RawMessage
	err := a.Store.Write(ctx, func(tx *sqlite.Tx) error {
		writer, err := boundWriter(tx, itemID, executionID, request.WriterID)
		if err != nil {
			return err
		}
		if writer.EndedAt == nil {
			if _, err := authorizeWriter(tx, writer, request.CaptainID); err != nil {
				return err
			}
			if writer.ConfirmedAt != nil && !request.Stopped {
				return &Conflict{"Withdrawing a confirmed writer requires attesting that its session is stopped"}
			}
			if err := a.releaseWriter(tx, writer, "withdrawn by Captain "+request.CaptainID+": "+request.Reason); err != nil {
				return err
			}
			writer, err = writerRecord(tx, writer.ID)
			if err != nil {
				return err
			}
		}
		result, err = binding(tx, writer)
		return err
	})
	return result, err
}

package application

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"math/big"
	"time"

	"github.com/caltseng/phalanx/server/internal/contract"
	"github.com/caltseng/phalanx/server/internal/sqlite"
)

func usageJSON(raw *string) (any, map[string]any, error) {
	if !present(raw) {
		return nil, nil, nil
	}
	value, _, err := contract.Decode([]byte(*raw))
	if err != nil {
		return nil, nil, err
	}
	if value == nil {
		return nil, nil, nil
	}
	body, ok := contract.ObjectValues(value)
	if !ok {
		return nil, nil, errors.New("Stored usage snapshot is not an object")
	}
	return value, body, nil
}

func usageField(body map[string]any, name string) (any, error) {
	value, exists := body[name]
	if !exists {
		return nil, fmt.Errorf("Stored usage snapshot is missing %s", name)
	}
	return value, nil
}

func usageText(body map[string]any, name string) (string, error) {
	value, err := usageField(body, name)
	if err != nil {
		return "", err
	}
	text, ok := value.(string)
	if !ok {
		return "", fmt.Errorf("Stored usage %s is not text", name)
	}
	return text, nil
}

func usageDate(body map[string]any, name string) (time.Time, error) {
	text, err := usageText(body, name)
	if err != nil {
		return time.Time{}, err
	}
	return time.Parse(time.RFC3339Nano, text)
}

func usageObject(body map[string]any, name string) (map[string]any, error) {
	value, err := usageField(body, name)
	if err != nil {
		return nil, err
	}
	object, ok := contract.ObjectValues(value)
	if !ok {
		return nil, fmt.Errorf("Stored usage %s is not an object", name)
	}
	return object, nil
}

func usageNumber(value any) (*big.Rat, error) {
	number, ok := value.(json.Number)
	if !ok {
		return nil, errors.New("Stored usage counter is not numeric")
	}
	text, err := contract.Canonical(number)
	if err != nil {
		return nil, err
	}
	result, ok := new(big.Rat).SetString(text)
	if !ok {
		return nil, errors.New("Stored usage counter is not finite")
	}
	return result, nil
}

func equalUsageValue(first, second any) (bool, error) {
	a, err := contract.Canonical(first)
	if err != nil {
		return false, err
	}
	b, err := contract.Canonical(second)
	return a == b, err
}

func (a *App) observeUsage(tx *sqlite.Tx, writer sqlite.WriterRecord, snapshot contract.UsageSnapshot) (json.RawMessage, error) {
	if !present(writer.ConfirmedAt) || writer.Runtime != snapshot.Runtime ||
		writer.SessionID == nil || *writer.SessionID != snapshot.SessionID {
		return nil, &Conflict{"Usage requires this writer's confirmed runtime and native session"}
	}
	if snapshot.Scope == "item_exclusive" {
		created, err := time.Parse(time.RFC3339Nano, writer.CreatedAt)
		if err != nil {
			return nil, err
		}
		if snapshot.WindowStart.Before(created) {
			return nil, &Conflict{"Item-exclusive window cannot predate writer allocation"}
		}
	}
	if present(writer.EndedAt) {
		ended, err := time.Parse(time.RFC3339Nano, *writer.EndedAt)
		if err != nil {
			return nil, err
		}
		if snapshot.Cutoff.After(ended) {
			return nil, &Conflict{"Late observation must retain a cutoff within ended writer ownership"}
		}
	}
	raw, err := contract.PythonJSON(snapshot)
	if err != nil {
		return nil, err
	}
	snapshotValue, _, err := contract.Decode([]byte(raw))
	if err != nil {
		return nil, err
	}
	current, ok := contract.ObjectValues(snapshotValue)
	if !ok {
		return nil, errors.New("Usage snapshot is not an object")
	}
	existing, err := tx.UsageRecord(writer.ID)
	if err != nil {
		return nil, err
	}
	var previousValue, failureValue any
	var previous, latest map[string]any
	sequence, err := snapshot.Sequence.Int64()
	if err != nil {
		return nil, err
	}
	if existing != nil {
		previousValue, previous, err = usageJSON(existing.SnapshotJSON)
		if err != nil {
			return nil, err
		}
		failureValue, latest, err = usageJSON(existing.FailureJSON)
		if err != nil {
			return nil, err
		}
		latestValue := failureValue
		if len(latest) == 0 {
			latest, latestValue = previous, previousValue
		}
		if len(latest) != 0 {
			if sequence <= existing.Sequence {
				equal, err := equalUsageValue(snapshotValue, latestValue)
				if err != nil {
					return nil, err
				}
				if equal {
					return encoded(map[string]any{"accepted": false, "reason": "Identical cumulative snapshot already observed"})
				}
				return nil, &Conflict{"Stale or conflicting usage sequence; read the latest observation"}
			}
			observed, err := time.Parse(time.RFC3339Nano, existing.ObservedAt)
			if err != nil {
				return nil, err
			}
			if snapshot.ObservedAt.Before(observed) {
				return nil, &Conflict{"Out-of-order usage observation"}
			}
			for _, name := range []string{"runtime", "provider", "account", "origin", "session_id", "source", "epoch", "model", "window_start", "scope"} {
				old, err := usageField(latest, name)
				if err != nil {
					return nil, err
				}
				equal, err := equalUsageValue(current[name], old)
				if err != nil {
					return nil, err
				}
				if !equal {
					return nil, &Conflict{"Usage series changed; preserve its original attribution and epoch"}
				}
			}
		}
	}
	if len(previous) != 0 {
		cutoff, err := usageDate(previous, "cutoff")
		if err != nil {
			return nil, err
		}
		if snapshot.Cutoff.Before(cutoff) {
			return nil, &Conflict{"Usage cutoff moved backwards"}
		}
		if snapshot.Status != "unavailable" {
			tokens, err := usageObject(previous, "tokens")
			if err != nil {
				return nil, err
			}
			currentTokens := snapshot.Tokens.Fields()
			for _, name := range contract.TokenFields {
				old, err := usageField(tokens, name)
				if err != nil {
					return nil, err
				}
				if old == nil {
					continue
				}
				oldNumber, err := usageNumber(old)
				if err != nil {
					return nil, err
				}
				next := currentTokens[name]
				if next == nil {
					return nil, &Conflict{"Cumulative counters decreased or disappeared; report a coverage gap"}
				}
				newNumber, err := usageNumber(*next)
				if err != nil {
					return nil, err
				}
				if newNumber.Cmp(oldNumber) < 0 {
					return nil, &Conflict{"Cumulative counters decreased or disappeared; report a coverage gap"}
				}
			}
			for _, name := range []string{"credits", "money"} {
				old, err := usageField(previous, name)
				if err != nil {
					return nil, err
				}
				if old == nil {
					continue
				}
				oldQuantity, ok := contract.ObjectValues(old)
				if !ok {
					return nil, errors.New("Stored cumulative usage quantity is not an object")
				}
				next := snapshot.Credits
				if name == "money" {
					next = snapshot.Money
				}
				if next == nil {
					return nil, &Conflict{"Cumulative quantity changed units, basis or decreased"}
				}
				values := []string{next.Unit, next.Basis, next.Source}
				for index, field := range []string{"unit", "basis", "source"} {
					previous, err := usageText(oldQuantity, field)
					if err != nil {
						return nil, err
					}
					if previous != values[index] {
						return nil, &Conflict{"Cumulative quantity changed units, basis or decreased"}
					}
				}
				amount, err := usageText(oldQuantity, "amount")
				if err != nil {
					return nil, err
				}
				oldAmount, oldOK := new(big.Rat).SetString(amount)
				newAmount, newOK := new(big.Rat).SetString(next.Amount)
				if !oldOK || !newOK {
					return nil, errors.New("Stored usage quantity amount is not decimal")
				}
				if newAmount.Cmp(oldAmount) < 0 {
					return nil, &Conflict{"Cumulative quantity changed units, basis or decreased"}
				}
			}
		}
	}
	var snapshotJSON, failureJSON *string
	if snapshot.Status == "unavailable" {
		failureJSON = &raw
		if len(previous) != 0 {
			text, err := contract.PythonJSON(previousValue)
			if err != nil {
				return nil, err
			}
			snapshotJSON = &text
		}
	} else {
		snapshotJSON = &raw
	}
	stamp, err := usageText(current, "observed_at")
	if err != nil {
		return nil, err
	}
	if err := tx.SaveUsage(writer.ID, sequence, stamp, snapshotJSON, failureJSON); err != nil {
		return nil, err
	}
	return encoded(map[string]any{"accepted": true, "status": snapshot.Status})
}

func (a *App) OwnUsage(ctx context.Context, writerID string, snapshot contract.UsageSnapshot) (json.RawMessage, error) {
	var result json.RawMessage
	err := a.Store.Write(ctx, func(tx *sqlite.Tx) error {
		writer, err := writerRecord(tx, writerID)
		if err != nil {
			return err
		}
		if present(writer.EndedAt) {
			return &Conflict{"The writer has ended"}
		}
		result, err = a.observeUsage(tx, writer, snapshot)
		return err
	})
	return result, err
}

func (a *App) CaptainUsage(ctx context.Context, itemID, executionID, writerID string, request contract.CaptainUsage) (json.RawMessage, error) {
	var result json.RawMessage
	err := a.Store.Write(ctx, func(tx *sqlite.Tx) error {
		writer, err := boundWriter(tx, itemID, executionID, writerID)
		if err != nil {
			return err
		}
		if _, err := requireActingCaptain(tx, request.CaptainID, "An ended Captain cannot act on writers"); err != nil {
			return err
		}
		captain, err := captainRow(tx, request.CaptainID)
		if err != nil {
			return err
		}
		pickup, _, err := contract.Decode([]byte(captain.PickupJSON))
		if err != nil {
			return err
		}
		body, ok := contract.ObjectValues(pickup)
		if !ok {
			return errors.New("Stored Captain pickup is not an object")
		}
		repositories, ok := contract.ObjectValues(body["repositories"])
		if !ok {
			return errors.New("Stored Captain repositories are not an object")
		}
		item, err := tx.Item(itemID)
		if err != nil {
			return err
		}
		if _, allowed := repositories[item.RepositoryID]; !allowed {
			return &Forbidden{"Usage observation is outside this Captain's repository scope"}
		}
		result, err = a.observeUsage(tx, writer, request.Snapshot)
		return err
	})
	return result, err
}

type usageSession struct {
	WriterID         string  `json:"writer_id"`
	ExecutionID      string  `json:"execution_id"`
	Runtime          string  `json:"runtime"`
	SessionID        *string `json:"session_id"`
	EndedAt          *string `json:"ended_at"`
	Included         bool    `json:"included"`
	Status           string  `json:"status"`
	Reason           string  `json:"reason"`
	Snapshot         any     `json:"snapshot"`
	AcquisitionError any     `json:"acquisition_error"`
}

func projectedUsage(row sqlite.UsageProjectionRow) (usageSession, error) {
	snapshotValue, snapshot, err := usageJSON(row.SnapshotJSON)
	if err != nil {
		return usageSession{}, err
	}
	failureValue, failure, err := usageJSON(row.FailureJSON)
	if err != nil {
		return usageSession{}, err
	}
	result := usageSession{WriterID: row.WriterID, ExecutionID: row.ExecutionID, Runtime: row.Runtime,
		SessionID: row.SessionID, EndedAt: row.EndedAt, Status: "unavailable",
		Reason:   "No authorized native usage observation; retry at an ordinary runtime observation",
		Snapshot: snapshotValue, AcquisitionError: failureValue}
	if len(snapshot) != 0 {
		scope, err := usageText(snapshot, "scope")
		if err != nil {
			return result, err
		}
		result.Included = scope == "item_exclusive" && row.Bindings == 1
		result.Status, err = usageText(snapshot, "status")
		if err != nil {
			return result, err
		}
	}
	reason := snapshot
	if len(reason) == 0 {
		reason = failure
	}
	if len(reason) != 0 {
		result.Reason, err = usageText(reason, "reason")
		if err != nil {
			return result, err
		}
	}
	if len(snapshot) != 0 && result.Status == "observed" {
		pending := !present(row.EndedAt)
		if !pending {
			ended, err := time.Parse(time.RFC3339Nano, *row.EndedAt)
			if err != nil {
				return result, err
			}
			cutoff, err := usageDate(snapshot, "cutoff")
			if err != nil {
				return result, err
			}
			pending = cutoff.Before(ended)
		}
		if pending {
			result.Status = "pending_final"
			result.Reason += "; final ownership boundary not observed"
		}
	}
	if len(snapshot) != 0 && (len(failure) != 0 || !result.Included) {
		result.Status = "partial"
		result.Reason = "Session lifetime or reused native session; excluded from attributable subtotal"
		if len(failure) != 0 {
			result.Reason, err = usageText(failure, "reason")
			if err != nil {
				return result, err
			}
		}
	}
	return result, nil
}

func usageSubtotal(sessions []usageSession) (map[string]any, error) {
	included := []usageSession{}
	statuses := map[string]bool{}
	for _, row := range sessions {
		if row.Included {
			included = append(included, row)
		}
		statuses[row.Status] = true
	}
	tokens, covered := map[string]any{}, map[string]int{}
	for _, name := range contract.TokenFields {
		total := new(big.Int)
		count := 0
		for _, row := range included {
			snapshot, ok := contract.ObjectValues(row.Snapshot)
			if !ok {
				return nil, errors.New("Included usage snapshot is not an object")
			}
			fields, err := usageObject(snapshot, "tokens")
			if err != nil {
				return nil, err
			}
			value, err := usageField(fields, name)
			if err != nil {
				return nil, err
			}
			if value == nil {
				continue
			}
			number, ok := value.(json.Number)
			if !ok {
				return nil, errors.New("Included usage counter is not numeric")
			}
			integer, ok := new(big.Int).SetString(string(number), 10)
			if !ok {
				return nil, errors.New("Included usage counter is not an integer")
			}
			total.Add(total, integer)
			count++
		}
		var value any
		if count != 0 {
			value = json.Number(total.String())
			if total.Cmp(big.NewInt(contract.MaxUsageCounter)) > 0 {
				value = total.String()
			}
		}
		tokens[name], covered[name] = value, count
	}
	status := "partial"
	if len(included) == 0 {
		status = "unavailable"
	} else if len(statuses) == 1 && statuses["pending_final"] {
		status = "pending_final"
	} else if len(statuses) == 1 && statuses["observed"] && len(included) == len(sessions) {
		status = "observed"
	}
	return map[string]any{"tokens": tokens, "field_sessions": covered, "covered_sessions": len(included),
		"bound_sessions": len(sessions), "status": status}, nil
}

func usageProjection(tx *sqlite.Tx, itemID, executionID string) (map[string]any, error) {
	rows, err := tx.UsageProjection(itemID)
	if err != nil {
		return nil, err
	}
	sessions, current := []usageSession{}, []usageSession{}
	for _, row := range rows {
		session, err := projectedUsage(row)
		if err != nil {
			return nil, err
		}
		sessions = append(sessions, session)
		if row.ExecutionID == executionID {
			current = append(current, session)
		}
	}
	execution, err := usageSubtotal(current)
	if err != nil {
		return nil, err
	}
	item, err := usageSubtotal(sessions)
	if err != nil {
		return nil, err
	}
	return map[string]any{
		"execution": execution, "item": item, "sessions": sessions,
		"overhead": "Captain/shared reviewer usage unallocated; not included",
		"credits":  "Unavailable unless explicitly supplied by an attributable provider source; never inferred from tokens",
		"capabilities": map[string]string{
			"copilot-app": "Read-only native session metadata transport; no automatic CLI/SDK attachment or credit feed",
			"codex-app":   "Automatic App-bound export unverified; authorized native observation required",
		},
	}, nil
}

package application

import (
	"context"
	"encoding/json"
	"errors"
	"math/big"
	"time"

	"github.com/caltseng/phalanx/server/internal/contract"
	"github.com/caltseng/phalanx/server/internal/sqlite"
)

func usageJSON(raw *string) (*contract.UsageSnapshot, error) {
	if !present(raw) {
		return nil, nil
	}
	var snapshot *contract.UsageSnapshot
	err := json.Unmarshal([]byte(*raw), &snapshot)
	return snapshot, err
}

func sameUsageSeries(first, second contract.UsageSnapshot) bool {
	return first.Runtime == second.Runtime && first.Provider == second.Provider && first.Account == second.Account &&
		first.Origin == second.Origin && first.SessionID == second.SessionID && first.Source == second.Source &&
		first.Epoch == second.Epoch && (first.Model == nil && second.Model == nil || sameText(first.Model, second.Model)) &&
		first.Scope == second.Scope &&
		first.WindowStart.Truncate(time.Microsecond).Format(time.RFC3339Nano) ==
			second.WindowStart.Truncate(time.Microsecond).Format(time.RFC3339Nano)
}

func usageCountersDecreased(previous, current contract.Tokens) (bool, error) {
	next := current.Fields()
	for name, old := range previous.Fields() {
		if old == nil {
			continue
		}
		if next[name] == nil {
			return true, nil
		}
		first, err := old.Int64()
		if err != nil {
			return false, err
		}
		second, err := next[name].Int64()
		if err != nil {
			return false, err
		}
		if second < first {
			return true, nil
		}
	}
	return false, nil
}

func usageQuantityDecreased(previous, current *contract.Quantity) (bool, error) {
	if previous == nil {
		return false, nil
	}
	if current == nil || previous.Unit != current.Unit || previous.Basis != current.Basis || previous.Source != current.Source {
		return true, nil
	}
	first, firstOK := new(big.Rat).SetString(previous.Amount)
	second, secondOK := new(big.Rat).SetString(current.Amount)
	if !firstOK || !secondOK {
		return false, errors.New("Stored usage quantity amount is not decimal")
	}
	return second.Cmp(first) < 0, nil
}

func (a *App) observeUsage(tx *sqlite.Tx, writer sqlite.WriterRecord, snapshot contract.UsageSnapshot) (any, error) {
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
	existing, err := tx.UsageRecord(writer.ID)
	if err != nil {
		return nil, err
	}
	var previous *contract.UsageSnapshot
	sequence, err := snapshot.Sequence.Int64()
	if err != nil {
		return nil, err
	}
	if existing != nil {
		previous, err = usageJSON(existing.SnapshotJSON)
		if err != nil {
			return nil, err
		}
		latest, err := usageJSON(existing.FailureJSON)
		if err != nil {
			return nil, err
		}
		if latest == nil {
			latest = previous
		}
		if latest != nil {
			if sequence <= existing.Sequence {
				first, err := contract.Canonical(snapshot)
				if err != nil {
					return nil, err
				}
				second, err := contract.Canonical(latest)
				if err != nil {
					return nil, err
				}
				if first == second {
					return responseValue(map[string]any{"accepted": false, "reason": "Identical cumulative snapshot already observed"})
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
			if !sameUsageSeries(snapshot, *latest) {
				return nil, &Conflict{"Usage series changed; preserve its original attribution and epoch"}
			}
		}
	}
	if previous != nil {
		if snapshot.Cutoff.Before(previous.Cutoff.Time) {
			return nil, &Conflict{"Usage cutoff moved backwards"}
		}
		if snapshot.Status != "unavailable" {
			decreased, err := usageCountersDecreased(previous.Tokens, snapshot.Tokens)
			if err != nil {
				return nil, err
			}
			if decreased {
				return nil, &Conflict{"Cumulative counters decreased or disappeared; report a coverage gap"}
			}
			for _, pair := range [][2]*contract.Quantity{{previous.Credits, snapshot.Credits}, {previous.Money, snapshot.Money}} {
				decreased, err := usageQuantityDecreased(pair[0], pair[1])
				if err != nil {
					return nil, err
				}
				if decreased {
					return nil, &Conflict{"Cumulative quantity changed units, basis or decreased"}
				}
			}
		}
	}
	var snapshotJSON, failureJSON *string
	if snapshot.Status == "unavailable" {
		failureJSON = &raw
		if previous != nil {
			snapshotJSON = existing.SnapshotJSON
		}
	} else {
		snapshotJSON = &raw
	}
	stamp, err := snapshot.ObservedAt.JSONValue()
	if err != nil {
		return nil, err
	}
	if err := tx.SaveUsage(writer.ID, sequence, stamp.(string), snapshotJSON, failureJSON); err != nil {
		return nil, err
	}
	return responseValue(map[string]any{"accepted": true, "status": snapshot.Status})
}

func (a *App) OwnUsage(ctx context.Context, writerID string, snapshot contract.UsageSnapshot) (any, error) {
	var result any
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

func (a *App) CaptainUsage(ctx context.Context, itemID, executionID, writerID string, request contract.CaptainUsage) (any, error) {
	var result any
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
		scope, err := captainScope(captain)
		if err != nil {
			return err
		}
		item, err := tx.Item(itemID)
		if err != nil {
			return err
		}
		if _, allowed := scope[item.RepositoryID]; !allowed {
			return &Forbidden{"Usage observation is outside this Captain's repository scope"}
		}
		result, err = a.observeUsage(tx, writer, request.Snapshot)
		return err
	})
	return result, err
}

package application

import (
	"encoding/json"
	"errors"
	"time"

	"github.com/caltseng/phalanx/server/internal/contract"
	"github.com/caltseng/phalanx/server/internal/sqlite"
)

type usageSession struct {
	WriterID         string                  `json:"writer_id"`
	ExecutionID      string                  `json:"execution_id"`
	Runtime          string                  `json:"runtime"`
	SessionID        *string                 `json:"session_id"`
	EndedAt          *string                 `json:"ended_at"`
	Included         bool                    `json:"included"`
	Status           string                  `json:"status"`
	Reason           string                  `json:"reason"`
	Snapshot         *contract.UsageSnapshot `json:"snapshot"`
	AcquisitionError *contract.UsageSnapshot `json:"acquisition_error"`
}

func projectedUsage(row sqlite.UsageProjectionRow) (usageSession, error) {
	snapshot, err := usageJSON(row.SnapshotJSON)
	if err != nil {
		return usageSession{}, err
	}
	failure, err := usageJSON(row.FailureJSON)
	if err != nil {
		return usageSession{}, err
	}
	result := usageSession{WriterID: row.WriterID, ExecutionID: row.ExecutionID, Runtime: row.Runtime,
		SessionID: row.SessionID, EndedAt: row.EndedAt, Status: "unavailable",
		Reason:   "No authorized native usage observation; retry at an ordinary runtime observation",
		Snapshot: snapshot, AcquisitionError: failure}
	if snapshot != nil {
		result.Included = snapshot.Scope == "item_exclusive" && row.Bindings == 1
		result.Status, result.Reason = snapshot.Status, snapshot.Reason
	} else if failure != nil {
		result.Reason = failure.Reason
	}
	if snapshot != nil && result.Status == "observed" {
		pending := !present(row.EndedAt)
		if !pending {
			ended, err := time.Parse(time.RFC3339Nano, *row.EndedAt)
			if err != nil {
				return result, err
			}
			pending = snapshot.Cutoff.Before(ended)
		}
		if pending {
			result.Status = "pending_final"
			result.Reason += "; final ownership boundary not observed"
		}
	}
	if snapshot != nil && (failure != nil || !result.Included) {
		result.Status, result.Reason = "partial", "Session lifetime or reused native session; excluded from attributable subtotal"
		if failure != nil {
			result.Reason = failure.Reason
		}
	}
	return result, nil
}

func usageSubtotal(sessions []usageSession) (map[string]any, error) {
	fields, covered, statuses := map[string][]json.Number{}, map[string]int{}, map[string]bool{}
	included := 0
	for _, row := range sessions {
		statuses[row.Status] = true
		if !row.Included {
			continue
		}
		if row.Snapshot == nil {
			return nil, errors.New("Included usage snapshot is not an object")
		}
		included++
		for name, value := range row.Snapshot.Tokens.Fields() {
			if value != nil {
				fields[name] = append(fields[name], *value)
			}
		}
	}
	tokens := map[string]any{}
	for _, name := range contract.TokenFields {
		total, err := contract.SumUsageCounters(fields[name])
		if err != nil {
			return nil, err
		}
		tokens[name], covered[name] = total, len(fields[name])
	}
	status := "partial"
	if included == 0 {
		status = "unavailable"
	} else if len(statuses) == 1 && statuses["pending_final"] {
		status = "pending_final"
	} else if len(statuses) == 1 && statuses["observed"] && included == len(sessions) {
		status = "observed"
	}
	return map[string]any{"tokens": tokens, "field_sessions": covered, "covered_sessions": included,
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
	return map[string]any{"execution": execution, "item": item, "sessions": sessions,
		"overhead": "Captain/shared reviewer usage unallocated; not included",
		"credits":  "Unavailable unless explicitly supplied by an attributable provider source; never inferred from tokens",
		"capabilities": map[string]string{
			"copilot-app": "Read-only native session metadata transport; no automatic CLI/SDK attachment or credit feed",
			"codex-app":   "Automatic App-bound export unverified; authorized native observation required",
		}}, nil
}

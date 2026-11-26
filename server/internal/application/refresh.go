package application

import (
	"context"
	"database/sql"
	"time"

	"github.com/caltseng/phalanx/server/internal/contract"
	"github.com/caltseng/phalanx/server/internal/sqlite"
)

const refreshBatchSize = 32

func milestoneEvidence(state State) any {
	if state.Stopping != nil {
		return *state.Stopping
	}
	if state.PendingOutput != nil {
		return *state.PendingOutput
	}
	if state.Status == Failed || state.Status == Cancelled {
		return state.Status
	}
	if state.Status == Completed {
		return state.FinalOutput
	}
	if state.Stage == Delivery && state.ReadyRevision != nil && state.Candidate != nil &&
		state.ReadyRevision.Head == *state.Candidate && present(state.PrimaryPR) {
		return []any{state.PrimaryPR, state.ReadyRevision}
	}
	return nil
}

func refreshWait(tx *sqlite.Tx, itemID, executionID string, nowMS int64) error {
	unmet, err := unmetDependencies(tx, itemID)
	if err != nil {
		return err
	}
	var wait *string
	if len(unmet) > 0 {
		text, err := pythonOrderedJSON(unmet)
		if err != nil {
			return err
		}
		wait = &text
	}
	return tx.UpdateDependencyWait(executionID, wait, nowMS)
}

func refreshItems(tx *sqlite.Tx, items []string, nowMS int64) (*string, error) {
	limit := min(len(items), refreshBatchSize)
	for _, itemID := range items[:limit] {
		executionID, err := tx.QueuedExecution(itemID)
		if err == sql.ErrNoRows {
			continue
		}
		if err != nil {
			return nil, err
		}
		if err := refreshWait(tx, itemID, executionID, nowMS); err != nil {
			return nil, err
		}
	}
	if len(items) > refreshBatchSize {
		return &items[refreshBatchSize-1], nil
	}
	return nil, nil
}

func refreshDependents(tx *sqlite.Tx, upstreamID, cursor string, now time.Time) error {
	items, err := tx.DependentItems(upstreamID, cursor, refreshBatchSize+1)
	if err != nil {
		return err
	}
	next, err := refreshItems(tx, items, now.UnixMilli())
	if err != nil {
		return err
	}
	return tx.ScheduleDependencyRefresh(upstreamID, next, time.Now().UnixNano())
}

func auditBatch(tx *sqlite.Tx, nowMS int64) error {
	cursor, err := tx.DependencyAuditCursor()
	if err != nil || cursor == nil {
		return err
	}
	items, err := tx.DependencyAuditItems(*cursor, refreshBatchSize+1)
	if err != nil {
		return err
	}
	next, err := refreshItems(tx, items, nowMS)
	if err != nil {
		return err
	}
	return tx.SetDependencyAuditCursor(next)
}

func (a *App) StartReconciliation(ctx context.Context) error {
	return a.Store.Write(ctx, func(tx *sqlite.Tx) error {
		if err := tx.StartDependencyAudit(); err != nil {
			return err
		}
		return auditBatch(tx, time.Now().UnixMilli())
	})
}

func (a *App) ProcessRefresh(ctx context.Context) (bool, error) {
	var jobs, audit bool
	err := a.Store.Read(ctx, func(tx *sqlite.Tx) error {
		var err error
		jobs, audit, err = tx.HasDependencyJobs()
		return err
	})
	if err != nil {
		return false, err
	}
	if jobs {
		err = a.Store.Write(ctx, func(tx *sqlite.Tx) error {
			upstreamID, cursor, err := tx.FirstDependencyJob()
			if err != nil || upstreamID == "" {
				return err
			}
			return refreshDependents(tx, upstreamID, cursor, time.Now())
		})
		if err != nil {
			return false, err
		}
	}
	if audit {
		err = a.Store.Write(ctx, func(tx *sqlite.Tx) error { return auditBatch(tx, time.Now().UnixMilli()) })
		if err != nil {
			return false, err
		}
	}
	return jobs || audit, nil
}

func evidenceChanged(before, after State) (bool, error) {
	first, err := contract.Canonical(milestoneEvidence(before))
	if err != nil {
		return false, err
	}
	second, err := contract.Canonical(milestoneEvidence(after))
	return first != second, err
}

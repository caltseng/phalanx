package sqlite

import (
	"errors"
	"fmt"
	"strings"
)

func (tx *Tx) UpdateExecution(id string, version int64, state, now string, readySince, retryAt *int64, retryCount int64) error {
	result, err := tx.conn.ExecContext(tx.ctx, `UPDATE executions SET version=?, state_json=?, updated_at=?,
		ready_since=?, retry_at=?, retry_count=?, dependency_wait_json=NULL WHERE id=? AND version=?`,
		version+1, state, now, readySince, retryAt, retryCount, id, version)
	if err != nil {
		return err
	}
	count, err := result.RowsAffected()
	if err != nil {
		return err
	}
	if count != 1 {
		return errors.New("Execution update lost its transaction-local version fence")
	}
	return nil
}

func (tx *Tx) RecordHistory(id string, version int64, attemptID *string, event, details, state, now string) error {
	_, err := tx.conn.ExecContext(tx.ctx, "INSERT INTO transition_history VALUES (?, ?, ?, ?, ?, ?, ?)",
		id, version, attemptID, event, details, state, now)
	return err
}

func (tx *Tx) HistoryDetails(id string, version int64) (string, error) {
	var result string
	err := tx.conn.QueryRowContext(tx.ctx,
		"SELECT details_json FROM transition_history WHERE execution_id=? AND version=?", id, version).Scan(&result)
	return result, err
}

func (tx *Tx) HasOutstandingWork(executionID string) (bool, error) {
	var found bool
	err := tx.conn.QueryRowContext(tx.ctx, `SELECT EXISTS (SELECT 1 FROM attempts WHERE execution_id=? AND ended_at IS NULL)
		OR EXISTS (SELECT 1 FROM writers WHERE execution_id=? AND ended_at IS NULL)`, executionID, executionID).Scan(&found)
	return found, err
}

func (tx *Tx) EndWriters(executionID, status, now string) error {
	if _, err := tx.conn.ExecContext(tx.ctx, "UPDATE executions SET handoff_head=NULL WHERE id=?", executionID); err != nil {
		return err
	}
	_, err := tx.conn.ExecContext(tx.ctx, `UPDATE writers SET ended_at=?, end_reason=?, attempt_id=NULL,
		building=0, building_requested_at=NULL, capacity_revision=capacity_revision+1
		WHERE execution_id=? AND ended_at IS NULL`, now, "closed as "+status, executionID)
	return err
}

func (tx *Tx) EnqueueFinalization(executionID string, version int64) error {
	_, err := tx.conn.ExecContext(tx.ctx,
		"INSERT INTO report_finalizations(execution_id, intent_version, due_at) VALUES (?, ?, 0)", executionID, version)
	return err
}

type FinalizationJob struct {
	ExecutionID   string
	IntentVersion int64
	Failures      int64
}

func (tx *Tx) DueFinalization(executionID string, now int64) (FinalizationJob, error) {
	var job FinalizationJob
	err := tx.conn.QueryRowContext(tx.ctx, `SELECT execution_id, intent_version, failures
		FROM report_finalizations WHERE execution_id=? AND due_at<=?`, executionID, now).
		Scan(&job.ExecutionID, &job.IntentVersion, &job.Failures)
	return job, err
}

func (tx *Tx) DueFinalizations(now int64, limit int) ([]string, error) {
	rows, err := tx.conn.QueryContext(tx.ctx, `SELECT execution_id FROM report_finalizations
		WHERE due_at<=? ORDER BY due_at, execution_id LIMIT ?`, now, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := []string{}
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			return nil, err
		}
		result = append(result, id)
	}
	return result, rows.Err()
}

func (tx *Tx) ExecutionItem(executionID string) (string, error) {
	var itemID string
	err := tx.conn.QueryRowContext(tx.ctx, "SELECT item_id FROM executions WHERE id=?", executionID).Scan(&itemID)
	return itemID, err
}

func (tx *Tx) RecordFinalizationFailure(executionID string, failures int64, due *int64) error {
	_, err := tx.conn.ExecContext(tx.ctx, "UPDATE report_finalizations SET failures=?, due_at=? WHERE execution_id=?",
		failures, due, executionID)
	return err
}

func (tx *Tx) RetryFinalization(executionID string) (bool, error) {
	result, err := tx.conn.ExecContext(tx.ctx,
		"UPDATE report_finalizations SET failures=0, due_at=0 WHERE execution_id=? AND due_at IS NULL", executionID)
	if err != nil {
		return false, err
	}
	count, err := result.RowsAffected()
	return count != 0, err
}

func (tx *Tx) DeleteFinalization(executionID string) error {
	_, err := tx.conn.ExecContext(tx.ctx, "DELETE FROM report_finalizations WHERE execution_id=?", executionID)
	return err
}

func (tx *Tx) Report(executionID string) (string, error) {
	var markdown string
	err := tx.conn.QueryRowContext(tx.ctx, "SELECT markdown FROM reports WHERE execution_id=?", executionID).Scan(&markdown)
	return markdown, err
}

func (tx *Tx) ExecutionStatus(executionID string) (string, error) {
	var status *string
	err := tx.conn.QueryRowContext(tx.ctx, "SELECT status FROM executions WHERE id=?", executionID).Scan(&status)
	if status == nil {
		return "", err
	}
	return *status, err
}

func (tx *Tx) InsertReport(executionID, markdown string) error {
	_, err := tx.conn.ExecContext(tx.ctx, "INSERT INTO reports VALUES (?, ?)", executionID, markdown)
	return err
}

func (tx *Tx) AttemptCounts(executionID string) (string, error) {
	rows, err := tx.conn.QueryContext(tx.ctx, `SELECT stage, count(*) FROM attempts WHERE execution_id=?
		GROUP BY stage ORDER BY stage`, executionID)
	if err != nil {
		return "", err
	}
	defer rows.Close()
	var counts []string
	for rows.Next() {
		var stage string
		var count int64
		if err := rows.Scan(&stage, &count); err != nil {
			return "", err
		}
		counts = append(counts, fmt.Sprintf("%s (%d)", stage, count))
	}
	if err := rows.Err(); err != nil {
		return "", err
	}
	if len(counts) == 0 {
		return "No stages claimed", nil
	}
	return strings.Join(counts, ", "), nil
}

type FinalizationFailure struct{ Cause error }

func (e *FinalizationFailure) Error() string { return e.Cause.Error() }
func (e *FinalizationFailure) Unwrap() error { return e.Cause }

func (tx *Tx) FinalizationSavepoint(perform func() error) error {
	if _, err := tx.conn.ExecContext(tx.ctx, "SAVEPOINT finalize_report"); err != nil {
		return err
	}
	if err := perform(); err != nil {
		_, rollback := tx.conn.ExecContext(tx.ctx, "ROLLBACK TO finalize_report")
		_, release := tx.conn.ExecContext(tx.ctx, "RELEASE finalize_report")
		if rollback != nil || release != nil {
			return errors.Join(err, rollback, release)
		}
		return &FinalizationFailure{err}
	}
	_, err := tx.conn.ExecContext(tx.ctx, "RELEASE finalize_report")
	return err
}

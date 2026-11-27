package sqlite

type FlagRecord struct {
	Kind, ItemID, ExecutionID, CaptainID string
	Reason, Recovery, FirstSeen          string
	UpdatedAt                            string
	NotifyOwner                          bool
}

func scanFlag(row scanner) (FlagRecord, error) {
	var result FlagRecord
	err := row.Scan(&result.Kind, &result.ItemID, &result.ExecutionID, &result.CaptainID, &result.Reason,
		&result.Recovery, &result.NotifyOwner, &result.FirstSeen, &result.UpdatedAt)
	return result, err
}

const flagColumns = "kind,item_id,execution_id,captain_id,reason,recovery,notify_owner,first_seen,updated_at"

func (tx *Tx) StoredFlags(executionID string) ([]FlagRecord, error) {
	query, parameters := "SELECT "+flagColumns+" FROM flags", []any{}
	if executionID != "" {
		query += " WHERE execution_id=?"
		parameters = append(parameters, executionID)
	}
	rows, err := tx.conn.QueryContext(tx.ctx, query, parameters...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := []FlagRecord{}
	for rows.Next() {
		row, err := scanFlag(rows)
		if err != nil {
			return nil, err
		}
		result = append(result, row)
	}
	return result, rows.Err()
}

func (tx *Tx) RaiseFlag(row FlagRecord) (FlagRecord, error) {
	_, err := tx.conn.ExecContext(tx.ctx, `INSERT INTO flags VALUES (?,?,?,?,?,?,?,?,?)
		ON CONFLICT(kind,item_id,execution_id,captain_id) DO UPDATE SET reason=excluded.reason,recovery=excluded.recovery,
		notify_owner=excluded.notify_owner,updated_at=excluded.updated_at`, row.Kind, row.ItemID, row.ExecutionID,
		row.CaptainID, row.Reason, row.Recovery, row.NotifyOwner, row.FirstSeen, row.UpdatedAt)
	if err != nil {
		return row, err
	}
	return scanFlag(tx.conn.QueryRowContext(tx.ctx, "SELECT "+flagColumns+
		" FROM flags WHERE kind=? AND item_id=? AND execution_id=? AND captain_id=?",
		row.Kind, row.ItemID, row.ExecutionID, row.CaptainID))
}

func (tx *Tx) ClearFlag(kind, itemID, executionID, captainID string) (bool, error) {
	result, err := tx.conn.ExecContext(tx.ctx, "DELETE FROM flags WHERE kind=? AND item_id=? AND execution_id=? AND captain_id=?",
		kind, itemID, executionID, captainID)
	if err != nil {
		return false, err
	}
	count, err := result.RowsAffected()
	return count > 0, err
}

type FlagWriter struct {
	WriterRecord
	LastAdvancedAt string
}

func (tx *Tx) FlagWriters(executionID string) ([]FlagWriter, error) {
	query := `SELECT w.id,w.execution_id,w.item_id,w.captain_id,w.runtime,w.nonce,w.state,w.session_id,w.created_at,
		w.confirmed_at,w.state_reported_at,w.last_progress_at,w.progress_note,w.building,w.building_requested_at,p.last_advanced_at
		FROM writers w JOIN executions e ON e.id=w.execution_id JOIN operator_progress p ON p.execution_id=w.execution_id
		WHERE w.ended_at IS NULL AND e.status NOT IN ` + terminalStatusesSQL
	parameters := []any{}
	if executionID != "" {
		query += " AND e.id=?"
		parameters = append(parameters, executionID)
	}
	rows, err := tx.conn.QueryContext(tx.ctx, query+" ORDER BY w.created_at,w.id", parameters...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := []FlagWriter{}
	for rows.Next() {
		var row FlagWriter
		if err := rows.Scan(&row.ID, &row.ExecutionID, &row.ItemID, &row.CaptainID, &row.Runtime, &row.Nonce,
			&row.State, &row.SessionID, &row.CreatedAt, &row.ConfirmedAt, &row.StateReportedAt, &row.LastProgressAt,
			&row.ProgressNote, &row.Building, &row.BuildingRequestedAt, &row.LastAdvancedAt); err != nil {
			return nil, err
		}
		result = append(result, row)
	}
	return result, rows.Err()
}

type QueueProjection struct {
	ID, ItemID, RepositoryID, Stage, StateJSON string
	ReadySince, RetryAt                        *int64
	DependencyWait                             *string
}

func (tx *Tx) OverdueQueue(cutoffMS int64) ([]QueueProjection, error) {
	rows, err := tx.conn.QueryContext(tx.ctx, `SELECT e.id,e.item_id,e.queue_repository,e.stage,e.state_json,
		e.ready_since,e.retry_at,e.dependency_wait_json FROM executions e WHERE e.status='queued'
		AND e.ready_since<=? AND e.dependency_wait_json IS NULL
		AND NOT EXISTS(SELECT 1 FROM writers WHERE execution_id=e.id AND ended_at IS NULL)
		ORDER BY e.ready_since,e.id`, cutoffMS)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := []QueueProjection{}
	for rows.Next() {
		var row QueueProjection
		if err := rows.Scan(&row.ID, &row.ItemID, &row.RepositoryID, &row.Stage, &row.StateJSON,
			&row.ReadySince, &row.RetryAt, &row.DependencyWait); err != nil {
			return nil, err
		}
		result = append(result, row)
	}
	return result, rows.Err()
}

type CaptainHold struct{ ExecutionID, CaptainID, Reason string }

func (tx *Tx) ActiveCaptainHolds(cutoff string) ([]CaptainHold, error) {
	rows, err := tx.conn.QueryContext(tx.ctx, `SELECT action.execution_id,action.captain_id,action.reason
		FROM captain_actions AS action JOIN executions AS execution ON execution.id=action.execution_id
		WHERE action.action='held' AND action.created_at>? AND execution.status='queued' AND action.version=execution.version
		AND NOT EXISTS(SELECT 1 FROM captain_actions AS latest
			WHERE latest.execution_id=action.execution_id AND latest.version=action.version
			AND (latest.created_at,latest.rowid)>(action.created_at,action.rowid))`, cutoff)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := []CaptainHold{}
	for rows.Next() {
		var row CaptainHold
		if err := rows.Scan(&row.ExecutionID, &row.CaptainID, &row.Reason); err != nil {
			return nil, err
		}
		result = append(result, row)
	}
	return result, rows.Err()
}

func (tx *Tx) OldestOpenWork() (*string, error) {
	var stamp *string
	err := tx.conn.QueryRowContext(tx.ctx,
		"SELECT min(created_at) FROM executions WHERE status NOT IN "+terminalStatusesSQL).Scan(&stamp)
	return stamp, err
}

func (tx *Tx) RegisteredSessionReferences() ([]string, error) {
	rows, err := tx.conn.QueryContext(tx.ctx, `SELECT session_id FROM writers WHERE ended_at IS NULL AND session_id IS NOT NULL
		UNION SELECT session_ref FROM captains WHERE state!='ended'`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := []string{}
	for rows.Next() {
		var value string
		if err := rows.Scan(&value); err != nil {
			return nil, err
		}
		result = append(result, value)
	}
	return result, rows.Err()
}

func (tx *Tx) AllArchivedSessions() (map[string]bool, error) {
	rows, err := tx.conn.QueryContext(tx.ctx, "SELECT session_id FROM session_archives")
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := map[string]bool{}
	for rows.Next() {
		var value string
		if err := rows.Scan(&value); err != nil {
			return nil, err
		}
		result[value] = true
	}
	return result, rows.Err()
}

type UnresolvedArchive struct{ SessionID, Outcome, Reason, UpdatedAt string }

func (tx *Tx) UnresolvedArchives(limit int) ([]UnresolvedArchive, error) {
	rows, err := tx.conn.QueryContext(tx.ctx,
		"SELECT session_id,outcome,reason,updated_at FROM session_archives WHERE outcome!='archived' ORDER BY updated_at LIMIT ?", limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := []UnresolvedArchive{}
	for rows.Next() {
		var row UnresolvedArchive
		if err := rows.Scan(&row.SessionID, &row.Outcome, &row.Reason, &row.UpdatedAt); err != nil {
			return nil, err
		}
		result = append(result, row)
	}
	return result, rows.Err()
}

type ActiveWorkProjection struct {
	ID, ItemID, Stage, Status, StateJSON, StartedAt string
	WriterID, CaptainID, SessionID                  *string
	LastProgressAt, ProgressNote                    *string
	ActionCaptainID, Action, ActionCreatedAt        *string
	ActionReason                                    *string
}

func (tx *Tx) ActiveOverdue(cutoff, executionID string, limit int) ([]ActiveWorkProjection, error) {
	query := `SELECT e.id,e.item_id,e.stage,e.status,e.state_json,
		(SELECT min(started_at) FROM attempts WHERE execution_id=e.id) AS started_at,
		w.id,w.captain_id,w.session_id,w.last_progress_at,w.progress_note,
		a.captain_id,a.action,a.created_at,a.reason
		FROM executions e LEFT JOIN writers w ON w.execution_id=e.id AND w.ended_at IS NULL
		LEFT JOIN captain_actions a ON a.rowid=(SELECT latest.rowid FROM captain_actions latest
			WHERE latest.execution_id=e.id ORDER BY latest.created_at DESC,latest.rowid DESC LIMIT 1)
		WHERE e.status NOT IN ` + terminalStatusesSQL + ` AND started_at<?`
	parameters := []any{cutoff}
	if executionID != "" {
		query += " AND e.id=?"
		parameters = append(parameters, executionID)
	}
	parameters = append(parameters, limit)
	rows, err := tx.conn.QueryContext(tx.ctx, query+" ORDER BY started_at,e.id LIMIT ?", parameters...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := []ActiveWorkProjection{}
	for rows.Next() {
		var row ActiveWorkProjection
		if err := rows.Scan(&row.ID, &row.ItemID, &row.Stage, &row.Status, &row.StateJSON, &row.StartedAt,
			&row.WriterID, &row.CaptainID, &row.SessionID, &row.LastProgressAt, &row.ProgressNote,
			&row.ActionCaptainID, &row.Action, &row.ActionCreatedAt, &row.ActionReason); err != nil {
			return nil, err
		}
		result = append(result, row)
	}
	return result, rows.Err()
}

type ObservationProjection struct {
	ItemID, ExecutionID, StateJSON, ClosedAt, ObservedAt, LastObservedAt string
	ObservationJSON                                                      string
	HandoffHead, CheckedAt, FailuresJSON                                 *string
}

func (tx *Tx) ObservationProjections(executionID string) ([]ObservationProjection, error) {
	query := `WITH latest AS (
		SELECT o.*,e.item_id,e.state_json,e.handoff_head,e.updated_at AS closed_at,
		row_number() OVER (PARTITION BY o.execution_id ORDER BY o.last_observed_at DESC,o.observed_at DESC) AS rank
		FROM github_observations o JOIN executions e ON e.id=o.execution_id
		WHERE (e.status NOT IN ` + terminalStatusesSQL + ` OR (e.status='completed'
			AND json_extract(e.state_json,'$.final_output.merge_commit') IS NOT NULL
			AND json_type(o.observation_json,'$.deployment')='object'))`
	parameters := []any{}
	if executionID != "" {
		query += " AND e.id=?"
		parameters = append(parameters, executionID)
	}
	query += `) SELECT latest.item_id,latest.execution_id,latest.state_json,latest.closed_at,
		latest.observed_at,latest.last_observed_at,latest.observation_json,latest.handoff_head,checked.checked_at,checked.failures_json
		FROM latest LEFT JOIN local_check_results checked ON checked.execution_id=latest.execution_id AND checked.head_sha=latest.head_sha
		WHERE rank=1 ORDER BY latest.execution_id`
	rows, err := tx.conn.QueryContext(tx.ctx, query, parameters...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := []ObservationProjection{}
	for rows.Next() {
		var row ObservationProjection
		if err := rows.Scan(&row.ItemID, &row.ExecutionID, &row.StateJSON, &row.ClosedAt, &row.ObservedAt,
			&row.LastObservedAt, &row.ObservationJSON, &row.HandoffHead, &row.CheckedAt, &row.FailuresJSON); err != nil {
			return nil, err
		}
		result = append(result, row)
	}
	return result, rows.Err()
}

type ReconcilerHeartbeat struct{ RunnerID, LastSeen string }

func (tx *Tx) ReconcilerHeartbeats() ([]ReconcilerHeartbeat, error) {
	rows, err := tx.conn.QueryContext(tx.ctx, "SELECT runner_id,last_seen FROM reconciler_heartbeats ORDER BY runner_id")
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := []ReconcilerHeartbeat{}
	for rows.Next() {
		var row ReconcilerHeartbeat
		if err := rows.Scan(&row.RunnerID, &row.LastSeen); err != nil {
			return nil, err
		}
		result = append(result, row)
	}
	return result, rows.Err()
}

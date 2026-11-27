package sqlite

import "database/sql"

type Dependency struct {
	Link
	ExecutionID  *string
	State        *string
	ReportExists bool
}

func (tx *Tx) Dependencies(itemID string) ([]Dependency, error) {
	rows, err := tx.conn.QueryContext(tx.ctx, `SELECT link.upstream_id, link.milestone,
		current.id, current.state_json,
		EXISTS (SELECT 1 FROM reports WHERE execution_id=current.id)
		FROM work_dependencies AS link
		LEFT JOIN executions AS current ON current.item_id=link.upstream_id
			AND NOT EXISTS (SELECT 1 FROM executions WHERE previous_id=current.id)
		WHERE link.item_id=? ORDER BY link.upstream_id`, itemID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := []Dependency{}
	for rows.Next() {
		var row Dependency
		if err := rows.Scan(&row.UpstreamID, &row.Milestone, &row.ExecutionID, &row.State, &row.ReportExists); err != nil {
			return nil, err
		}
		result = append(result, row)
	}
	return result, rows.Err()
}

func (tx *Tx) UpdateDependencyWait(executionID string, wait *string, nowMS int64) error {
	_, err := tx.conn.ExecContext(tx.ctx, `UPDATE executions SET dependency_wait_json=?,
		ready_since=CASE WHEN ? IS NULL AND dependency_wait_json IS NOT NULL
		THEN max(?, coalesce(retry_at,0)) ELSE ready_since END
		WHERE id=? AND status='queued' AND dependency_wait_json IS NOT ?`, wait, wait, nowMS, executionID, wait)
	return err
}

func (tx *Tx) DependencyWaitPresent(executionID string) (bool, error) {
	var present bool
	err := tx.conn.QueryRowContext(tx.ctx,
		"SELECT dependency_wait_json IS NOT NULL FROM executions WHERE id=?", executionID).Scan(&present)
	return present, err
}

func (tx *Tx) DependentItems(upstreamID, cursor string, limit int) ([]string, error) {
	rows, err := tx.conn.QueryContext(tx.ctx, `SELECT item_id FROM work_dependencies
		WHERE upstream_id=? AND item_id>? ORDER BY item_id LIMIT ?`, upstreamID, cursor, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := []string{}
	for rows.Next() {
		var itemID string
		if err := rows.Scan(&itemID); err != nil {
			return nil, err
		}
		result = append(result, itemID)
	}
	return result, rows.Err()
}

func (tx *Tx) QueuedExecution(itemID string) (string, error) {
	var id string
	err := tx.conn.QueryRowContext(tx.ctx, "SELECT id FROM executions WHERE item_id=? AND status='queued'", itemID).Scan(&id)
	return id, err
}

func (tx *Tx) ScheduleDependencyRefresh(upstreamID string, cursor *string, nowNS int64) error {
	if cursor == nil {
		_, err := tx.conn.ExecContext(tx.ctx, "DELETE FROM dependency_refresh_jobs WHERE upstream_id=?", upstreamID)
		return err
	}
	_, err := tx.conn.ExecContext(tx.ctx, `INSERT INTO dependency_refresh_jobs VALUES (?, ?, ?)
		ON CONFLICT(upstream_id) DO UPDATE SET cursor=excluded.cursor, scheduled_at=excluded.scheduled_at`,
		upstreamID, cursor, nowNS)
	return err
}

func (tx *Tx) StartDependencyAudit() error {
	_, err := tx.conn.ExecContext(tx.ctx, "UPDATE dependency_audit SET cursor='' WHERE id=1 AND cursor IS NULL")
	return err
}

func (tx *Tx) DependencyAuditCursor() (*string, error) {
	var cursor *string
	err := tx.conn.QueryRowContext(tx.ctx, "SELECT cursor FROM dependency_audit WHERE id=1").Scan(&cursor)
	return cursor, err
}

func (tx *Tx) DependencyAuditItems(cursor string, limit int) ([]string, error) {
	rows, err := tx.conn.QueryContext(tx.ctx, `SELECT DISTINCT item_id FROM work_dependencies
		WHERE item_id>? ORDER BY item_id LIMIT ?`, cursor, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := []string{}
	for rows.Next() {
		var itemID string
		if err := rows.Scan(&itemID); err != nil {
			return nil, err
		}
		result = append(result, itemID)
	}
	return result, rows.Err()
}

func (tx *Tx) SetDependencyAuditCursor(cursor *string) error {
	_, err := tx.conn.ExecContext(tx.ctx, "UPDATE dependency_audit SET cursor=? WHERE id=1", cursor)
	return err
}

func (tx *Tx) HasDependencyJobs() (bool, bool, error) {
	var jobs, audit bool
	err := tx.conn.QueryRowContext(tx.ctx, `SELECT EXISTS (SELECT 1 FROM dependency_refresh_jobs),
		EXISTS (SELECT 1 FROM dependency_audit WHERE id=1 AND cursor IS NOT NULL)`).Scan(&jobs, &audit)
	return jobs, audit, err
}

func (tx *Tx) FirstDependencyJob() (string, string, error) {
	var upstreamID, cursor string
	err := tx.conn.QueryRowContext(tx.ctx, `SELECT upstream_id, cursor FROM dependency_refresh_jobs
		ORDER BY scheduled_at, upstream_id LIMIT 1`).Scan(&upstreamID, &cursor)
	if err == sql.ErrNoRows {
		return "", "", nil
	}
	return upstreamID, cursor, err
}

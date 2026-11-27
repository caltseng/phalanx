package sqlite

func (tx *Tx) LatestObservationTime(executionID string) (*string, error) {
	var result *string
	err := tx.conn.QueryRowContext(tx.ctx, "SELECT max(last_observed_at) FROM github_observations WHERE execution_id=?",
		executionID).Scan(&result)
	return result, err
}

func (tx *Tx) RecordObservation(executionID, url, head, digest string, version int64, stamp, payload string) (bool, error) {
	result, err := tx.conn.ExecContext(tx.ctx, `UPDATE github_observations SET last_observed_at=max(last_observed_at,?)
		WHERE execution_id=? AND pr_url=? AND head_sha=? AND digest=?`, stamp, executionID, url, head, digest)
	if err != nil {
		return false, err
	}
	count, err := result.RowsAffected()
	if err != nil || count != 0 {
		return false, err
	}
	_, err = tx.conn.ExecContext(tx.ctx, "INSERT INTO github_observations VALUES (?,?,?,?,?,?,?,?)",
		executionID, url, head, digest, version, stamp, stamp, payload)
	return err == nil, err
}

func (tx *Tx) HasObservedHead(executionID, head string) (bool, error) {
	var result bool
	err := tx.conn.QueryRowContext(tx.ctx,
		"SELECT EXISTS(SELECT 1 FROM github_observations WHERE execution_id=? AND head_sha=?)", executionID, head).Scan(&result)
	return result, err
}

func (tx *Tx) HasLocalChecks(executionID, head string) (bool, error) {
	var result bool
	err := tx.conn.QueryRowContext(tx.ctx,
		"SELECT EXISTS(SELECT 1 FROM local_check_results WHERE execution_id=? AND head_sha=?)", executionID, head).Scan(&result)
	return result, err
}

func (tx *Tx) RecordLocalChecks(executionID, head string, version int64, stamp, failures string) (bool, error) {
	result, err := tx.conn.ExecContext(tx.ctx, "INSERT INTO local_check_results VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING",
		executionID, head, version, stamp, failures)
	if err != nil {
		return false, err
	}
	count, err := result.RowsAffected()
	return count > 0, err
}

func (tx *Tx) WriterReworkOutstanding(executionID string) (bool, error) {
	var result bool
	err := tx.conn.QueryRowContext(tx.ctx, `SELECT EXISTS(SELECT 1 FROM writers WHERE execution_id=?
		AND ended_at IS NULL AND (building=1 OR building_requested_at IS NOT NULL))`, executionID).Scan(&result)
	return result, err
}

func (tx *Tx) ReconcilerHeartbeat(runnerID, now string) error {
	_, err := tx.conn.ExecContext(tx.ctx, `INSERT INTO reconciler_heartbeats VALUES (?,?)
		ON CONFLICT DO UPDATE SET last_seen=excluded.last_seen`, runnerID, now)
	return err
}

func (tx *Tx) RetireReconciler(runnerID string) (bool, error) {
	result, err := tx.conn.ExecContext(tx.ctx, "DELETE FROM reconciler_heartbeats WHERE runner_id=?", runnerID)
	if err != nil {
		return false, err
	}
	count, err := result.RowsAffected()
	return count > 0, err
}

type DeploymentRetirement struct {
	ExecutionID, ItemID, Payload string
}

func (tx *Tx) DeploymentRetirements(repositoryID string, name *string) ([]DeploymentRetirement, error) {
	rows, err := tx.conn.QueryContext(tx.ctx, `SELECT execution.id,execution.item_id,observation.observation_json
		FROM executions AS execution
		JOIN work_items AS item ON item.id=execution.item_id
		JOIN github_observations AS observation ON observation.execution_id=execution.id
		WHERE item.repository_id=? AND execution.status='completed'
		AND json_extract(execution.state_json,'$.final_output.merge_commit') IS NOT NULL
		AND observation.digest=(SELECT latest.digest FROM github_observations AS latest
			WHERE latest.execution_id=execution.id ORDER BY latest.last_observed_at DESC,latest.observed_at DESC LIMIT 1)
		AND json_extract(observation.observation_json,'$.deployment.state')!='disabled'
		AND (? IS NULL OR json_extract(observation.observation_json,'$.deployment.check_name')!=?)`,
		repositoryID, name, name)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := []DeploymentRetirement{}
	for rows.Next() {
		var row DeploymentRetirement
		if err := rows.Scan(&row.ExecutionID, &row.ItemID, &row.Payload); err != nil {
			return nil, err
		}
		result = append(result, row)
	}
	return result, rows.Err()
}

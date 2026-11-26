package sqlite

func (tx *Tx) InsertDraft(executionID, revision, markdown string) error {
	_, err := tx.conn.ExecContext(tx.ctx,
		"INSERT INTO report_drafts VALUES (?,?,?) ON CONFLICT DO NOTHING", executionID, revision, markdown)
	return err
}

func (tx *Tx) DraftExists(executionID, revision string) (bool, error) {
	var exists bool
	err := tx.conn.QueryRowContext(tx.ctx,
		"SELECT EXISTS (SELECT 1 FROM report_drafts WHERE execution_id=? AND revision=?)", executionID, revision).Scan(&exists)
	return exists, err
}

func (tx *Tx) PreviousMergedPR(itemID, executionID, url string) (bool, error) {
	var exists bool
	err := tx.conn.QueryRowContext(tx.ctx, `SELECT EXISTS (SELECT 1 FROM executions e
		LEFT JOIN github_observations o ON o.execution_id=e.id
		WHERE e.item_id=? AND e.id<>? AND (
		(lower(o.pr_url)=lower(?) AND json_extract(o.observation_json,'$.pr.merged'))
		OR (lower(json_extract(e.state_json,'$.final_output.url'))=lower(?)
			AND json_extract(e.state_json,'$.final_output.merge_commit') IS NOT NULL)))`,
		itemID, executionID, url, url).Scan(&exists)
	return exists, err
}

func (tx *Tx) ReworkCounts(executionID, findings string) (int64, int64, error) {
	var total, repeated int64
	err := tx.conn.QueryRowContext(tx.ctx, `SELECT count(*),
		coalesce(sum(json_extract(state_json,'$.findings')=?),0)
		FROM transition_history WHERE execution_id=? AND event='result'
		AND json_extract(state_json,'$.stage')='development'
		AND json_extract(details_json,'$.stage')='self_review'
		AND version>coalesce((SELECT max(version) FROM transition_history WHERE execution_id=? AND event='reply'),0)`,
		findings, executionID, executionID).Scan(&total, &repeated)
	return total, repeated, err
}

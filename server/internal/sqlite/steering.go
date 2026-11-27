package sqlite

func (tx *Tx) ReplaceLinks(itemID string, links []Link) error {
	if _, err := tx.conn.ExecContext(tx.ctx, "DELETE FROM work_dependencies WHERE item_id=?", itemID); err != nil {
		return err
	}
	for _, link := range links {
		if err := tx.InsertLink(itemID, link); err != nil {
			return err
		}
	}
	return nil
}

func (tx *Tx) SteerItem(itemID, request string, executionID string, version int64, state, now string, priority int) error {
	if _, err := tx.conn.ExecContext(tx.ctx, "UPDATE work_items SET request_json=? WHERE id=?", request, itemID); err != nil {
		return err
	}
	_, err := tx.conn.ExecContext(tx.ctx, "UPDATE executions SET version=?,state_json=?,updated_at=?,queue_priority=? WHERE id=?",
		version, state, now, priority, executionID)
	return err
}

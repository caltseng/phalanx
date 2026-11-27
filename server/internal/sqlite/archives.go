package sqlite

import "database/sql"

type Archive struct {
	SessionID  string `json:"session_id"`
	Outcome    string `json:"outcome"`
	Reason     string `json:"reason"`
	ReportedBy string `json:"reported_by"`
	UpdatedAt  string `json:"updated_at"`
}

func (tx *Tx) Archive(sessionID string) (*Archive, error) {
	var result Archive
	err := tx.conn.QueryRowContext(tx.ctx, "SELECT session_id,outcome,reason,reported_by,updated_at FROM session_archives WHERE session_id=?",
		sessionID).Scan(&result.SessionID, &result.Outcome, &result.Reason, &result.ReportedBy, &result.UpdatedAt)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	return &result, err
}

func (tx *Tx) ArchiveWriters(sessionID string) ([]map[string]any, error) {
	rows, err := tx.conn.QueryContext(tx.ctx, "SELECT id,item_id,execution_id,ended_at FROM writers WHERE session_id=?", sessionID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := []map[string]any{}
	for rows.Next() {
		var id, item, execution string
		var ended *string
		if err := rows.Scan(&id, &item, &execution, &ended); err != nil {
			return nil, err
		}
		result = append(result, map[string]any{"id": id, "item_id": item, "execution_id": execution, "ended_at": ended})
	}
	return result, rows.Err()
}

func (tx *Tx) ItemHasWriter(itemID string) (bool, error) {
	var active bool
	err := tx.conn.QueryRowContext(tx.ctx, "SELECT EXISTS (SELECT 1 FROM writers WHERE item_id=? AND ended_at IS NULL)", itemID).Scan(&active)
	return active, err
}

func (tx *Tx) CaptainHasSuccessor(captainID string) (bool, error) {
	var exists bool
	err := tx.conn.QueryRowContext(tx.ctx, "SELECT EXISTS (SELECT 1 FROM captains WHERE parent_id=?)", captainID).Scan(&exists)
	return exists, err
}

func (tx *Tx) RecordArchive(sessionID, outcome, reason, reportedBy, now string) error {
	_, err := tx.conn.ExecContext(tx.ctx, `INSERT INTO session_archives VALUES (?,?,?,?,?)
		ON CONFLICT(session_id) DO UPDATE SET outcome=excluded.outcome,reason=excluded.reason,
		reported_by=excluded.reported_by,updated_at=excluded.updated_at`, sessionID, outcome, reason, reportedBy, now)
	return err
}

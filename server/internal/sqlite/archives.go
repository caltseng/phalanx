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

type ArchiveWriter struct {
	ID          string  `json:"id"`
	ItemID      string  `json:"item_id"`
	ExecutionID string  `json:"execution_id"`
	EndedAt     *string `json:"ended_at"`
	ItemActive  bool    `json:"-"`
}

func (tx *Tx) ArchiveWriters(sessionID string) ([]ArchiveWriter, error) {
	rows, err := tx.conn.QueryContext(tx.ctx, `SELECT w.id,w.item_id,w.execution_id,w.ended_at,
		EXISTS (SELECT 1 FROM writers active WHERE active.item_id=w.item_id AND active.ended_at IS NULL)
		FROM writers w WHERE w.session_id=?`, sessionID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := []ArchiveWriter{}
	for rows.Next() {
		var row ArchiveWriter
		if err := rows.Scan(&row.ID, &row.ItemID, &row.ExecutionID, &row.EndedAt, &row.ItemActive); err != nil {
			return nil, err
		}
		result = append(result, row)
	}
	return result, rows.Err()
}

func (tx *Tx) RecordArchive(sessionID, outcome, reason, reportedBy, now string) error {
	_, err := tx.conn.ExecContext(tx.ctx, `INSERT INTO session_archives VALUES (?,?,?,?,?)
		ON CONFLICT(session_id) DO UPDATE SET outcome=excluded.outcome,reason=excluded.reason,
		reported_by=excluded.reported_by,updated_at=excluded.updated_at`, sessionID, outcome, reason, reportedBy, now)
	return err
}

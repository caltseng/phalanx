package sqlite

import "database/sql"

type UsageRecord struct {
	Sequence                  int64
	ObservedAt                string
	SnapshotJSON, FailureJSON *string
}

func (tx *Tx) UsageRecord(writerID string) (*UsageRecord, error) {
	var row UsageRecord
	err := tx.conn.QueryRowContext(tx.ctx,
		"SELECT sequence,observed_at,snapshot_json,failure_json FROM writer_usage WHERE writer_id=?", writerID).
		Scan(&row.Sequence, &row.ObservedAt, &row.SnapshotJSON, &row.FailureJSON)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	return &row, err
}

func (tx *Tx) SaveUsage(writerID string, sequence int64, observedAt string, snapshot, failure *string) error {
	_, err := tx.conn.ExecContext(tx.ctx, `INSERT INTO writer_usage VALUES (?,?,?,?,?)
		ON CONFLICT(writer_id) DO UPDATE SET sequence=excluded.sequence,observed_at=excluded.observed_at,
		snapshot_json=excluded.snapshot_json,failure_json=excluded.failure_json`,
		writerID, sequence, observedAt, snapshot, failure)
	return err
}

type UsageProjectionRow struct {
	WriterID, ExecutionID, Runtime string
	SessionID, EndedAt             *string
	SnapshotJSON, FailureJSON      *string
	Bindings                       int64
}

func (tx *Tx) UsageProjection(itemID string) ([]UsageProjectionRow, error) {
	rows, err := tx.conn.QueryContext(tx.ctx, `SELECT writer.id,writer.execution_id,writer.runtime,writer.session_id,
		writer.ended_at,usage.snapshot_json,usage.failure_json,
		(SELECT count(*) FROM writers AS other WHERE other.session_id=writer.session_id)
		FROM writers AS writer LEFT JOIN writer_usage AS usage ON usage.writer_id=writer.id
		WHERE writer.item_id=? ORDER BY writer.created_at,writer.id`, itemID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := []UsageProjectionRow{}
	for rows.Next() {
		var row UsageProjectionRow
		if err := rows.Scan(&row.WriterID, &row.ExecutionID, &row.Runtime, &row.SessionID, &row.EndedAt,
			&row.SnapshotJSON, &row.FailureJSON, &row.Bindings); err != nil {
			return nil, err
		}
		result = append(result, row)
	}
	return result, rows.Err()
}

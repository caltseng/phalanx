package sqlite

import (
	"database/sql"
	"encoding/json"
	"strings"
)

type WriterRecord struct {
	ID, ExecutionID, ItemID, CaptainID, Runtime, Nonce, TokenDigest, State, CreatedAt string
	ReplacedID, AttemptID, SessionID, SessionURL                                      *string
	ConfirmedAt, AbandonedAt, StateReportedAt, LastProgressAt, ProgressNote           *string
	EndedAt, EndReason, BuildingRequestedAt                                           *string
	Building                                                                          bool
	CapacityRevision                                                                  int64
}

const writerColumns = `id,execution_id,item_id,captain_id,runtime,nonce,token_digest,state,created_at,
	replaced_id,attempt_id,session_id,session_url,confirmed_at,abandoned_at,state_reported_at,
	last_progress_at,progress_note,ended_at,end_reason,building_requested_at,building,capacity_revision`

func scanWriter(row scanner) (WriterRecord, error) {
	var w WriterRecord
	err := row.Scan(&w.ID, &w.ExecutionID, &w.ItemID, &w.CaptainID, &w.Runtime, &w.Nonce, &w.TokenDigest, &w.State, &w.CreatedAt,
		&w.ReplacedID, &w.AttemptID, &w.SessionID, &w.SessionURL, &w.ConfirmedAt, &w.AbandonedAt, &w.StateReportedAt,
		&w.LastProgressAt, &w.ProgressNote, &w.EndedAt, &w.EndReason, &w.BuildingRequestedAt, &w.Building, &w.CapacityRevision)
	return w, err
}

func (tx *Tx) WriterRecord(id string) (WriterRecord, error) {
	return scanWriter(tx.conn.QueryRowContext(tx.ctx, "SELECT "+writerColumns+" FROM writers WHERE id=?", id))
}

func (tx *Tx) WriterByNonce(nonce string) (*WriterRecord, error) {
	row, err := scanWriter(tx.conn.QueryRowContext(tx.ctx, "SELECT "+writerColumns+" FROM writers WHERE nonce=?", nonce))
	if err == sql.ErrNoRows {
		return nil, nil
	}
	return &row, err
}

func (tx *Tx) OpenWriterRecord(executionID string) (*WriterRecord, error) {
	row, err := scanWriter(tx.conn.QueryRowContext(tx.ctx,
		"SELECT "+writerColumns+" FROM writers WHERE execution_id=? AND ended_at IS NULL", executionID))
	if err == sql.ErrNoRows {
		return nil, nil
	}
	return &row, err
}

func (tx *Tx) InsertWriter(w WriterRecord) error {
	_, err := tx.conn.ExecContext(tx.ctx, `INSERT INTO writers
		(id,execution_id,item_id,captain_id,runtime,nonce,token_digest,replaced_id,state,created_at,building,building_requested_at)
		VALUES (?,?,?,?,?,?,?,?,'launching',?,?,?)`, w.ID, w.ExecutionID, w.ItemID, w.CaptainID, w.Runtime, w.Nonce,
		w.TokenDigest, w.ReplacedID, w.CreatedAt, w.Building, w.BuildingRequestedAt)
	return err
}

func (tx *Tx) RequestBuilding(writerID, now string) error {
	_, err := tx.conn.ExecContext(tx.ctx,
		"UPDATE writers SET building_requested_at=?,capacity_revision=capacity_revision+1 WHERE id=?", now, writerID)
	return err
}

func (tx *Tx) GrantBuilding(writerID string) error {
	_, err := tx.conn.ExecContext(tx.ctx,
		"UPDATE writers SET building=1,building_requested_at=NULL,capacity_revision=capacity_revision+1 WHERE id=?", writerID)
	return err
}

func (tx *Tx) RelinquishBuilding(writerID string) error {
	_, err := tx.conn.ExecContext(tx.ctx, `UPDATE writers SET building=0,building_requested_at=NULL,
		capacity_revision=capacity_revision+1 WHERE id=? AND (building=1 OR building_requested_at IS NOT NULL)`, writerID)
	return err
}

func (tx *Tx) SetHandoff(executionID string, head *string) error {
	_, err := tx.conn.ExecContext(tx.ctx, "UPDATE executions SET handoff_head=? WHERE id=?", head, executionID)
	return err
}

func (tx *Tx) Handoff(executionID string) (*string, error) {
	var head *string
	err := tx.conn.QueryRowContext(tx.ctx, "SELECT handoff_head FROM executions WHERE id=?", executionID).Scan(&head)
	return head, err
}

func (tx *Tx) SetWriterAttempt(writerID string, attemptID *string) error {
	_, err := tx.conn.ExecContext(tx.ctx, "UPDATE writers SET attempt_id=? WHERE id=?", attemptID, writerID)
	return err
}

func (tx *Tx) InsertAttempt(id, executionID, stage, now string) error {
	_, err := tx.conn.ExecContext(tx.ctx, "INSERT INTO attempts(id,execution_id,stage,started_at) VALUES (?,?,?,?)",
		id, executionID, stage, now)
	return err
}

func (tx *Tx) EndAttempt(id, now string) error {
	_, err := tx.conn.ExecContext(tx.ctx, "UPDATE attempts SET ended_at=? WHERE id=?", now, id)
	return err
}

func (tx *Tx) OpenAttempt(executionID string) (*string, error) {
	var id string
	err := tx.conn.QueryRowContext(tx.ctx, "SELECT id FROM attempts WHERE execution_id=? AND ended_at IS NULL", executionID).Scan(&id)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	return &id, err
}

func (tx *Tx) AttemptActive(id string) (bool, error) {
	var active bool
	err := tx.conn.QueryRowContext(tx.ctx, "SELECT EXISTS (SELECT 1 FROM attempts WHERE id=? AND ended_at IS NULL)", id).Scan(&active)
	return active, err
}

func (tx *Tx) CurrentAttemptStage(writerID, executionID, attemptID string) (string, error) {
	var stage string
	err := tx.conn.QueryRowContext(tx.ctx, `SELECT a.stage FROM attempts a JOIN writers w
		ON w.attempt_id=a.id AND w.execution_id=a.execution_id
		WHERE a.id=? AND a.execution_id=? AND a.ended_at IS NULL AND w.id=? AND w.ended_at IS NULL`,
		attemptID, executionID, writerID).Scan(&stage)
	return stage, err
}

func (tx *Tx) AttemptStage(id string) (string, error) {
	var stage string
	err := tx.conn.QueryRowContext(tx.ctx, "SELECT stage FROM attempts WHERE id=?", id).Scan(&stage)
	return stage, err
}

func (tx *Tx) Progress(writerID, now string, note *string) error {
	_, err := tx.conn.ExecContext(tx.ctx,
		"UPDATE writers SET last_progress_at=?,progress_note=coalesce(?,progress_note) WHERE id=?", now, note, writerID)
	return err
}

func (tx *Tx) EndWriter(writerID, now, reason string) error {
	_, err := tx.conn.ExecContext(tx.ctx,
		"UPDATE writers SET ended_at=?,end_reason=?,attempt_id=NULL WHERE id=?", now, reason, writerID)
	return err
}

func (tx *Tx) HistoryEventDetails(executionID string, version int64, event string) (*string, error) {
	var details string
	err := tx.conn.QueryRowContext(tx.ctx, `SELECT details_json FROM transition_history
		WHERE execution_id=? AND version=? AND event=?`, executionID, version, event).Scan(&details)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	return &details, err
}

type LatestUsage struct {
	Sequence         int64           `json:"sequence"`
	Snapshot         json.RawMessage `json:"snapshot"`
	AcquisitionError json.RawMessage `json:"acquisition_error"`
}

func (tx *Tx) LatestUsage(writerID string) (*LatestUsage, error) {
	var result LatestUsage
	var snapshot, failure *string
	err := tx.conn.QueryRowContext(tx.ctx, "SELECT sequence,snapshot_json,failure_json FROM writer_usage WHERE writer_id=?",
		writerID).Scan(&result.Sequence, &snapshot, &failure)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	if snapshot != nil && *snapshot != "" {
		result.Snapshot = json.RawMessage(*snapshot)
	}
	if failure != nil && *failure != "" {
		result.AcquisitionError = json.RawMessage(*failure)
	}
	return &result, err
}

type DispatchSettings struct {
	Paused             bool  `json:"paused"`
	MaxWriters         int64 `json:"max_writers"`
	ActiveWriters      int64 `json:"active_writers"`
	MaxBuildingWriters int64 `json:"max_building_writers"`
	BuildingWriters    int64 `json:"building_writers"`
	DeliveringWriters  int64 `json:"delivering_writers"`
}

func (tx *Tx) DispatchSettings() (DispatchSettings, error) {
	var result DispatchSettings
	err := tx.conn.QueryRowContext(tx.ctx, "SELECT paused,max_writers,max_building_writers FROM dispatch_settings WHERE id=1").
		Scan(&result.Paused, &result.MaxWriters, &result.MaxBuildingWriters)
	if err != nil {
		return result, err
	}
	err = tx.conn.QueryRowContext(tx.ctx, `SELECT count(*),coalesce(sum(w.building),0) FROM writers w
		WHERE w.ended_at IS NULL AND EXISTS (SELECT 1 FROM executions e WHERE e.id=w.execution_id
		AND e.status NOT IN ('completed','failed','cancelled'))`).Scan(&result.ActiveWriters, &result.BuildingWriters)
	result.DeliveringWriters = result.ActiveWriters - result.BuildingWriters
	return result, err
}

func (tx *Tx) FirstRework(nowMS int64) (*string, error) {
	var id string
	err := tx.conn.QueryRowContext(tx.ctx, `SELECT w.id FROM writers w JOIN executions e ON e.id=w.execution_id
		WHERE w.ended_at IS NULL AND w.building=0 AND w.building_requested_at IS NOT NULL
		AND json_extract(e.state_json,'$.primary_pr') IS NOT NULL
		AND e.status IN ('queued','running','waiting_for_external')
		AND json_extract(e.state_json,'$.stopping') IS NULL AND json_extract(e.state_json,'$.pending_output') IS NULL
		AND e.dependency_wait_json IS NULL AND (e.retry_at IS NULL OR e.retry_at<=?)
		AND (e.status!='queued' OR e.ready_since<=?)
		ORDER BY w.building_requested_at,w.id LIMIT 1`, nowMS, nowMS).Scan(&id)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	return &id, err
}

func (tx *Tx) ActiveWriters() ([]WriterRecord, error) {
	rows, err := tx.conn.QueryContext(tx.ctx, `SELECT w.id,w.item_id,w.session_id,w.building FROM writers w
		WHERE w.ended_at IS NULL AND EXISTS (SELECT 1 FROM executions e WHERE e.id=w.execution_id
		AND e.status NOT IN ('completed','failed','cancelled')) ORDER BY w.created_at,w.id`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := []WriterRecord{}
	for rows.Next() {
		var row WriterRecord
		if err := rows.Scan(&row.ID, &row.ItemID, &row.SessionID, &row.Building); err != nil {
			return nil, err
		}
		result = append(result, row)
	}
	return result, rows.Err()
}

func (tx *Tx) ConfirmWriter(writerID, owner, state, now string, sessionID, sessionURL *string, confirming, abandon, stateReported bool) error {
	var confirmedAt *string
	if confirming {
		confirmedAt = &now
	}
	_, err := tx.conn.ExecContext(tx.ctx, `UPDATE writers SET captain_id=?,session_id=coalesce(?,session_id),
		session_url=coalesce(?,session_url),confirmed_at=coalesce(confirmed_at,?),
		abandoned_at=CASE WHEN ? THEN coalesce(abandoned_at,?) ELSE abandoned_at END,
		state=?,state_reported_at=CASE WHEN ? THEN ? ELSE state_reported_at END WHERE id=?`,
		owner, sessionID, sessionURL, confirmedAt, abandon, now, state, stateReported, now, writerID)
	return err
}

func (tx *Tx) UpdateDispatch(paused *bool, maxWriters, maxBuilding *int64) error {
	columns := []string{}
	values := []any{}
	if paused != nil {
		columns, values = append(columns, "paused=?"), append(values, *paused)
	}
	if maxWriters != nil {
		columns, values = append(columns, "max_writers=?"), append(values, *maxWriters)
	}
	if maxBuilding != nil {
		columns, values = append(columns, "max_building_writers=?"), append(values, *maxBuilding)
	}
	if len(columns) == 0 {
		return nil
	}
	_, err := tx.conn.ExecContext(tx.ctx, "UPDATE dispatch_settings SET "+strings.Join(columns, ", ")+" WHERE id=1", values...)
	return err
}

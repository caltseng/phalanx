package sqlite

import "strings"

type Captain struct {
	ID, SessionRef, CharterRevision, State, CreatedAt, HeartbeatAt string
	PickupJSON, FocusJSON, SessionsJSON                            string
	ParentID, HandoffNote, EndingAt                                *string
	WakeCount, ReinforcedInterval                                  int64
	CompactionCount                                                *int64
}

const captainColumns = `id,session_ref,charter_revision,state,created_at,heartbeat_at,
	pickup_json,focus_json,sessions_json,parent_id,handoff_note,ending_at,wake_count,reinforced_interval,compaction_count`

func scanCaptain(row scanner) (Captain, error) {
	var result Captain
	err := row.Scan(&result.ID, &result.SessionRef, &result.CharterRevision, &result.State, &result.CreatedAt,
		&result.HeartbeatAt, &result.PickupJSON, &result.FocusJSON, &result.SessionsJSON,
		&result.ParentID, &result.HandoffNote, &result.EndingAt, &result.WakeCount, &result.ReinforcedInterval, &result.CompactionCount)
	return result, err
}

func (tx *Tx) Captain(id string) (Captain, error) {
	return scanCaptain(tx.conn.QueryRowContext(tx.ctx, "SELECT "+captainColumns+" FROM captains WHERE id=?", id))
}

func (tx *Tx) Captains(limit int) ([]Captain, error) {
	rows, err := tx.conn.QueryContext(tx.ctx, "SELECT "+captainColumns+" FROM captains ORDER BY state='ended',heartbeat_at DESC,id LIMIT ?", limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := []Captain{}
	for rows.Next() {
		row, err := scanCaptain(rows)
		if err != nil {
			return nil, err
		}
		result = append(result, row)
	}
	return result, rows.Err()
}

func (tx *Tx) AllCaptains() ([]Captain, error) {
	rows, err := tx.conn.QueryContext(tx.ctx, "SELECT "+captainColumns+" FROM captains ORDER BY heartbeat_at,id")
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := []Captain{}
	for rows.Next() {
		row, err := scanCaptain(rows)
		if err != nil {
			return nil, err
		}
		result = append(result, row)
	}
	return result, rows.Err()
}

func (tx *Tx) InsertCaptain(id, reference, revision, pickup, now string, parentID *string) error {
	_, err := tx.conn.ExecContext(tx.ctx, `INSERT INTO captains
		(id,session_ref,charter_revision,parent_id,state,created_at,heartbeat_at,pickup_json)
		VALUES (?,?,?,?,'active',?,?,?)`, id, reference, revision, parentID, now, now, pickup)
	return err
}

func (tx *Tx) CaptainPickup(id, pickup string) error {
	_, err := tx.conn.ExecContext(tx.ctx, "UPDATE captains SET pickup_json=? WHERE id=?", pickup, id)
	return err
}

func (tx *Tx) CaptainHeartbeat(id, revision, state, focus, sessions, now string, wake int64, handoff, ending *string, compaction *int64) error {
	_, err := tx.conn.ExecContext(tx.ctx, `UPDATE captains SET charter_revision=?,wake_count=?,state=?,focus_json=?,sessions_json=?,
		handoff_note=?,heartbeat_at=?,ending_at=?,compaction_count=? WHERE id=?`,
		revision, wake, state, focus, sessions, handoff, now, ending, compaction, id)
	return err
}

func (tx *Tx) CaptainReinforcement(id string, interval int64) error {
	_, err := tx.conn.ExecContext(tx.ctx, "UPDATE captains SET reinforced_interval=max(reinforced_interval,?) WHERE id=?", interval, id)
	return err
}

func (tx *Tx) InsertCaptainAction(id, executionID, captainID string, version int64, action, reason, now string) error {
	_, err := tx.conn.ExecContext(tx.ctx, "INSERT INTO captain_actions VALUES (?,?,?,?,?,?,?)",
		id, executionID, captainID, version, action, reason, now)
	return err
}

type CaptainDetails struct {
	OpenIntents    []map[string]any `json:"open_intents"`
	Successors     []string         `json:"successors"`
	CleanupPending int64            `json:"cleanup_pending"`
	EndedWriters   []map[string]any `json:"ended_writers"`
}

func (tx *Tx) CaptainDetails(captainID string) (CaptainDetails, error) {
	details, err := tx.CaptainDetailsBatch([]string{captainID})
	return details[captainID], err
}

func (tx *Tx) CaptainDetailsBatch(captainIDs []string) (map[string]CaptainDetails, error) {
	result := make(map[string]CaptainDetails, len(captainIDs))
	parameters := make([]any, 0, len(captainIDs))
	for _, id := range captainIDs {
		if _, exists := result[id]; exists {
			continue
		}
		result[id] = CaptainDetails{OpenIntents: []map[string]any{}, Successors: []string{}, EndedWriters: []map[string]any{}}
		parameters = append(parameters, id)
	}
	if len(parameters) == 0 {
		return result, nil
	}
	placeholders := strings.TrimSuffix(strings.Repeat("?,", len(parameters)), ",")
	rows, err := tx.conn.QueryContext(tx.ctx, `SELECT captain_id,id,item_id,execution_id,nonce,created_at FROM writers
		WHERE captain_id IN (`+placeholders+`) AND confirmed_at IS NULL AND abandoned_at IS NULL AND ended_at IS NULL
		ORDER BY created_at,id`, parameters...)
	if err != nil {
		return result, err
	}
	for rows.Next() {
		var captainID, id, item, execution, nonce, created string
		if err := rows.Scan(&captainID, &id, &item, &execution, &nonce, &created); err != nil {
			rows.Close()
			return result, err
		}
		detail := result[captainID]
		detail.OpenIntents = append(detail.OpenIntents, map[string]any{"writer_id": id, "item_id": item, "execution_id": execution, "nonce": nonce, "created_at": created})
		result[captainID] = detail
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return result, err
	}
	rows.Close()
	rows, err = tx.conn.QueryContext(tx.ctx, "SELECT parent_id,id FROM captains WHERE parent_id IN ("+placeholders+") ORDER BY created_at,id", parameters...)
	if err != nil {
		return result, err
	}
	for rows.Next() {
		var captainID, id string
		if err := rows.Scan(&captainID, &id); err != nil {
			rows.Close()
			return result, err
		}
		detail := result[captainID]
		detail.Successors = append(detail.Successors, id)
		result[captainID] = detail
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return result, err
	}
	rows.Close()
	rows, err = tx.conn.QueryContext(tx.ctx, `SELECT w.captain_id,count(*) FROM writers w LEFT JOIN session_archives a ON a.session_id=w.session_id
		WHERE w.captain_id IN (`+placeholders+`) AND w.session_id IS NOT NULL
		AND (w.ended_at IS NULL OR a.outcome IS NULL OR a.outcome='blocked') GROUP BY w.captain_id`, parameters...)
	if err != nil {
		return result, err
	}
	for rows.Next() {
		var captainID string
		var pending int64
		if err := rows.Scan(&captainID, &pending); err != nil {
			rows.Close()
			return result, err
		}
		detail := result[captainID]
		detail.CleanupPending = pending
		result[captainID] = detail
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return result, err
	}
	rows.Close()
	rows, err = tx.conn.QueryContext(tx.ctx, `SELECT captain_id,id,item_id,execution_id,session_id,ended_at,end_reason,archive_outcome,archive_reason
		FROM (SELECT w.captain_id,w.id,w.item_id,w.execution_id,w.session_id,w.ended_at,w.end_reason,
			a.outcome AS archive_outcome,a.reason AS archive_reason,
			row_number() OVER (PARTITION BY w.captain_id ORDER BY a.updated_at,w.ended_at,w.id) AS position
			FROM writers w LEFT JOIN session_archives a ON a.session_id=w.session_id
			WHERE w.captain_id IN (`+placeholders+`) AND w.ended_at IS NOT NULL AND w.session_id IS NOT NULL
			AND (a.outcome IS NULL OR a.outcome='blocked'))
		WHERE position<=100 ORDER BY captain_id,position`, parameters...)
	if err != nil {
		return result, err
	}
	defer rows.Close()
	for rows.Next() {
		var captainID, id, item, execution string
		var session, ended, reason, outcome, archiveReason *string
		if err := rows.Scan(&captainID, &id, &item, &execution, &session, &ended, &reason, &outcome, &archiveReason); err != nil {
			return result, err
		}
		detail := result[captainID]
		detail.EndedWriters = append(detail.EndedWriters, map[string]any{"id": id, "item_id": item, "execution_id": execution,
			"session_id": session, "ended_at": ended, "end_reason": reason, "archive_outcome": outcome, "archive_reason": archiveReason})
		result[captainID] = detail
	}
	return result, rows.Err()
}

type SessionAvailability struct {
	ID, Runtime           *string
	Availability, Session string
}

func (tx *Tx) CaptainAvailability() ([]SessionAvailability, error) {
	rows, err := tx.conn.QueryContext(tx.ctx, `SELECT json_extract(s.value,'$.id'),
		json_extract(s.value,'$.runtime'),json_extract(s.value,'$.availability'),c.session_ref
		FROM captains c,json_each(c.sessions_json) s
		WHERE json_extract(s.value,'$.availability') IN ('present','missing','deleted')
		ORDER BY c.heartbeat_at,c.rowid`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := []SessionAvailability{}
	for rows.Next() {
		var row SessionAvailability
		if err := rows.Scan(&row.ID, &row.Runtime, &row.Availability, &row.Session); err != nil {
			return nil, err
		}
		result = append(result, row)
	}
	return result, rows.Err()
}

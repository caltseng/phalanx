package sqlite

import (
	"encoding/json"
	"errors"
)

type InboxEntry struct {
	ItemID       string          `json:"item_id"`
	ExecutionID  string          `json:"execution_id"`
	Stage        string          `json:"stage"`
	Version      int64           `json:"version"`
	WaitingSince string          `json:"waiting_since"`
	Wait         json.RawMessage `json:"wait"`
	Question     json.RawMessage `json:"question"`
}

func (tx *Tx) Inbox(limit int, afterTime, afterID string) ([]InboxEntry, error) {
	rows, err := tx.conn.QueryContext(tx.ctx, `SELECT queued.item_id, queued.id, queued.stage, queued.version,
		queued.updated_at, queued.state_json, history.details_json FROM (
		SELECT id, item_id, stage, version, updated_at, state_json
		FROM executions WHERE status='waiting_for_input' AND (updated_at,id)>(?,?)
		ORDER BY updated_at,id LIMIT ?) AS queued
		JOIN transition_history AS history ON history.execution_id=queued.id AND history.version=queued.version
		ORDER BY queued.updated_at, queued.id`, afterTime, afterID, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := []InboxEntry{}
	for rows.Next() {
		var entry InboxEntry
		var stateJSON, detailsJSON string
		if err := rows.Scan(&entry.ItemID, &entry.ExecutionID, &entry.Stage, &entry.Version, &entry.WaitingSince,
			&stateJSON, &detailsJSON); err != nil {
			return nil, err
		}
		var state, details map[string]json.RawMessage
		if err := json.Unmarshal([]byte(stateJSON), &state); err != nil {
			return nil, err
		}
		if err := json.Unmarshal([]byte(detailsJSON), &details); err != nil {
			return nil, err
		}
		wait, ok := state["wait"]
		if !ok {
			return nil, errors.New("Stored input-wait state has no wait field")
		}
		entry.Wait, entry.Question = wait, details["question"]
		result = append(result, entry)
	}
	return result, rows.Err()
}

package sqlite

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"sort"
)

const executionColumns = `id, previous_id, version, created_at, updated_at, state_json,
	ready_since, retry_at, retry_count, coalesce(dependency_wait_json, '[]')`

type scanner interface {
	Scan(...any) error
}

func scanExecution(row scanner) (Execution, error) {
	var result Execution
	var state, dependencies string
	err := row.Scan(&result.ID, &result.PreviousID, &result.Version, &result.CreatedAt, &result.UpdatedAt,
		&state, &result.ReadySince, &result.RetryAt, &result.RetryCount, &dependencies)
	result.State = json.RawMessage(state)
	result.UnmetDependencies = json.RawMessage(dependencies)
	return result, err
}

func (tx *Tx) Execution(itemID, executionID string) (Execution, error) {
	return scanExecution(tx.conn.QueryRowContext(tx.ctx,
		"SELECT "+executionColumns+" FROM executions WHERE item_id=? AND id=?", itemID, executionID))
}

func (tx *Tx) Executions(itemID string) ([]Execution, error) {
	rows, err := tx.conn.QueryContext(tx.ctx,
		"SELECT "+executionColumns+" FROM executions WHERE item_id=? ORDER BY created_at, id", itemID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := []Execution{}
	for rows.Next() {
		execution, err := scanExecution(rows)
		if err != nil {
			return nil, err
		}
		result = append(result, execution)
	}
	return result, rows.Err()
}

type DisplayTitle struct {
	ItemID    string  `json:"item_id"`
	Revision  int64   `json:"revision"`
	Title     *string `json:"title"`
	ChangedBy *string `json:"changed_by"`
	CreatedAt *string `json:"created_at"`
}

func (tx *Tx) Title(itemID string) (DisplayTitle, error) {
	result := DisplayTitle{ItemID: itemID}
	err := tx.conn.QueryRowContext(tx.ctx,
		"SELECT revision, title, changed_by, created_at FROM item_titles WHERE item_id=? ORDER BY revision DESC LIMIT 1",
		itemID).Scan(&result.Revision, &result.Title, &result.ChangedBy, &result.CreatedAt)
	if err == sql.ErrNoRows {
		return result, nil
	}
	return result, err
}

func (tx *Tx) InsertTitle(itemID string, revision int64, title *string, now string) error {
	_, err := tx.conn.ExecContext(tx.ctx,
		"INSERT INTO item_titles VALUES (?, ?, ?, 'owner', ?)", itemID, revision, title, now)
	return err
}

func (tx *Tx) Draft(executionID, revision string) (string, error) {
	var result string
	err := tx.conn.QueryRowContext(tx.ctx,
		"SELECT markdown FROM report_drafts WHERE execution_id=? AND revision=?", executionID, revision).Scan(&result)
	return result, err
}

type TimelineEvent struct {
	Version   int64           `json:"version"`
	Event     string          `json:"event"`
	AttemptID *string         `json:"attempt_id"`
	CreatedAt string          `json:"created_at"`
	Details   json.RawMessage `json:"details"`
	State     json.RawMessage `json:"state"`
}

func (tx *Tx) Timeline(executionID string) ([]TimelineEvent, error) {
	result := []TimelineEvent{}
	states := map[int64]json.RawMessage{}
	rows, err := tx.conn.QueryContext(tx.ctx, `SELECT version, event, attempt_id, created_at, details_json, state_json
		FROM transition_history WHERE execution_id=? ORDER BY version`, executionID)
	if err != nil {
		return nil, err
	}
	for rows.Next() {
		var event TimelineEvent
		var details, state string
		if err := rows.Scan(&event.Version, &event.Event, &event.AttemptID, &event.CreatedAt, &details, &state); err != nil {
			rows.Close()
			return nil, err
		}
		event.Details, event.State = json.RawMessage(details), json.RawMessage(state)
		states[event.Version] = event.State
		result = append(result, event)
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return nil, err
	}
	rows.Close()
	// This union preserves Python's append order on equal version/time ties.
	rows, err = tx.conn.QueryContext(tx.ctx, `SELECT version, 'github_observation', observed_at,
		json_set(observation_json, '$.last_observed_at', last_observed_at)
		FROM github_observations WHERE execution_id=?
		UNION ALL SELECT version, 'local_checks', checked_at,
		json_object('head', head_sha, 'failures', json(failures_json))
		FROM local_check_results WHERE execution_id=?
		UNION ALL SELECT version, 'captain_action', created_at,
		json_object('captain_id', captain_id, 'action', action, 'reason', reason)
		FROM captain_actions WHERE execution_id=?`, executionID, executionID, executionID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var event TimelineEvent
		var details string
		if err := rows.Scan(&event.Version, &event.Event, &event.CreatedAt, &details); err != nil {
			return nil, err
		}
		state, ok := states[event.Version]
		if !ok {
			return nil, fmt.Errorf("Timeline evidence references absent transition version %d", event.Version)
		}
		event.Details, event.State = json.RawMessage(details), state
		result = append(result, event)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	sort.SliceStable(result, func(i, j int) bool {
		if result[i].Version != result[j].Version {
			return result[i].Version < result[j].Version
		}
		return result[i].CreatedAt < result[j].CreatedAt
	})
	return result, nil
}

package sqlite

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"strconv"
)

const terminalStatusesSQL = "('completed','failed','cancelled')"

// Number retains SQLite INTEGER/REAL identity at the JSON boundary.
type Number json.Number

func (n *Number) Scan(value any) error {
	switch value := value.(type) {
	case int64:
		*n = Number(strconv.FormatInt(value, 10))
	case float64:
		*n = Number(strconv.FormatFloat(value, 'e', -1, 64))
	default:
		return fmt.Errorf("Stored PR metric is not numeric: %T", value)
	}
	return nil
}

func (n Number) JSONValue() (any, error) {
	if integer, err := json.Number(n).Int64(); err == nil {
		return integer, nil
	}
	return json.Number(n).Float64()
}

type OperatorRow struct {
	ID, ItemID, CreatedAt, UpdatedAt, Stage, Status, StateJSON string
	Version                                                    int64
	RetryAt, ReadySince                                        *int64
	HandoffHead                                                *string
	ItemCreatedAt, RepositoryID                                string
	Label, Goal, DisplayTitle, DependencyWait, TargetBranch    *string
	QueuePriority                                              int64
	LastAdvancedAt                                             string
	WaitSince, ActiveStartedAt                                 *string
	RevisionRound                                              int64
	PRURL, PRState, PRHeadSHA, PRCI, PRCIHeadSHA               *string
	PRNumber, PRAdditions, PRDeletions, DuplicateCount         *Number
	PRDraft, PRMerged                                          *bool
	BlockedCount                                               int64
	WriterID, WriterState, WriterRuntime, WriterCaptainID      *string
	WriterSessionID, WriterSessionURL, WriterProgressAt        *string
	WriterProgressNote, WriterEndedAt, WriterNonce             *string
	WriterCreatedAt, WriterConfirmedAt, WriterAbandonedAt      *string
	WriterBuilding                                             *bool
	BuildingRequestedAt, DeploymentJSON, StageOwner            *string
}

const operatorSummarySQL = `SELECT execution.id,execution.item_id,execution.version,execution.created_at,execution.updated_at,
	execution.stage,execution.status,execution.state_json,execution.retry_at,execution.handoff_head,execution.ready_since,
	item.created_at,item.label,item.repository_id,json_extract(item.request_json,'$.goal'),title.title,
	execution.queue_priority,execution.dependency_wait_json,json_extract(item.request_json,'$.target_branch'),
	progress.last_advanced_at,progress.wait_since,progress.revision_round,
	(SELECT min(started_at) FROM attempts WHERE execution_id=execution.id),
	pr.url,pr.number,pr.state,pr.draft,pr.merged,pr.head_sha,pr.additions,pr.deletions,pr.ci,pr.ci_head_sha,pr.duplicate_count,
	coalesce(json_array_length(execution.dependency_wait_json),0),
	writer.id,writer.state,writer.runtime,writer.captain_id,writer.session_id,writer.session_url,writer.last_progress_at,
	writer.progress_note,writer.ended_at,writer.nonce,writer.created_at,writer.confirmed_at,writer.abandoned_at,
	writer.building,writer.building_requested_at,
	(SELECT json_extract(observation.observation_json,'$.deployment') FROM github_observations observation
		WHERE observation.execution_id=execution.id ORDER BY observation.last_observed_at DESC,observation.observed_at DESC LIMIT 1),
	(SELECT attempt.id FROM attempts attempt WHERE attempt.execution_id=execution.id AND attempt.ended_at IS NULL)
	FROM executions AS execution JOIN work_items AS item ON item.id=execution.item_id
	LEFT JOIN item_titles title ON title.item_id=item.id AND title.revision=(SELECT max(revision) FROM item_titles WHERE item_id=item.id)
	JOIN operator_progress progress ON progress.execution_id=execution.id
	LEFT JOIN operator_pr pr ON pr.execution_id=execution.id
	LEFT JOIN(SELECT execution_id,max(rowid) AS latest_row FROM writers GROUP BY execution_id) latest_writer
		ON latest_writer.execution_id=execution.id
	LEFT JOIN writers writer ON writer.rowid=latest_writer.latest_row`

func scanOperatorRow(row scanner) (OperatorRow, error) {
	var r OperatorRow
	err := row.Scan(&r.ID, &r.ItemID, &r.Version, &r.CreatedAt, &r.UpdatedAt, &r.Stage, &r.Status, &r.StateJSON,
		&r.RetryAt, &r.HandoffHead, &r.ReadySince, &r.ItemCreatedAt, &r.Label, &r.RepositoryID, &r.Goal, &r.DisplayTitle,
		&r.QueuePriority, &r.DependencyWait, &r.TargetBranch, &r.LastAdvancedAt, &r.WaitSince, &r.RevisionRound, &r.ActiveStartedAt,
		&r.PRURL, &r.PRNumber, &r.PRState, &r.PRDraft, &r.PRMerged, &r.PRHeadSHA, &r.PRAdditions, &r.PRDeletions,
		&r.PRCI, &r.PRCIHeadSHA, &r.DuplicateCount, &r.BlockedCount, &r.WriterID, &r.WriterState, &r.WriterRuntime,
		&r.WriterCaptainID, &r.WriterSessionID, &r.WriterSessionURL, &r.WriterProgressAt, &r.WriterProgressNote, &r.WriterEndedAt,
		&r.WriterNonce, &r.WriterCreatedAt, &r.WriterConfirmedAt, &r.WriterAbandonedAt, &r.WriterBuilding, &r.BuildingRequestedAt,
		&r.DeploymentJSON, &r.StageOwner)
	return r, err
}

func (tx *Tx) operatorRows(query string, values []any) ([]OperatorRow, error) {
	rows, err := tx.conn.QueryContext(tx.ctx, query, values...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := []OperatorRow{}
	for rows.Next() {
		row, err := scanOperatorRow(rows)
		if err != nil {
			return nil, err
		}
		result = append(result, row)
	}
	return result, rows.Err()
}

func sqlCursor(value any) any {
	if number, ok := value.(json.Number); ok {
		integer, err := number.Int64()
		if err == nil {
			return integer
		}
	}
	return value
}

func openBucket(selectSQL string, rank int64, after, through []any, where string, parameters []any, limit int) (string, []any) {
	query := selectSQL + " WHERE execution.status NOT IN " + terminalStatusesSQL +
		" AND(CASE execution.status WHEN 'waiting_for_input' THEN 0 ELSE 1 END)=?"
	values := []any{rank}
	if after != nil && sqlCursor(after[0]) == rank {
		query += " AND(execution.created_at,execution.id)>(?,?)"
		values = append(values, after[1], after[2])
	}
	if through != nil && sqlCursor(through[0]) == rank {
		query += " AND(execution.created_at,execution.id)<=(?,?)"
		values = append(values, through[1], through[2])
	}
	query += where + " ORDER BY execution.created_at,execution.id LIMIT ?"
	values = append(values, parameters...)
	values = append(values, limit)
	return query, values
}

func cursorRank(key []any, fallback int64) int64 {
	if key == nil {
		return fallback
	}
	return sqlCursor(key[0]).(int64)
}

func (tx *Tx) OperatorOpenPage(after, through []any, size int, where string, parameters []any) ([]OperatorRow, error) {
	result := []OperatorRow{}
	for rank := cursorRank(after, 0); rank < 2; rank++ {
		if through != nil && rank > cursorRank(through, 1) {
			break
		}
		query, values := openBucket(operatorSummarySQL, rank, after, through, where, parameters, size-len(result))
		bucket, err := tx.operatorRows(query, values)
		if err != nil {
			return nil, err
		}
		result = append(result, bucket...)
		if len(result) == size {
			break
		}
	}
	return result, nil
}

func (tx *Tx) OperatorOpenCandidates(after []any, size int) ([][]any, error) {
	result := [][]any{}
	for rank := cursorRank(after, 0); rank < 2; rank++ {
		query, values := openBucket("SELECT execution.created_at,execution.id FROM executions execution",
			rank, after, nil, "", nil, size-len(result))
		bucket, err := tx.operatorCandidates(query, values, rank)
		if err != nil {
			return nil, err
		}
		result = append(result, bucket...)
		if len(result) == size {
			break
		}
	}
	return result, nil
}

func (tx *Tx) OperatorClosedPage(after, through []any, size int, where string, parameters []any) ([]OperatorRow, error) {
	query := operatorSummarySQL + " WHERE execution.status IN " + terminalStatusesSQL
	values := []any{}
	if after != nil {
		query += " AND(execution.updated_at,execution.id)<(?,?)"
		values = append(values, after...)
	}
	if through != nil {
		query += " AND(execution.updated_at,execution.id)>=(?,?)"
		values = append(values, through...)
	}
	query += where + " ORDER BY execution.updated_at DESC,execution.id DESC LIMIT ?"
	values = append(values, parameters...)
	values = append(values, size)
	return tx.operatorRows(query, values)
}

func (tx *Tx) OperatorClosedCandidates(after []any, floor *string, size int) ([][]any, error) {
	query := "SELECT execution.updated_at,execution.id FROM executions execution WHERE status IN " + terminalStatusesSQL
	values := []any{}
	if after != nil {
		query += " AND(execution.updated_at,execution.id)<(?,?)"
		values = append(values, after...)
	}
	if floor != nil {
		query += " AND execution.updated_at>?"
		values = append(values, *floor)
	}
	query += " ORDER BY execution.updated_at DESC,execution.id DESC LIMIT ?"
	values = append(values, size)
	return tx.operatorCandidates(query, values)
}

func (tx *Tx) operatorCandidates(query string, values []any, prefix ...any) ([][]any, error) {
	rows, err := tx.conn.QueryContext(tx.ctx, query, values...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := [][]any{}
	for rows.Next() {
		var stamp, id string
		if err := rows.Scan(&stamp, &id); err != nil {
			return nil, err
		}
		key := append([]any{}, prefix...)
		result = append(result, append(key, stamp, id))
	}
	return result, rows.Err()
}

func (tx *Tx) OperatorCurrentItem(itemID string) (OperatorRow, error) {
	return scanOperatorRow(tx.conn.QueryRowContext(tx.ctx, operatorSummarySQL+
		" WHERE execution.item_id=? AND NOT EXISTS(SELECT 1 FROM executions later WHERE later.previous_id=execution.id)", itemID))
}

func (tx *Tx) OperatorExecution(executionID string) (OperatorRow, error) {
	return scanOperatorRow(tx.conn.QueryRowContext(tx.ctx, operatorSummarySQL+" WHERE execution.id=?", executionID))
}

func (tx *Tx) OperatorGroupExists(label string) (bool, error) {
	var exists bool
	err := tx.conn.QueryRowContext(tx.ctx, "SELECT EXISTS(SELECT 1 FROM work_items WHERE label=?)", label).Scan(&exists)
	return exists, err
}

func (tx *Tx) OperatorGroup(label string, after []any, limit int) ([]OperatorRow, error) {
	query := "WITH group_page AS MATERIALIZED(SELECT item.id FROM work_items item WHERE item.label=?"
	values := []any{label}
	if after != nil {
		query += " AND(item.created_at,item.id)>(?,?)"
		values = append(values, after...)
	}
	query += " ORDER BY item.created_at,item.id LIMIT ?) " + operatorSummarySQL +
		" WHERE item.id IN(SELECT id FROM group_page) AND NOT EXISTS(SELECT 1 FROM executions later WHERE later.previous_id=execution.id)" +
		" ORDER BY item.created_at,item.id"
	values = append(values, limit)
	return tx.operatorRows(query, values)
}

func (tx *Tx) OperatorBrief(itemID string) (*string, *string, error) {
	var criteria, context *string
	err := tx.conn.QueryRowContext(tx.ctx,
		"SELECT json_extract(request_json,'$.acceptance_criteria'),json_extract(request_json,'$.context_links') FROM work_items WHERE id=?",
		itemID).Scan(&criteria, &context)
	return criteria, context, err
}

func (tx *Tx) OperatorDeadlines(nowMS int64, launchCutoff, closedCutoff string) ([]any, error) {
	var retry, ready *int64
	var launch, closed *string
	err := tx.conn.QueryRowContext(tx.ctx, `SELECT
		(SELECT max(retry_at) FROM executions WHERE status NOT IN `+terminalStatusesSQL+` AND retry_at<=?),
		(SELECT max(ready_since) FROM executions WHERE status NOT IN `+terminalStatusesSQL+` AND ready_since<=?),
		(SELECT max(created_at) FROM writers WHERE ended_at IS NULL AND confirmed_at IS NULL AND created_at<=?),
		(SELECT max(updated_at) FROM executions WHERE status IN `+terminalStatusesSQL+` AND updated_at<?)`,
		nowMS, nowMS, launchCutoff, closedCutoff).Scan(&retry, &ready, &launch, &closed)
	return []any{retry, ready, launch, closed}, err
}

func (tx *Tx) OperatorChangeToken() (string, error) {
	var result string
	err := tx.conn.QueryRowContext(tx.ctx, "SELECT token FROM operator_change").Scan(&result)
	return result, err
}

type OperatorOpenCount struct {
	Status  string
	Blocked bool
	Count   int64
}

func (tx *Tx) OperatorOpenCounts() ([]OperatorOpenCount, error) {
	rows, err := tx.conn.QueryContext(tx.ctx, `SELECT status,dependency_wait_json IS NOT NULL,count(*) FROM executions
		WHERE status NOT IN `+terminalStatusesSQL+` GROUP BY status,dependency_wait_json IS NOT NULL`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := []OperatorOpenCount{}
	for rows.Next() {
		var row OperatorOpenCount
		if err := rows.Scan(&row.Status, &row.Blocked, &row.Count); err != nil {
			return nil, err
		}
		result = append(result, row)
	}
	return result, rows.Err()
}

func (tx *Tx) OperatorClosedCounts(cutoff string) (map[string]int64, error) {
	rows, err := tx.conn.QueryContext(tx.ctx, `SELECT outcome,count(*) FROM executions
		WHERE status IN `+terminalStatusesSQL+` AND updated_at>=? GROUP BY outcome`, cutoff)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := map[string]int64{}
	for rows.Next() {
		var name string
		var count int64
		if err := rows.Scan(&name, &count); err != nil {
			return nil, err
		}
		result[name] = count
	}
	return result, rows.Err()
}

func (tx *Tx) OperatorOldestQueue(nowMS int64) (*int64, error) {
	var result *int64
	err := tx.conn.QueryRowContext(tx.ctx, `SELECT min(ready_since) FROM executions
		WHERE status='queued' AND dependency_wait_json IS NULL AND ready_since<=?`, nowMS).Scan(&result)
	return result, err
}

func (tx *Tx) OperatorRelatives(executionID string) (*string, *string, error) {
	var previous, next *string
	err := tx.conn.QueryRowContext(tx.ctx, `SELECT previous_id,
		(SELECT id FROM executions later WHERE later.previous_id=execution.id)
		FROM executions execution WHERE id=?`, executionID).Scan(&previous, &next)
	return previous, next, err
}

type OperatorPrerequisite struct {
	ItemID                   string
	Title, Milestone, Reason *string
}

func (tx *Tx) OperatorPrerequisites(executionID string) ([]OperatorPrerequisite, error) {
	rows, err := tx.conn.QueryContext(tx.ctx, `SELECT json_extract(block.value,'$.upstream_id'),
		coalesce((SELECT title FROM item_titles WHERE item_id=upstream.id ORDER BY revision DESC LIMIT 1),
			json_extract(upstream.request_json,'$.goal')),
		json_extract(block.value,'$.milestone'),json_extract(block.value,'$.reason')
		FROM executions execution JOIN json_each(execution.dependency_wait_json) block
		JOIN work_items upstream ON upstream.id=json_extract(block.value,'$.upstream_id')
		WHERE execution.id=? ORDER BY block.key`, executionID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := []OperatorPrerequisite{}
	for rows.Next() {
		var row OperatorPrerequisite
		if err := rows.Scan(&row.ItemID, &row.Title, &row.Milestone, &row.Reason); err != nil {
			return nil, err
		}
		result = append(result, row)
	}
	return result, rows.Err()
}

func (tx *Tx) OperatorQuestion(executionID string, version int64) (*string, error) {
	var question *string
	err := tx.conn.QueryRowContext(tx.ctx,
		"SELECT json_extract(details_json,'$.question') FROM transition_history WHERE execution_id=? AND version=?",
		executionID, version).Scan(&question)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	return question, err
}

type OperatorEvent struct {
	Version    int64   `json:"version"`
	Event      string  `json:"event"`
	CreatedAt  string  `json:"created_at"`
	AttemptID  *string `json:"attempt_id"`
	Stage      *string `json:"stage"`
	Status     *string `json:"status"`
	FromStage  *string `json:"from_stage"`
	Reason     *string `json:"reason"`
	AnsweredBy *string `json:"answered_by"`
	Details    *string `json:"-"`
}

func (tx *Tx) OperatorEvents(executionID string, after []any, limit int) ([]OperatorEvent, error) {
	query := `WITH events AS(
		SELECT event.version,event.event,event.created_at,event.attempt_id,
		json_extract(event.state_json,'$.stage') AS stage,json_extract(event.state_json,'$.status') AS status,
		CASE WHEN json_extract(prior.state_json,'$.stage')<>json_extract(event.state_json,'$.stage')
			THEN json_extract(prior.state_json,'$.stage') END AS from_stage,
		coalesce(json_extract(event.details_json,'$.reason'),
			CASE WHEN event.event='reply' THEN 'Answered by '||coalesce(json_extract(event.details_json,'$.answered_by'),'owner') END,
			json_extract(event.state_json,'$.wait.reason')) AS reason,
		CASE WHEN event.event='reply' THEN coalesce(json_extract(event.details_json,'$.answered_by'),'owner') END AS answered_by,
		event.details_json AS details
		FROM transition_history event LEFT JOIN transition_history prior
			ON prior.execution_id=event.execution_id AND prior.version=event.version-1 WHERE event.execution_id=?
		UNION ALL SELECT observation.version,'github_observation',observation.observed_at,NULL,
			json_extract(observed.state_json,'$.stage'),json_extract(observed.state_json,'$.status'),NULL,
			coalesce('Observed '||json_extract(observation.observation_json,'$.pr.url')||' '||
				json_extract(observation.observation_json,'$.pr.state')||' at '||observation.head_sha,
				'Observed no PR for the item branch'),NULL,NULL
		FROM github_observations observation JOIN transition_history observed
			ON observed.execution_id=observation.execution_id AND observed.version=observation.version
		WHERE observation.execution_id=?
		UNION ALL SELECT result.version,'local_checks',result.checked_at,NULL,
			json_extract(checked.state_json,'$.stage'),json_extract(checked.state_json,'$.status'),NULL,
			CASE WHEN json_array_length(result.failures_json)=0 THEN 'Local checks passed on ' ELSE 'Local checks failed on ' END||result.head_sha,
			NULL,NULL FROM local_check_results result JOIN transition_history checked
			ON checked.execution_id=result.execution_id AND checked.version=result.version WHERE result.execution_id=?
		UNION ALL SELECT action.version,'captain_action',action.created_at,NULL,
			json_extract(acted.state_json,'$.stage'),json_extract(acted.state_json,'$.status'),NULL,
			'Captain '||substr(action.captain_id,1,8)||' '||action.action||': '||action.reason,NULL,NULL
		FROM captain_actions action JOIN transition_history acted
			ON acted.execution_id=action.execution_id AND acted.version=action.version WHERE action.execution_id=?)
		SELECT events.* FROM events`
	values := []any{executionID, executionID, executionID, executionID}
	if after != nil {
		query += " WHERE(events.version,events.created_at)<(?,?)"
		values = append(values, sqlCursor(after[0]), after[1])
	}
	query += " ORDER BY events.version DESC,events.created_at DESC LIMIT ?"
	values = append(values, limit)
	rows, err := tx.conn.QueryContext(tx.ctx, query, values...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := []OperatorEvent{}
	for rows.Next() {
		var row OperatorEvent
		if err := rows.Scan(&row.Version, &row.Event, &row.CreatedAt, &row.AttemptID, &row.Stage, &row.Status,
			&row.FromStage, &row.Reason, &row.AnsweredBy, &row.Details); err != nil {
			return nil, err
		}
		result = append(result, row)
	}
	return result, rows.Err()
}

func (tx *Tx) MusterWriters() ([]map[string]any, error) {
	rows, err := tx.conn.QueryContext(tx.ctx, `SELECT writer.id,writer.item_id,writer.execution_id,writer.captain_id,
		writer.state,writer.session_id,writer.session_url,writer.last_progress_at
		FROM writers writer JOIN executions execution ON execution.id=writer.execution_id
		WHERE writer.ended_at IS NULL AND writer.abandoned_at IS NULL AND execution.status NOT IN `+terminalStatusesSQL+`
		ORDER BY writer.id`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := []map[string]any{}
	for rows.Next() {
		var id, item, execution, captain, state string
		var session, url, progress *string
		if err := rows.Scan(&id, &item, &execution, &captain, &state, &session, &url, &progress); err != nil {
			return nil, err
		}
		result = append(result, map[string]any{"id": id, "item_id": item, "execution_id": execution, "captain_id": captain,
			"state": state, "session_id": session, "session_url": url, "last_progress_at": progress})
	}
	return result, rows.Err()
}

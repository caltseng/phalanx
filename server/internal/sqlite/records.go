package sqlite

import (
	"database/sql"
	"encoding/json"
)

type Repository struct {
	ID        string `json:"id"`
	Remote    string `json:"remote"`
	CreatedAt string `json:"created_at"`
}

func (tx *Tx) Repository(id string) (Repository, error) {
	var result Repository
	err := tx.conn.QueryRowContext(tx.ctx, "SELECT id, remote, created_at FROM repositories WHERE id=?", id).Scan(&result.ID, &result.Remote, &result.CreatedAt)
	return result, err
}

func (tx *Tx) Register(id, remote, now string) error {
	_, err := tx.conn.ExecContext(tx.ctx, "INSERT INTO repositories VALUES (?, ?, ?) ON CONFLICT DO NOTHING", id, remote, now)
	return err
}

func (tx *Tx) Repositories() ([]Repository, error) {
	rows, err := tx.conn.QueryContext(tx.ctx, "SELECT id, remote, created_at FROM repositories ORDER BY id")
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := []Repository{}
	for rows.Next() {
		var row Repository
		if err := rows.Scan(&row.ID, &row.Remote, &row.CreatedAt); err != nil {
			return nil, err
		}
		result = append(result, row)
	}
	return result, rows.Err()
}

type Receipt struct {
	Digest   string
	Response string
}

func (tx *Tx) Receipt(scope, key string) (Receipt, error) {
	var result Receipt
	err := tx.conn.QueryRowContext(tx.ctx, "SELECT request_digest, response_json FROM request_receipts WHERE scope=? AND idempotency_key=?", scope, key).Scan(&result.Digest, &result.Response)
	return result, err
}

func (tx *Tx) SaveReceipt(scope, key, digest, response string) error {
	_, err := tx.conn.ExecContext(tx.ctx, "INSERT INTO request_receipts VALUES (?, ?, ?, ?)", scope, key, digest, response)
	return err
}

type Item struct {
	ID           string
	RepositoryID string
	Request      string
	CreatedAt    string
}

func (tx *Tx) Item(id string) (Item, error) {
	var result Item
	err := tx.conn.QueryRowContext(tx.ctx, "SELECT id, repository_id, request_json, created_at FROM work_items WHERE id=?", id).Scan(&result.ID, &result.RepositoryID, &result.Request, &result.CreatedAt)
	return result, err
}

func (tx *Tx) InsertItem(item Item) error {
	_, err := tx.conn.ExecContext(tx.ctx, "INSERT INTO work_items (id, repository_id, request_json, created_at) VALUES (?, ?, ?, ?)", item.ID, item.RepositoryID, item.Request, item.CreatedAt)
	return err
}

type Execution struct {
	ID                string          `json:"id"`
	PreviousID        *string         `json:"previous_id"`
	Version           int64           `json:"version"`
	CreatedAt         string          `json:"created_at"`
	UpdatedAt         string          `json:"updated_at"`
	State             json.RawMessage `json:"state"`
	ReadySince        *int64          `json:"ready_since_ms"`
	RetryAt           *int64          `json:"retry_at_ms"`
	RetryCount        int64           `json:"retry_count"`
	UnmetDependencies json.RawMessage `json:"unmet_dependencies"`
}

func (tx *Tx) CurrentExecution(id string) (Execution, error) {
	row := tx.conn.QueryRowContext(tx.ctx, "SELECT "+executionColumns+` FROM executions AS current WHERE item_id=?
		AND NOT EXISTS (SELECT 1 FROM executions WHERE previous_id=current.id)`, id)
	return scanExecution(row)
}

func (tx *Tx) InsertExecution(id, itemID, state, now, repository string, priority int, readySince int64, previousID *string) error {
	_, err := tx.conn.ExecContext(tx.ctx, `INSERT INTO executions
		(id, item_id, previous_id, version, state_json, created_at, updated_at, queue_repository, queue_priority, ready_since)
		VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?)`, id, itemID, previousID, state, now, now, repository, priority, readySince)
	if err != nil {
		return err
	}
	event := "intake"
	if previousID != nil {
		event = "rerun"
	}
	_, err = tx.conn.ExecContext(tx.ctx, "INSERT INTO transition_history VALUES (?, 1, NULL, ?, '{}', ?, ?)", id, event, state, now)
	return err
}

type Link struct {
	UpstreamID string `json:"upstream_id"`
	Milestone  string `json:"milestone"`
}

func (tx *Tx) Links(itemID string) ([]Link, error) {
	rows, err := tx.conn.QueryContext(tx.ctx, "SELECT upstream_id, milestone FROM work_dependencies WHERE item_id=? ORDER BY upstream_id", itemID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := []Link{}
	for rows.Next() {
		var link Link
		if err := rows.Scan(&link.UpstreamID, &link.Milestone); err != nil {
			return nil, err
		}
		result = append(result, link)
	}
	return result, rows.Err()
}

func (tx *Tx) InsertLink(itemID string, link Link) error {
	_, err := tx.conn.ExecContext(tx.ctx, "INSERT INTO work_dependencies (item_id, upstream_id, milestone) VALUES (?, ?, ?)", itemID, link.UpstreamID, link.Milestone)
	return err
}

func (tx *Tx) Cycle(upstream, itemID string) (bool, error) {
	var found int
	err := tx.conn.QueryRowContext(tx.ctx, `WITH RECURSIVE reachable(id) AS (
		VALUES (?) UNION SELECT d.upstream_id FROM work_dependencies AS d JOIN reachable ON d.item_id=reachable.id
	) SELECT 1 FROM reachable WHERE id=? LIMIT 1`, upstream, itemID).Scan(&found)
	if err == sql.ErrNoRows {
		return false, nil
	}
	return err == nil, err
}

type Reply struct {
	Version    int64  `json:"version"`
	CreatedAt  string `json:"created_at"`
	Answer     string `json:"answer"`
	AnsweredBy string `json:"answered_by"`
}

func (tx *Tx) LatestReply(executionID string) (*Reply, error) {
	result := Reply{AnsweredBy: "owner"}
	var details string
	err := tx.conn.QueryRowContext(tx.ctx, `SELECT version, created_at, details_json FROM transition_history
		WHERE execution_id=? AND event='reply' ORDER BY version DESC LIMIT 1`, executionID).Scan(&result.Version, &result.CreatedAt, &details)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var decoded struct {
		Answer     string  `json:"answer"`
		AnsweredBy *string `json:"answered_by"`
	}
	if err := json.Unmarshal([]byte(details), &decoded); err != nil {
		return nil, err
	}
	result.Answer = decoded.Answer
	if decoded.AnsweredBy != nil {
		result.AnsweredBy = *decoded.AnsweredBy
	}
	return &result, nil
}

type Writer struct {
	ID          string
	ItemID      string
	ExecutionID string
}

func (tx *Tx) Writer(tokenDigest string) (Writer, error) {
	var writer Writer
	err := tx.conn.QueryRowContext(tx.ctx, `SELECT writer.id, writer.item_id, writer.execution_id
		FROM writers AS writer JOIN executions AS execution ON execution.id=writer.execution_id
		WHERE writer.token_digest=? AND writer.ended_at IS NULL
		AND execution.status NOT IN `+terminalStatusesSQL, tokenDigest).Scan(&writer.ID, &writer.ItemID, &writer.ExecutionID)
	return writer, err
}

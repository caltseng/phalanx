package sqlite

import "database/sql"

type ScoutConfiguration struct {
	ID, Revision, CadenceMinutes  int64
	Enabled                       bool
	AuthorLogin, DueAt, ChangedBy *string
	UpdatedAt                     *string
	AuthorID                      *int64
	RepositoriesJSON              string
}

func (tx *Tx) ScoutConfiguration() (ScoutConfiguration, error) {
	var result ScoutConfiguration
	err := tx.conn.QueryRowContext(tx.ctx, `SELECT id,revision,enabled,cadence_minutes,author_login,author_id,
		repositories_json,due_at,changed_by,updated_at FROM issue_scout_settings WHERE id=1`).
		Scan(&result.ID, &result.Revision, &result.Enabled, &result.CadenceMinutes, &result.AuthorLogin,
			&result.AuthorID, &result.RepositoriesJSON, &result.DueAt, &result.ChangedBy, &result.UpdatedAt)
	return result, err
}

type ScoutPass struct {
	ID              string  `json:"id"`
	Revision        int64   `json:"revision"`
	State           string  `json:"state"`
	RepositoryIndex int64   `json:"repository_index"`
	Page            int64   `json:"page"`
	StartedAt       string  `json:"started_at"`
	CompletedAt     *string `json:"completed_at"`
	ClaimID         *string `json:"claim_id"`
	CaptainID       *string `json:"captain_id"`
	LeaseUntil      *string `json:"lease_until"`
	RetryAt         *string `json:"retry_at"`
	FailureKind     *string `json:"failure_kind"`
	FailureStatus   *int64  `json:"failure_status"`
	Failures        int64   `json:"failures"`
	CountsJSON      string  `json:"counts_json"`
}

const scoutPassColumns = `id,revision,state,repository_index,page,started_at,completed_at,claim_id,
	captain_id,lease_until,retry_at,failure_kind,failure_status,failures,counts_json`

func scanScoutPass(row scanner) (*ScoutPass, error) {
	var result ScoutPass
	err := row.Scan(&result.ID, &result.Revision, &result.State, &result.RepositoryIndex, &result.Page,
		&result.StartedAt, &result.CompletedAt, &result.ClaimID, &result.CaptainID, &result.LeaseUntil,
		&result.RetryAt, &result.FailureKind, &result.FailureStatus, &result.Failures, &result.CountsJSON)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	return &result, err
}

func (tx *Tx) LatestScoutPass() (*ScoutPass, error) {
	return scanScoutPass(tx.conn.QueryRowContext(tx.ctx,
		"SELECT "+scoutPassColumns+" FROM issue_scout_passes ORDER BY rowid DESC LIMIT 1"))
}

func (tx *Tx) OpenScoutPass() (*ScoutPass, error) {
	return scanScoutPass(tx.conn.QueryRowContext(tx.ctx,
		"SELECT "+scoutPassColumns+" FROM issue_scout_passes WHERE completed_at IS NULL"))
}

func (tx *Tx) ScoutPass(id string) (*ScoutPass, error) {
	return scanScoutPass(tx.conn.QueryRowContext(tx.ctx,
		"SELECT "+scoutPassColumns+" FROM issue_scout_passes WHERE id=?", id))
}

func (tx *Tx) ConfigureScout(enabled bool, cadence int64, author string, authorID int64, repositories, now string) error {
	_, err := tx.conn.ExecContext(tx.ctx, `UPDATE issue_scout_settings SET enabled=?,cadence_minutes=?,author_login=?,
		author_id=?,repositories_json=?,revision=revision+1,due_at=?,changed_by='owner',updated_at=? WHERE id=1`,
		enabled, cadence, author, authorID, repositories, now, now)
	if err != nil {
		return err
	}
	_, err = tx.conn.ExecContext(tx.ctx, `UPDATE issue_scout_passes SET state='superseded',completed_at=?,
		claim_id=NULL,lease_until=NULL WHERE completed_at IS NULL`, now)
	return err
}

func (tx *Tx) ScoutSourceCounts(repositories string) (map[string]int64, error) {
	rows, err := tx.conn.QueryContext(tx.ctx, `SELECT state,count(*) FROM issue_sources
		WHERE repository_id IN (SELECT value FROM json_each(?)) GROUP BY state`, repositories)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := map[string]int64{}
	for rows.Next() {
		var state string
		var count int64
		if err := rows.Scan(&state, &count); err != nil {
			return nil, err
		}
		result[state] = count
	}
	return result, rows.Err()
}

func (tx *Tx) InsertScoutPass(id string, revision int64, now string) error {
	_, err := tx.conn.ExecContext(tx.ctx, `INSERT INTO issue_scout_passes
		(id,revision,state,repository_index,page,started_at) VALUES (?,?,'running',0,1,?)`, id, revision, now)
	return err
}

func (tx *Tx) ExpiredScoutPage(id string) error {
	_, err := tx.conn.ExecContext(tx.ctx,
		"UPDATE issue_scout_passes SET failure_kind='api',failures=failures+1 WHERE id=?", id)
	return err
}

func (tx *Tx) ClaimScoutPage(id, claimID, captainID, lease string) error {
	_, err := tx.conn.ExecContext(tx.ctx, `UPDATE issue_scout_passes SET claim_id=?,captain_id=?,lease_until=?,
		state='running',retry_at=NULL WHERE id=?`, claimID, captainID, lease, id)
	return err
}

type ScoutSource struct {
	RepositoryID  string  `json:"repository_id"`
	Number        int64   `json:"number"`
	URL           string  `json:"url"`
	AuthorLogin   *string `json:"author_login"`
	AuthorID      *int64  `json:"author_id"`
	Fingerprint   string  `json:"fingerprint"`
	IssueState    string  `json:"issue_state"`
	State         string  `json:"state"`
	Reason        string  `json:"reason"`
	ItemID        *string `json:"item_id"`
	Decision      *string `json:"decision"`
	DecidedBy     *string `json:"decided_by"`
	DecidedAt     *string `json:"decided_at"`
	ObservedBy    string  `json:"observed_by"`
	ObservedAt    string  `json:"observed_at"`
	ResumeTrigger string  `json:"resume_trigger"`
}

func scanScoutSource(row scanner) (*ScoutSource, error) {
	var result ScoutSource
	err := row.Scan(&result.RepositoryID, &result.Number, &result.URL, &result.AuthorLogin, &result.AuthorID,
		&result.Fingerprint, &result.IssueState, &result.State, &result.Reason, &result.ItemID, &result.Decision,
		&result.DecidedBy, &result.DecidedAt, &result.ObservedBy, &result.ObservedAt, &result.ResumeTrigger)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	return &result, err
}

func (tx *Tx) ScoutSource(repositoryID string, number int64) (*ScoutSource, error) {
	return scanScoutSource(tx.conn.QueryRowContext(tx.ctx,
		"SELECT * FROM issue_sources WHERE repository_id=? AND number=?", repositoryID, number))
}

func (tx *Tx) ScoutIntakeItem(key, repositoryID string) (*string, error) {
	var id string
	err := tx.conn.QueryRowContext(tx.ctx, `SELECT json_extract(receipt.response_json,'$.id')
		FROM request_receipts AS receipt JOIN work_items AS item ON item.id=json_extract(receipt.response_json,'$.id')
		WHERE receipt.scope='intake' AND receipt.idempotency_key=? AND item.repository_id=?`, key, repositoryID).Scan(&id)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	return &id, err
}

type HistoricalIssueItem struct{ ID, Request string }

func (tx *Tx) HistoricalIssueItems(repositoryID, url string) ([]HistoricalIssueItem, error) {
	rows, err := tx.conn.QueryContext(tx.ctx, `SELECT id,request_json FROM work_items
		WHERE repository_id=? AND instr(lower(request_json),?)>0 ORDER BY created_at,id`, repositoryID, url)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := []HistoricalIssueItem{}
	for rows.Next() {
		var row HistoricalIssueItem
		if err := rows.Scan(&row.ID, &row.Request); err != nil {
			return nil, err
		}
		result = append(result, row)
	}
	return result, rows.Err()
}

func (tx *Tx) RecordScoutSource(source ScoutSource) error {
	_, err := tx.conn.ExecContext(tx.ctx, `INSERT INTO issue_sources VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
		ON CONFLICT(repository_id,number) DO UPDATE SET author_login=excluded.author_login,author_id=excluded.author_id,
		fingerprint=excluded.fingerprint,issue_state=excluded.issue_state,state=excluded.state,reason=excluded.reason,
		item_id=excluded.item_id,decision=excluded.decision,decided_by=excluded.decided_by,decided_at=excluded.decided_at,
		observed_by=excluded.observed_by,observed_at=excluded.observed_at,resume_trigger=excluded.resume_trigger`,
		source.RepositoryID, source.Number, source.URL, source.AuthorLogin, source.AuthorID, source.Fingerprint,
		source.IssueState, source.State, source.Reason, source.ItemID, source.Decision, source.DecidedBy,
		source.DecidedAt, source.ObservedBy, source.ObservedAt, source.ResumeTrigger)
	return err
}

func (tx *Tx) CompleteScoutPage(id string, repositoryIndex, page int64, counts, state string, completedAt *string) error {
	_, err := tx.conn.ExecContext(tx.ctx, `UPDATE issue_scout_passes SET repository_index=?,page=?,counts_json=?,
		claim_id=NULL,lease_until=NULL,failure_kind=NULL,failure_status=NULL,state=?,completed_at=? WHERE id=?`,
		repositoryIndex, page, counts, state, completedAt, id)
	return err
}

func (tx *Tx) ScoutApprovalDuringPass(startedAt string) (bool, error) {
	var result bool
	err := tx.conn.QueryRowContext(tx.ctx, `SELECT EXISTS(SELECT 1 FROM issue_scout_decisions
		WHERE decision='approved' AND decided_at>=?)`, startedAt).Scan(&result)
	return result, err
}

func (tx *Tx) ScoutDueAt(due string) error {
	_, err := tx.conn.ExecContext(tx.ctx, "UPDATE issue_scout_settings SET due_at=? WHERE id=1", due)
	return err
}

func (tx *Tx) ScoutFailure(id, kind string, status *int64, retry string) error {
	_, err := tx.conn.ExecContext(tx.ctx, `UPDATE issue_scout_passes SET state='failed',failure_kind=?,failure_status=?,
		failures=failures+1,retry_at=?,claim_id=NULL,lease_until=NULL WHERE id=?`, kind, status, retry, id)
	return err
}

func (tx *Tx) ScoutDecision(repositoryID string, number int64, fingerprint, decision, now string) error {
	_, err := tx.conn.ExecContext(tx.ctx, "INSERT INTO issue_scout_decisions VALUES (?,?,?,?,'owner',?)",
		repositoryID, number, fingerprint, decision, now)
	if err != nil {
		return err
	}
	state := "skipped"
	if decision == "approved" {
		state = "approved"
	}
	_, err = tx.conn.ExecContext(tx.ctx, `UPDATE issue_sources SET decision=?,decided_by='owner',decided_at=?,
		state=?,reason=?,resume_trigger='Next Captain wake re-fetches the issue before intake'
		WHERE repository_id=? AND number=?`, decision, now, state, "Owner "+decision, repositoryID, number)
	return err
}

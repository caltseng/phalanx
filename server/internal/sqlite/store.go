// Package sqlite owns physical connections, SQL, admission, and commit outcomes.
package sqlite

import (
	"context"
	"database/sql"
	"database/sql/driver"
	"errors"
	"fmt"
	"net/url"
	"os"
	"path/filepath"

	"modernc.org/sqlite"
)

const ApplicationID = 0x50484c58
const SchemaVersion = 26
const BusyTimeoutMS = 2000

type AdmissionError struct{ Read bool }

func (e *AdmissionError) Error() string {
	if e.Read {
		return "SQLite store busy waiting for read admission"
	}
	return "SQLite store busy waiting for write admission"
}

type UnknownCommit struct{ Cause error }

func (e *UnknownCommit) Error() string { return "SQLite commit outcome unknown: " + e.Cause.Error() }
func (e *UnknownCommit) Unwrap() error { return e.Cause }

func IsBusy(err error) bool {
	var admission *AdmissionError
	if errors.As(err, &admission) {
		return true
	}
	var driver *sqlite.Error
	return errors.As(err, &driver) && (driver.Code()&0xff == 5 || driver.Code()&0xff == 6)
}

func BusyReason(err error) string {
	var admission *AdmissionError
	if errors.As(err, &admission) {
		return admission.Error()
	}
	var driver *sqlite.Error
	if errors.As(err, &driver) {
		switch driver.Code() & 0xff {
		case 5:
			return "database is locked"
		case 6:
			return "database table is locked"
		}
	}
	return err.Error()
}

type Store struct {
	path  string
	gate  *gate
	read  *sql.DB
	write *sql.DB
	// Commit is a fault seam for disposable tests, never a transport option.
	commit func(context.Context, *sql.Conn) error
}

type Tx struct {
	conn *sql.Conn
	ctx  context.Context
}

func Open(path string) (*Store, error) {
	resolved, err := filepath.Abs(path)
	if err != nil {
		return nil, err
	}
	resolved, err = filepath.EvalSymlinks(resolved)
	if err != nil {
		return nil, err
	}
	info, err := os.Stat(resolved)
	if err != nil {
		return nil, err
	}
	if !info.Mode().IsRegular() {
		return nil, fmt.Errorf("Phalanx database must be an existing regular file")
	}
	store := &Store{path: resolved, gate: newGate()}
	store.read, err = pool(resolved, "ro", 8)
	if err != nil {
		return nil, err
	}
	store.write, err = pool(resolved, "rw", 1)
	if err != nil {
		return nil, errors.Join(err, store.read.Close())
	}
	err = store.Read(context.Background(), func(tx *Tx) error {
		var appID, version int
		var journal string
		if err := tx.conn.QueryRowContext(tx.ctx, "PRAGMA application_id").Scan(&appID); err != nil {
			return err
		}
		if err := tx.conn.QueryRowContext(tx.ctx, "PRAGMA user_version").Scan(&version); err != nil {
			return err
		}
		if appID != ApplicationID || version != SchemaVersion {
			return errors.New("Unsupported Phalanx database identity or schema version; existing stores require explicit offline conversion")
		}
		if err := tx.conn.QueryRowContext(tx.ctx, "PRAGMA journal_mode").Scan(&journal); err != nil {
			return err
		}
		if journal != "delete" {
			return errors.New("Phalanx requires SQLite DELETE journal mode")
		}
		return nil
	})
	if err != nil {
		return nil, errors.Join(err, store.Close())
	}
	return store, nil
}

func pool(path, mode string, idle int) (*sql.DB, error) {
	path = filepath.ToSlash(path)
	if len(path) > 1 && path[1] == ':' {
		path = "/" + path
	}
	query := url.Values{"mode": {mode}, "_pragma": {
		fmt.Sprintf("busy_timeout(%d)", BusyTimeoutMS), "foreign_keys(ON)", "synchronous(EXTRA)",
	}}
	uri := (&url.URL{Scheme: "file", Path: path, RawQuery: query.Encode()}).String()
	db, err := sql.Open("sqlite", uri)
	if err != nil {
		return nil, err
	}
	// Bound retained handles, not active reads: another admission queue here
	// would change cancellation/busy behavior and could stall the health probe.
	db.SetMaxIdleConns(idle)
	return db, nil
}

func (s *Store) Close() error {
	return errors.Join(s.read.Close(), s.write.Close())
}

func (s *Store) connection(ctx context.Context, mode string) (*sql.Conn, func() error, error) {
	db := s.read
	if mode == "rw" {
		db = s.write
	}
	conn, err := db.Conn(ctx)
	if err != nil {
		return nil, nil, err
	}
	return conn, func() error {
		err := conn.Close()
		if errors.Is(err, sql.ErrConnDone) {
			return nil
		}
		return err
	}, nil
}

func rollback(conn *sql.Conn) error {
	_, err := conn.ExecContext(context.Background(), "ROLLBACK")
	if err != nil {
		// Never return an uncertain transaction to the idle pool.
		conn.Raw(func(any) error { return driver.ErrBadConn })
		return fmt.Errorf("SQLite rollback failed; discarded connection: %w", err)
	}
	return nil
}

func (s *Store) Write(ctx context.Context, perform func(*Tx) error) (result error) {
	release, err := s.gate.write(ctx)
	if err != nil {
		return err
	}
	defer release()
	conn, close, err := s.connection(ctx, "rw")
	if err != nil {
		return err
	}
	defer func() {
		if err := close(); err != nil {
			result = errors.Join(result, err)
		}
	}()
	if _, err = conn.ExecContext(ctx, "BEGIN IMMEDIATE"); err != nil {
		return err
	}
	committed := false
	defer func() {
		if !committed {
			if err := rollback(conn); err != nil {
				result = errors.Join(result, err)
			}
		}
	}()
	if err := perform(&Tx{conn: conn, ctx: ctx}); err != nil {
		return err
	}
	if s.commit != nil {
		err = s.commit(ctx, conn)
	} else {
		_, err = conn.ExecContext(ctx, "COMMIT")
	}
	if err != nil {
		// BUSY/LOCKED COMMIT leaves the transaction active and is rolled back.
		// Other failures require receipt/state reconciliation, never a retry
		// labeled as definitely unaccepted.
		if IsBusy(err) {
			return err
		}
		return &UnknownCommit{Cause: err}
	}
	committed = true
	return nil
}

func (s *Store) Read(ctx context.Context, perform func(*Tx) error) (result error) {
	release, err := s.gate.readStart(ctx)
	if err != nil {
		return err
	}
	conn, close, err := s.connection(ctx, "ro")
	if err != nil {
		release()
		return err
	}
	defer func() {
		if err := close(); err != nil {
			result = errors.Join(result, err)
		}
	}()
	if _, err = conn.ExecContext(ctx, "BEGIN"); err != nil {
		release()
		return err
	}
	committed := false
	defer func() {
		if !committed {
			if err := rollback(conn); err != nil {
				result = errors.Join(result, err)
			}
		}
	}()
	var unused int
	err = conn.QueryRowContext(ctx, "SELECT 1 FROM sqlite_schema LIMIT 1").Scan(&unused)
	if err == sql.ErrNoRows {
		err = nil
	}
	release()
	if err != nil {
		return err
	}
	if err := perform(&Tx{conn: conn, ctx: ctx}); err != nil {
		return err
	}
	_, err = conn.ExecContext(ctx, "COMMIT")
	committed = err == nil
	return err
}

// Health deliberately mirrors Python's un-gated read-only PRAGMA probe.
func (s *Store) Health(ctx context.Context) (result error) {
	conn, close, err := s.connection(ctx, "ro")
	if err != nil {
		return err
	}
	defer func() {
		if err := close(); err != nil {
			result = errors.Join(result, err)
		}
	}()
	var version int
	return conn.QueryRowContext(ctx, "PRAGMA user_version").Scan(&version)
}

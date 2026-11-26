// Package sqlite owns physical connections, SQL, admission, and commit outcomes.
package sqlite

import (
	"context"
	"database/sql"
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
	path string
	gate *gate
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
		return nil, err
	}
	return store, nil
}

func (s *Store) connection(ctx context.Context, mode string) (*sql.Conn, func(), error) {
	path := filepath.ToSlash(s.path)
	if len(path) > 1 && path[1] == ':' {
		path = "/" + path
	}
	uri := (&url.URL{Scheme: "file", Path: path, RawQuery: "mode=" + mode}).String()
	db, err := sql.Open("sqlite", uri)
	if err != nil {
		return nil, nil, err
	}
	db.SetMaxIdleConns(0)
	conn, err := db.Conn(ctx)
	if err != nil {
		db.Close()
		return nil, nil, err
	}
	close := func() { conn.Close(); db.Close() }
	for _, pragma := range []string{
		"PRAGMA busy_timeout = 2000",
		"PRAGMA foreign_keys = ON",
		"PRAGMA synchronous = EXTRA",
	} {
		if _, err := conn.ExecContext(ctx, pragma); err != nil {
			close()
			return nil, nil, err
		}
	}
	return conn, close, nil
}

func (s *Store) Write(ctx context.Context, perform func(*Tx) error) error {
	release, err := s.gate.write(ctx)
	if err != nil {
		return err
	}
	defer release()
	conn, close, err := s.connection(ctx, "rw")
	if err != nil {
		return err
	}
	defer close()
	if _, err = conn.ExecContext(ctx, "BEGIN IMMEDIATE"); err != nil {
		return err
	}
	// Rollback uses its own context: an interrupted operation still cleans up
	// before admission is released. Closing this short-lived connection is final.
	defer conn.ExecContext(context.Background(), "ROLLBACK")
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
	return nil
}

func (s *Store) Read(ctx context.Context, perform func(*Tx) error) error {
	release, err := s.gate.readStart(ctx)
	if err != nil {
		return err
	}
	conn, close, err := s.connection(ctx, "ro")
	if err != nil {
		release()
		return err
	}
	defer close()
	if _, err = conn.ExecContext(ctx, "BEGIN"); err == nil {
		var unused int
		err = conn.QueryRowContext(ctx, "SELECT 1 FROM sqlite_schema LIMIT 1").Scan(&unused)
		if err == sql.ErrNoRows {
			err = nil
		}
	}
	release()
	if err != nil {
		return err
	}
	defer conn.ExecContext(context.Background(), "ROLLBACK")
	if err := perform(&Tx{conn: conn, ctx: ctx}); err != nil {
		return err
	}
	_, err = conn.ExecContext(ctx, "COMMIT")
	return err
}

// Health deliberately mirrors Python's un-gated read-only PRAGMA probe.
func (s *Store) Health(ctx context.Context) error {
	conn, close, err := s.connection(ctx, "ro")
	if err != nil {
		return err
	}
	defer close()
	var version int
	return conn.QueryRowContext(ctx, "PRAGMA user_version").Scan(&version)
}

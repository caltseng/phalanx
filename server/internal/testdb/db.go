// Package testdb owns disposable Go test databases, not live-store initialization.
package testdb

import (
	"database/sql"
	_ "embed"
	"path/filepath"
	"testing"

	_ "modernc.org/sqlite"
)

//go:embed schema.sql
var schema string

func New(t testing.TB) (string, *sql.DB) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "fixture.sqlite3")
	db := Open(t, path)
	tx, err := db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	if _, err := tx.Exec(schema); err != nil {
		t.Fatal(err)
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}
	return path, db
}

func Open(t testing.TB, path string) *sql.DB {
	t.Helper()
	db, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := db.Close(); err != nil {
			t.Error(err)
		}
	})
	return db
}

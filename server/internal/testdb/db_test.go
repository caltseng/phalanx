package testdb

import (
	"bytes"
	"crypto/sha256"
	"database/sql"
	"encoding/json"
	"fmt"
	"testing"
)

func TestGoldenSchema(t *testing.T) {
	_, db := New(t)
	rows, err := db.Query("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name")
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	ddl := make([][4]*string, 0)
	for rows.Next() {
		var row [4]*string
		if err := rows.Scan(&row[0], &row[1], &row[2], &row[3]); err != nil {
			t.Fatal(err)
		}
		ddl = append(ddl, row)
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	var encoded bytes.Buffer
	encoder := json.NewEncoder(&encoded)
	encoder.SetEscapeHTML(false)
	if err := encoder.Encode(ddl); err != nil {
		t.Fatal(err)
	}
	// SHA-256 of compact JSON schema.ddl in the current golden-fresh corpus,
	// including implicit indexes, SQL whitespace and NULL SQL values.
	const goldenDDL = "c8dd5872e18dad37d74f800028922a439814365bcc2fee6d4f3f8fa5c5f4e288"
	if got := fmt.Sprintf("%x", sha256.Sum256(bytes.TrimSuffix(encoded.Bytes(), []byte("\n")))); got != goldenDDL {
		t.Fatalf("embedded DDL digest %s, want golden schema %s", got, goldenDDL)
	}
	for pragma, want := range map[string]any{
		"application_id": int64(1346915416),
		"user_version":   int64(26),
		"journal_mode":   "delete",
	} {
		var got any
		if err := db.QueryRow("PRAGMA " + pragma).Scan(&got); err != nil {
			t.Fatal(err)
		}
		if got != want {
			t.Fatalf("%s = %v, want %v", pragma, got, want)
		}
	}
}

func TestNewIsEmptyAndIsolated(t *testing.T) {
	path, first := New(t)
	otherPath, second := New(t)
	if path == otherPath {
		t.Fatal("fixtures share a database path")
	}
	if _, err := first.Exec("INSERT INTO repositories VALUES ('fixture','https://github.com/example/fixture.git','stamp')"); err != nil {
		t.Fatal(err)
	}
	reopened := Open(t, path)
	for connection, want := range map[*sql.DB]int{reopened: 1, second: 0} {
		var count int
		if err := connection.QueryRow("SELECT count(*) FROM repositories").Scan(&count); err != nil {
			t.Fatal(err)
		}
		if count != want {
			t.Fatalf("fixture has %d repositories, want %d", count, want)
		}
	}
}

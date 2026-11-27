package sqlite

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"github.com/caltseng/phalanx/server/internal/testdb"
	driver "modernc.org/sqlite"
)

func fixture(t *testing.T) *Store {
	t.Helper()
	path, db := testdb.New(t)
	if _, err := db.Exec(`CREATE TABLE parent(id INTEGER PRIMARY KEY);
		CREATE TABLE child(id INTEGER REFERENCES parent(id));`); err != nil {
		t.Fatal(err)
	}
	if err := db.Close(); err != nil {
		t.Fatal(err)
	}
	store, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := store.Close(); err != nil {
			t.Error(err)
		}
	})
	return store
}

func TestIdentityMissingAndJournalRefusal(t *testing.T) {
	path := filepath.Join(t.TempDir(), "absent.sqlite3")
	if _, err := Open(path); err == nil {
		t.Fatal("opened missing database")
	}
	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Fatal("missing database was created")
	}
	store := fixture(t)
	if err := store.Close(); err != nil {
		t.Fatal(err)
	}
	for _, sqlText := range []string{"PRAGMA user_version=25", "PRAGMA application_id=0", "PRAGMA journal_mode=WAL"} {
		db := testdb.Open(t, store.path)
		if _, err := db.Exec(sqlText); err != nil {
			t.Fatal(err)
		}
		db.Close()
		if _, err := Open(store.path); err == nil {
			t.Fatal("accepted unsupported database", sqlText)
		}
		db = testdb.Open(t, store.path)
		_, err := db.Exec("PRAGMA application_id=1346915416; PRAGMA user_version=26; PRAGMA journal_mode=DELETE")
		db.Close()
		if err != nil {
			t.Fatal(err)
		}
	}
}

func TestEveryConnectionPragmasForeignKeysAndRollback(t *testing.T) {
	store := fixture(t)
	for _, write := range []bool{false, true, false, true} {
		perform := func(tx *Tx) error {
			var version string
			if err := tx.conn.QueryRowContext(tx.ctx, "SELECT sqlite_version()").Scan(&version); err != nil {
				return err
			}
			t.Log("SQLite engine", version)
			for name, expected := range map[string]int{"foreign_keys": 1, "synchronous": 3, "busy_timeout": BusyTimeoutMS} {
				var actual int
				if err := tx.conn.QueryRowContext(tx.ctx, "PRAGMA "+name).Scan(&actual); err != nil || actual != expected {
					t.Fatalf("%s=%d err=%v", name, actual, err)
				}
			}
			return nil
		}
		var err error
		if write {
			err = store.Write(context.Background(), perform)
		} else {
			err = store.Read(context.Background(), perform)
		}
		if err != nil {
			t.Fatal(err)
		}
	}
	if err := store.Write(context.Background(), func(tx *Tx) error {
		_, err := tx.conn.ExecContext(tx.ctx, "INSERT INTO child VALUES (99)")
		return err
	}); err == nil || IsBusy(err) {
		t.Fatal("FK failure not permanent", err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	if err := store.Write(ctx, func(tx *Tx) error {
		if _, err := tx.conn.ExecContext(ctx, "INSERT INTO parent VALUES (1)"); err != nil {
			return err
		}
		cancel()
		return ctx.Err()
	}); !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
	if err := store.Read(context.Background(), func(tx *Tx) error {
		var count int
		if err := tx.conn.QueryRowContext(tx.ctx, "SELECT count(*) FROM parent").Scan(&count); err != nil {
			return err
		}
		if count != 0 {
			t.Fatal("interrupted transaction leaked rows")
		}
		return nil
	}); err != nil {
		t.Fatal(err)
	}
}

func TestPoolsReuseConnectionsWithoutCrossingReadWriteModes(t *testing.T) {
	store := fixture(t)
	for _, mode := range []string{"ro", "rw"} {
		var previous any
		for i := 0; i < 3; i++ {
			conn, close, err := store.connection(context.Background(), mode)
			if err != nil {
				t.Fatal(err)
			}
			err = conn.Raw(func(raw any) error {
				if previous != nil && previous != raw {
					t.Error("physical connection was not reused", mode)
				}
				previous = raw
				return nil
			})
			if err != nil {
				t.Fatal(err)
			}
			_, err = conn.ExecContext(context.Background(), "INSERT INTO parent DEFAULT VALUES")
			if (err == nil) != (mode == "rw") {
				t.Fatal("connection mode changed", mode, err)
			}
			if err := close(); err != nil {
				t.Fatal(err)
			}
		}
	}
}

func TestConcurrentReadPoolInitializesAndBoundsIdleConnections(t *testing.T) {
	store := fixture(t)
	var releases []func() error
	defer func() {
		for _, release := range releases {
			if err := release(); err != nil {
				t.Error(err)
			}
		}
	}()
	for i := 0; i < 12; i++ {
		conn, close, err := store.connection(context.Background(), "ro")
		if err != nil {
			t.Fatal(err)
		}
		releases = append(releases, close)
		for name, expected := range map[string]int{"foreign_keys": 1, "synchronous": 3, "busy_timeout": BusyTimeoutMS} {
			var actual int
			if err := conn.QueryRowContext(context.Background(), "PRAGMA "+name).Scan(&actual); err != nil || actual != expected {
				t.Fatalf("new connection %d: %s=%d err=%v", i, name, actual, err)
			}
		}
	}
	for _, release := range releases {
		if err := release(); err != nil {
			t.Fatal(err)
		}
	}
	releases = nil
	if stats := store.read.Stats(); stats.InUse != 0 || stats.Idle != 8 || stats.MaxIdleClosed != 4 {
		t.Fatal("read burst retained unbounded idle connections", stats)
	}
	if err := store.Close(); err != nil {
		t.Fatal(err)
	}
	if stats := store.read.Stats(); stats.OpenConnections != 0 {
		t.Fatal("shutdown retained pooled connections", stats)
	}
	if err := os.Rename(store.path, store.path+".closed"); err != nil {
		t.Fatal("closed store retained file handles", err)
	}
}

func TestPooledTransactionsCleanUpFailuresAndPanics(t *testing.T) {
	for _, write := range []bool{false, true} {
		for _, failure := range []string{"error", "cancel", "panic", "rollback"} {
			t.Run(fmt.Sprintf("write=%t/%s", write, failure), func(t *testing.T) {
				store := fixture(t)
				run := store.Read
				pool := store.read
				if write {
					run, pool = store.Write, store.write
				}
				ctx, cancel := context.WithCancel(context.Background())
				defer cancel()
				sentinel := errors.New("callback failed")
				var result error
				var panicked any
				func() {
					defer func() { panicked = recover() }()
					result = run(ctx, func(tx *Tx) error {
						if write {
							if _, err := tx.conn.ExecContext(ctx, "INSERT INTO parent VALUES (1)"); err != nil {
								return err
							}
						}
						switch failure {
						case "cancel":
							cancel()
							return ctx.Err()
						case "panic":
							panic(sentinel)
						case "rollback":
							// Simulate cleanup failing after the transaction ended.
							if _, err := tx.conn.ExecContext(ctx, "ROLLBACK"); err != nil {
								return err
							}
						}
						return sentinel
					})
				}()
				if failure == "panic" {
					if panicked != sentinel {
						t.Fatal("panic was not preserved", panicked)
					}
				} else if failure == "cancel" {
					if !errors.Is(result, context.Canceled) {
						t.Fatal(result)
					}
				} else if failure == "error" && result != sentinel {
					t.Fatal("callback error identity changed", result)
				} else if !errors.Is(result, sentinel) {
					t.Fatal("callback error was lost", result)
				}
				if stats := pool.Stats(); failure == "rollback" && stats.OpenConnections != 0 {
					t.Fatal("failed rollback returned a connection to the pool", stats)
				}
				if err := run(context.Background(), func(tx *Tx) error {
					var count int
					err := tx.conn.QueryRowContext(tx.ctx, "SELECT count(*) FROM parent").Scan(&count)
					if count != 0 {
						t.Fatal("failed transaction leaked data", count)
					}
					return err
				}); err != nil {
					t.Fatal("pool did not recover", err)
				}
			})
		}
	}
}

func TestBusyBeginBoundAndCommitRollback(t *testing.T) {
	store := fixture(t)
	blocker, close, err := store.connection(context.Background(), "rw")
	if err != nil {
		t.Fatal(err)
	}
	defer close()
	if _, err := blocker.ExecContext(context.Background(), "BEGIN EXCLUSIVE"); err != nil {
		t.Fatal(err)
	}
	start := time.Now()
	err = store.Write(context.Background(), func(*Tx) error { t.Fatal("entered blocked BEGIN"); return nil })
	if !IsBusy(err) || time.Since(start) > 3*time.Second {
		t.Fatal("wrong busy classification/bound", err, time.Since(start))
	}
	if _, err := blocker.ExecContext(context.Background(), "ROLLBACK; BEGIN; SELECT * FROM parent"); err != nil {
		t.Fatal(err)
	}
	err = store.Write(context.Background(), func(tx *Tx) error {
		_, err := tx.conn.ExecContext(tx.ctx, "INSERT INTO parent VALUES (1)")
		return err
	})
	if !IsBusy(err) {
		t.Fatal("reader should block COMMIT", err)
	}
	if _, err := blocker.ExecContext(context.Background(), "ROLLBACK"); err != nil {
		t.Fatal(err)
	}
	if err := store.Write(context.Background(), func(tx *Tx) error {
		var count int
		err := tx.conn.QueryRowContext(tx.ctx, "SELECT count(*) FROM parent").Scan(&count)
		if count != 0 {
			t.Fatal("failed commit retained write")
		}
		return err
	}); err != nil {
		t.Fatal(err)
	}
}

func TestUnknownCommitReconcilesReceiptWithoutReplay(t *testing.T) {
	store := fixture(t)
	store.commit = func(ctx context.Context, conn *sql.Conn) error {
		if _, err := conn.ExecContext(ctx, "COMMIT"); err != nil {
			return err
		}
		return errors.New("injected lost commit acknowledgement")
	}
	err := store.Write(context.Background(), func(tx *Tx) error {
		return tx.SaveReceipt("intake", "key", "digest", `{"accepted":true}`)
	})
	var unknown *UnknownCommit
	if !errors.As(err, &unknown) || IsBusy(err) {
		t.Fatal("commit uncertainty mislabeled", err)
	}
	store.commit = nil
	if err := store.Read(context.Background(), func(tx *Tx) error {
		receipt, err := tx.Receipt("intake", "key")
		if err == nil && receipt.Response != `{"accepted":true}` {
			t.Fatal("lost committed receipt")
		}
		return err
	}); err != nil {
		t.Fatal(err)
	}
}

func TestDriverOnlineBackupPreservesIdentityTypedRowsAndDDL(t *testing.T) {
	store := fixture(t)
	if err := store.Write(context.Background(), func(tx *Tx) error {
		_, err := tx.conn.ExecContext(tx.ctx, `CREATE TABLE retained(value BLOB, number INTEGER, text TEXT);
			CREATE INDEX retained_index ON retained(number);
			INSERT INTO retained VALUES (x'00ff', 9007199254740993, '雪');`)
		return err
	}); err != nil {
		t.Fatal(err)
	}
	destination := filepath.Join(t.TempDir(), "backup.sqlite3")
	conn, close, err := store.connection(context.Background(), "ro")
	if err != nil {
		t.Fatal(err)
	}
	defer close()
	err = conn.Raw(func(raw any) error {
		creator, ok := raw.(interface {
			NewBackup(string) (*driver.Backup, error)
		})
		if !ok {
			return errors.New("driver lacks online backup API")
		}
		backup, err := creator.NewBackup(destination)
		if err != nil {
			return err
		}
		more, err := backup.Step(-1)
		finish := backup.Finish()
		if err == nil && more {
			err = errors.New("whole-file backup remained incomplete")
		}
		return errors.Join(err, finish)
	})
	if err != nil {
		t.Fatal(err)
	}
	copy, err := Open(destination)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := copy.Close(); err != nil {
			t.Error(err)
		}
	})
	if err := copy.Read(context.Background(), func(tx *Tx) error {
		var blob []byte
		var number int64
		var text, integrity, ddl string
		if err := tx.conn.QueryRowContext(tx.ctx, "SELECT value, number, text FROM retained").Scan(&blob, &number, &text); err != nil {
			return err
		}
		if string(blob) != "\x00\xff" || number != 9007199254740993 || text != "雪" {
			t.Fatal("backup changed typed values", blob, number, text)
		}
		if err := tx.conn.QueryRowContext(tx.ctx, "PRAGMA integrity_check").Scan(&integrity); err != nil {
			return err
		}
		if integrity != "ok" {
			t.Fatal(integrity)
		}
		if err := tx.conn.QueryRowContext(tx.ctx, "SELECT sql FROM sqlite_schema WHERE name='retained_index'").Scan(&ddl); err != nil {
			return err
		}
		if ddl != "CREATE INDEX retained_index ON retained(number)" {
			t.Fatal("backup changed DDL", ddl)
		}
		return nil
	}); err != nil {
		t.Fatal(err)
	}
}

func TestExistingUnicodeURIPathKeepsTheSameDatabase(t *testing.T) {
	store := fixture(t)
	if err := store.Close(); err != nil {
		t.Fatal(err)
	}
	directory := filepath.Join(t.TempDir(), "雪 #%&")
	if err := os.Mkdir(directory, 0700); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(directory, "fixture.sqlite3")
	if err := os.Rename(store.path, path); err != nil {
		t.Fatal(err)
	}
	moved, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := moved.Close(); err != nil {
			t.Error(err)
		}
	})
	if err := moved.Write(context.Background(), func(tx *Tx) error {
		_, err := tx.conn.ExecContext(tx.ctx, "INSERT INTO parent VALUES (7)")
		return err
	}); err != nil {
		t.Fatal(err)
	}
	if err := moved.Read(context.Background(), func(tx *Tx) error {
		var value int
		err := tx.conn.QueryRowContext(tx.ctx, "SELECT id FROM parent").Scan(&value)
		if value != 7 {
			t.Fatal("URI did not address the original store")
		}
		return err
	}); err != nil {
		t.Fatal(err)
	}
	entries, err := os.ReadDir(directory)
	if err != nil || len(entries) != 1 || entries[0].Name() != "fixture.sqlite3" {
		t.Fatal("URI created a second database", entries, err)
	}
}

func gateCounts(g *gate) (int, int, int, bool) {
	g.mu.Lock()
	defer g.mu.Unlock()
	return g.writers, g.admitted, g.queued, g.writing
}

func await(t *testing.T, condition func() bool) {
	t.Helper()
	until := time.Now().Add(2 * time.Second)
	for !condition() {
		if time.Now().After(until) {
			t.Fatal("barrier deadline exceeded")
		}
		time.Sleep(time.Millisecond)
	}
}

func TestGateReaderBatchPrecedesNextWriter(t *testing.T) {
	g := newGate()
	release, err := g.write(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	readAdmitted := make(chan struct{})
	readDone := make(chan struct{})
	go func() {
		exit, err := g.readStart(context.Background())
		if err != nil {
			t.Error(err)
			return
		}
		close(readAdmitted)
		<-readDone
		exit()
	}()
	writerAdmitted := make(chan struct{})
	go func() {
		exit, err := g.write(context.Background())
		if err != nil {
			t.Error(err)
			return
		}
		close(writerAdmitted)
		exit()
	}()
	await(t, func() bool { writers, _, queued, _ := gateCounts(g); return writers == 1 && queued == 1 })
	release()
	<-readAdmitted
	select {
	case <-writerAdmitted:
		t.Fatal("writer passed admitted reader")
	default:
	}
	close(readDone)
	<-writerAdmitted
	await(t, func() bool { w, a, q, busy := gateCounts(g); return w+a+q == 0 && !busy })
}

func TestGateInterruptionBeforeAndAfterReaderTransfer(t *testing.T) {
	for _, transferred := range []bool{false, true} {
		g := newGate()
		release, err := g.write(context.Background())
		if err != nil {
			t.Fatal(err)
		}
		ctx, cancel := context.WithCancel(context.Background())
		done := make(chan error)
		go func() {
			exit, err := g.readStart(ctx)
			if err == nil {
				exit()
			}
			done <- err
		}()
		await(t, func() bool { _, _, q, _ := gateCounts(g); return q == 1 })
		if transferred {
			// Transfer and cancellation under the same condition lock ensures
			// the waiter observes the interruption only after batch transfer.
			g.mu.Lock()
			g.writing = false
			g.writes++
			g.admitted += g.queued
			g.queued = 0
			cancel()
			g.notify()
			g.mu.Unlock()
		} else {
			cancel()
		}
		if err := <-done; !errors.Is(err, context.Canceled) {
			t.Fatal("interrupted reader admitted", err)
		}
		if !transferred {
			release()
		}
		w, a, q, busy := gateCounts(g)
		if w+a+q != 0 || busy {
			t.Fatal("phantom gate counters", w, a, q, busy)
		}
	}
}

func TestGateTimeoutAndContendedRecovery(t *testing.T) {
	g := newGate()
	g.timeout = 40 * time.Millisecond
	release, err := g.write(context.Background())
	if err != nil {
		t.Fatal(err)
	}

	var workers sync.WaitGroup
	for i := 0; i < 12; i++ {
		workers.Add(1)
		go func(read bool) {
			defer workers.Done()
			var err error
			if read {
				_, err = g.readStart(context.Background())
			} else {
				_, err = g.write(context.Background())
			}
			if !IsBusy(err) {
				t.Errorf("timeout not classified busy: %v", err)
			}
		}(i%2 == 0)
	}
	workers.Wait()
	release()
	w, a, q, busy := gateCounts(g)
	if w+a+q != 0 || busy {
		t.Fatal("waiters not cleaned", w, a, q, busy)
	}
	exit, err := g.write(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	exit()
}

func TestDefaultAdmissionBoundCleansBothWaiterKinds(t *testing.T) {
	g := newGate()
	release, err := g.write(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	defer release()
	start := time.Now()
	done := make(chan error, 2)
	go func() { _, err := g.write(context.Background()); done <- err }()
	go func() { _, err := g.readStart(context.Background()); done <- err }()
	for i := 0; i < 2; i++ {
		if err := <-done; !IsBusy(err) {
			t.Fatal("admission did not refuse bounded wait", err)
		}
	}
	elapsed := time.Since(start)
	if elapsed < 3900*time.Millisecond || elapsed > 5*time.Second {
		t.Fatal("four-second admission bound outside one-second host tolerance", elapsed)
	}
	w, a, q, _ := gateCounts(g)
	if w+a+q != 0 {
		t.Fatal("timed-out waiters retained admission", w, a, q)
	}
}

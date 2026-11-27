package sqlite

import (
	"context"
	"testing"

	"github.com/caltseng/phalanx/server/internal/testdb"
)

func BenchmarkStore(b *testing.B) {
	path, db := testdb.New(b)
	if err := db.Close(); err != nil {
		b.Fatal(err)
	}
	store, err := Open(path)
	if err != nil {
		b.Fatal(err)
	}
	b.Cleanup(func() {
		if err := store.Close(); err != nil {
			b.Error(err)
		}
	})
	ctx := context.Background()
	read := func(tx *Tx) error {
		var count int
		return tx.conn.QueryRowContext(tx.ctx, "SELECT count(*) FROM repositories").Scan(&count)
	}
	for _, workload := range []struct {
		name string
		run  func() error
	}{
		{"Health", func() error { return store.Health(ctx) }},
		{"Read", func() error { return store.Read(ctx, read) }},
		{"Write", func() error {
			return store.Write(ctx, func(tx *Tx) error {
				_, err := tx.conn.ExecContext(tx.ctx, "UPDATE dispatch_settings SET paused = paused")
				return err
			})
		}},
	} {
		b.Run(workload.name, func(b *testing.B) {
			b.ReportAllocs()
			for b.Loop() {
				if err := workload.run(); err != nil {
					b.Fatal(err)
				}
			}
		})
	}
	b.Run("ParallelRead", func(b *testing.B) {
		b.ReportAllocs()
		b.RunParallel(func(pb *testing.PB) {
			for pb.Next() {
				if err := store.Read(ctx, read); err != nil {
					b.Error(err)
					return
				}
			}
		})
	})
}

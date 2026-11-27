// conformance-server is a disposable test adapter, not a deployment entrypoint.
package main

import (
	"bufio"
	"context"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"sync/atomic"
	"time"

	"github.com/caltseng/phalanx/server/internal/application"
	"github.com/caltseng/phalanx/server/internal/httpapi"
	"github.com/caltseng/phalanx/server/internal/instancelock"
	"github.com/caltseng/phalanx/server/internal/sqlite"
)

func main() {
	if err := run(); err != nil {
		slog.Error("Disposable conformance server failed", "error", err)
		os.Exit(1)
	}
}

func run() (result error) {
	database := flag.String("fixture", "", "existing disposable fixture database")
	idStart := flag.Uint64("id-start", 0, "deterministic fixture ID starting ordinal")
	clockOffset := flag.Duration("clock-offset", 0, "elapsed time since the deterministic fixture clock")
	localBoard := flag.Bool("local-board", false, "enable direct loopback-only disposable board authority")
	fixtureAssets := flag.Bool("fixture-assets", false, "serve pinned assets copied into this disposable fixture")
	flag.Parse()
	if *database == "" {
		return fmt.Errorf("--fixture is required; no environment/config discovery")
	}
	path, err := filepath.Abs(*database)
	if err != nil {
		return err
	}
	marker, err := os.ReadFile(filepath.Join(filepath.Dir(path), "DISPOSABLE"))
	if err != nil || string(marker) != "phalanx-go-conformance-v1\n" || filepath.Base(path) != "fixture.sqlite3" {
		return fmt.Errorf("requires the harness's disposable fixture and marker")
	}
	lock, err := instancelock.Acquire(path)
	if err != nil {
		return err
	}
	defer lock.Close()
	store, err := sqlite.Open(path)
	if err != nil {
		return err
	}
	defer func() { result = errors.Join(result, store.Close()) }()
	app := application.New(store)
	// Both implementations get the same deterministic test clock and IDs.
	app.Clock = func() time.Time { return time.Date(2026, 1, 2, 3, 4, 5, 123456000, time.UTC).Add(*clockOffset) }
	var counter atomic.Uint64
	counter.Store(*idStart)
	app.ID = func() (string, error) { return fmt.Sprintf("%032x", counter.Add(1)), nil }
	if err := app.StartReconciliation(context.Background()); err != nil {
		return err
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return err
	}
	home := filepath.Dir(path)
	assets := filepath.Join("phalanx", "static")
	if *fixtureAssets {
		assets = filepath.Join(home, "assets")
	}
	server := &http.Server{
		Handler:           &httpapi.Handler{App: app, Owner: "conformance-owner-only-0000000000000000", Runner: "conformance-runner-only-0000000000000000", Logger: slog.Default(), Home: &home, LocalBoard: *localBoard, AssetsDirectory: assets},
		ReadHeaderTimeout: 5 * time.Second,
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
	defer stop()
	jobsContext, stopJobs := context.WithCancel(context.Background())
	jobsDone := make(chan struct{})
	go func() {
		defer close(jobsDone)
		app.RunBackground(jobsContext, slog.Default())
	}()
	defer func() {
		stopJobs()
		<-jobsDone
	}()
	done := make(chan error, 1)
	go func() { done <- server.Serve(listener) }()
	inputClosed := make(chan error, 1)
	go func() {
		scanner := bufio.NewScanner(os.Stdin)
		scanner.Scan()
		inputClosed <- scanner.Err()
	}()
	fmt.Println("http://" + listener.Addr().String())
	select {
	case err := <-done:
		return err
	case <-ctx.Done():
		// No claimed deployment drain deadline. This test adapter waits for
		// dispatched operations, including ones whose HTTP caller left.
		return server.Shutdown(context.Background())
	case err := <-inputClosed:
		return errors.Join(err, server.Shutdown(context.Background()))
	}
}

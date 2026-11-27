// phalanx-server is explicitly selected by the existing deployment supervisor.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"regexp"
	"runtime"
	"strconv"
	"syscall"
	"time"

	"github.com/caltseng/phalanx/server/internal/application"
	"github.com/caltseng/phalanx/server/internal/httpapi"
	"github.com/caltseng/phalanx/server/internal/instancelock"
	"github.com/caltseng/phalanx/server/internal/sqlite"
)

var revision = "unversioned"

type configuration struct {
	database, owner, runner, assets, host string
	forwardedAllowIPs                     string
	home                                  *string
	port                                  int
	localBoard                            bool
}

func configure(host string, port int, assets string, lookup func(string) (string, bool)) (configuration, error) {
	getenv := func(key string) string { value, _ := lookup(key); return value }
	config := configuration{host: host, port: port, assets: assets,
		database: getenv("PHALANX_DB"), owner: getenv("PHALANX_OWNER_TOKEN"), runner: getenv("PHALANX_RUNNER_TOKEN"),
		forwardedAllowIPs: "127.0.0.1"}
	if value, present := lookup("FORWARDED_ALLOW_IPS"); present {
		config.forwardedAllowIPs = value
	}
	if host != "127.0.0.1" && host != "::1" && host != "localhost" {
		return config, errors.New("Server host must be loopback; retain the existing TLS proxy")
	}
	if port < 1 || port > 65535 {
		return config, errors.New("Server port must be between 1 and 65535")
	}
	if !filepath.IsAbs(config.database) {
		return config, errors.New("Server requires an explicit absolute PHALANX_DB")
	}
	validToken := regexp.MustCompile(`^[A-Za-z0-9_-]{32,}$`)
	for _, role := range []struct{ name, token string }{{"OWNER", config.owner}, {"RUNNER", config.runner}} {
		if !validToken.MatchString(role.token) {
			return config, fmt.Errorf("PHALANX_%s_TOKEN requires at least 32 URL-safe characters", role.name)
		}
	}
	if config.owner == config.runner {
		return config, errors.New("Owner and runner tokens must be distinct")
	}
	switch getenv("PHALANX_LOCAL_BOARD") {
	case "", "0":
	case "1":
		config.localBoard = true
	default:
		return config, errors.New("PHALANX_LOCAL_BOARD must be 1 to open the local board, or 0 or unset")
	}
	var err error
	config.home, err = configuredHome(getenv("PHALANX_HOME"), lookup)
	if err != nil {
		return config, fmt.Errorf("Cannot resolve PHALANX_HOME: %w", err)
	}
	if !filepath.IsAbs(assets) {
		return config, errors.New("Server requires an absolute packaged assets directory")
	}
	for _, name := range []string{"operator-board.html", "operator-board.js", "operator-board.css",
		"operator-helpers.js", "operator-http.js", "operator-markdown.js", "operator-shell.js", "phalanx-brand.svg"} {
		info, err := os.Stat(filepath.Join(assets, name))
		if err != nil || !info.Mode().IsRegular() {
			return config, fmt.Errorf("Missing packaged Scribe asset: %s", name)
		}
	}
	return config, nil
}

func main() {
	host := flag.String("host", "", "existing supervisor's loopback bind host")
	port := flag.Int("port", 0, "existing supervisor's port")
	assets := flag.String("assets", "", "absolute versioned package assets directory")
	version := flag.Bool("version", false, "print credential-free build identity and exit")
	flag.Parse()
	if flag.NArg() != 0 {
		slog.Error("Unexpected server arguments")
		os.Exit(1)
	}
	if *version {
		if err := json.NewEncoder(os.Stdout).Encode(map[string]any{
			"revision": revision, "go": runtime.Version(), "os": runtime.GOOS, "arch": runtime.GOARCH,
			"schema": sqlite.SchemaVersion, "application_id": sqlite.ApplicationID,
		}); err != nil {
			slog.Error("Cannot write build identity", "error", err)
			os.Exit(1)
		}
		return
	}
	config, err := configure(*host, *port, *assets, os.LookupEnv)
	if err == nil {
		err = foregroundPriority()
	}
	if err == nil {
		ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
		defer stop()
		err = serve(ctx, config)
	}
	if err != nil {
		slog.Error("Phalanx server failed", "error", err)
		os.Exit(1)
	}
}

func serve(ctx context.Context, config configuration) (result error) {
	lock, err := instancelock.Acquire(config.database)
	if err != nil {
		return fmt.Errorf("Cannot acquire sole server ownership: %w", err)
	}
	defer func() { result = errors.Join(result, lock.Close()) }()
	store, err := sqlite.Open(config.database)
	if err != nil {
		return err
	}
	app := application.New(store)
	if err := app.StartReconciliation(ctx); err != nil {
		return err
	}
	listener, err := net.Listen("tcp", net.JoinHostPort(config.host, strconv.Itoa(config.port)))
	if err != nil {
		return err
	}
	server := &http.Server{
		Handler: httpapi.ProxyHeaders(&httpapi.Handler{App: app, Owner: config.owner, Runner: config.runner,
			Logger: slog.Default(), Home: config.home, LocalBoard: config.localBoard, AssetsDirectory: config.assets},
			config.forwardedAllowIPs),
		IdleTimeout: 5 * time.Second,
		ConnState:   idleState,
		// h11 bounds incomplete events, not the number of fields in a complete one.
		MaxHeaderValueCount: int(^uint(0) >> 1),
	}
	jobsContext, stopJobs := context.WithCancel(context.Background())
	jobsDone := make(chan struct{})
	go func() {
		defer close(jobsDone)
		app.RunBackground(jobsContext, slog.Default())
	}()
	defer func() {
		result = errors.Join(result, server.Shutdown(context.Background()))
		stopJobs()
		<-jobsDone
	}()
	done := make(chan error, 1)
	go func() { done <- server.Serve(idleListener{listener}) }()
	slog.Info("Phalanx Go server listening", "revision", revision, "address", listener.Addr().String())
	select {
	case err := <-done:
		return err
	case <-ctx.Done():
		// Keep the OS lock until dispatched HTTP work and durable batches drain.
		// No new operation/drain deadline or success for unknown commits is added.
		err := server.Shutdown(context.Background())
		return errors.Join(err, ignoreClosed(<-done))
	}
}

func ignoreClosed(err error) error {
	if errors.Is(err, http.ErrServerClosed) {
		return nil
	}
	return err
}

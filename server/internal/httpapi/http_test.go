package httpapi

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/caltseng/phalanx/server/internal/application"
	"github.com/caltseng/phalanx/server/internal/contract"
	"github.com/caltseng/phalanx/server/internal/sqlite"
	"github.com/caltseng/phalanx/server/internal/testdb"
)

func fixtureApp(t *testing.T) *application.App {
	app, _ := fixtureAppDatabase(t)
	return app
}

func fixtureAppDatabase(t *testing.T) (*application.App, string) {
	t.Helper()
	path, db := testdb.New(t)
	if err := db.Close(); err != nil {
		t.Fatal(err)
	}
	store, err := sqlite.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	app := application.New(store)
	if _, err := app.Register(context.Background(), contract.Registration{ID: "fixture", Remote: "https://github.com/example/fixture.git"}); err != nil {
		t.Fatal(err)
	}
	return app, path
}

func deadline(t *testing.T, ch <-chan struct{}) {
	t.Helper()
	select {
	case <-ch:
	case <-time.After(5 * time.Second):
		t.Fatal("barrier deadline exceeded")
	}
}

func TestDisconnectWhileDispatchedWaitingForAdmissionDoesNotCancelMutation(t *testing.T) {
	app := fixtureApp(t)
	idCalls := 0
	app.ID = func() (string, error) {
		idCalls++
		return fmt.Sprintf("%032x", idCalls), nil
	}
	held := make(chan struct{})
	release := make(chan struct{})
	holderDone := make(chan error, 1)
	go func() {
		holderDone <- app.Store.Write(context.Background(), func(*sqlite.Tx) error {
			close(held)
			<-release
			return nil
		})
	}()
	deadline(t, held)
	dispatched := make(chan struct{})
	finished := make(chan struct{})
	handler := &Handler{App: app, Owner: "synthetic-owner", Runner: "synthetic-runner",
		dispatch: func() { close(dispatched) }}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		handler.ServeHTTP(w, r)
		close(finished)
	}))
	defer server.Close()
	ctx, cancel := context.WithCancel(context.Background())
	request, err := http.NewRequestWithContext(ctx, "POST", server.URL+"/v1/items", strings.NewReader(`{"repository_id":"fixture","goal":"Disconnect"}`))
	if err != nil {
		t.Fatal(err)
	}
	request.Header.Set("Authorization", "Bearer synthetic-owner")
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Idempotency-Key", "lost-reply")
	clientDone := make(chan error, 1)
	go func() {
		response, err := server.Client().Do(request)
		if response != nil {
			io.Copy(io.Discard, response.Body)
			response.Body.Close()
		}
		clientDone <- err
	}()
	deadline(t, dispatched)
	cancel()
	if err := <-clientDone; !errors.Is(err, context.Canceled) {
		t.Fatal("caller did not disconnect", err)
	}
	close(release)
	if err := <-holderDone; err != nil {
		t.Fatal(err)
	}
	deadline(t, finished)
	key := "lost-reply"
	body := contract.Intake{RepositoryID: "fixture", Goal: "Disconnect", Target: "checked_in", ContextLinks: []string{}, AcceptanceCriteria: []string{}, Dependencies: []contract.Prerequisite{}, Priority: "normal", PublicationMode: "ready"}
	replayed, err := app.Intake(context.Background(), body, &key)
	if err != nil {
		t.Fatal(err)
	}
	if err := app.Store.Read(context.Background(), func(tx *sqlite.Tx) error {
		receipt, err := tx.Receipt("intake", key)
		if err == nil && string(replayed) != receipt.Response {
			t.Fatal("retry did not preserve receipt")
		}
		return err
	}); err != nil || idCalls != 2 {
		t.Fatal("committed receipt not reconcilable", err)
	}
}

func TestExplicitInterruptionWaitingForAdmissionDoesNotCommit(t *testing.T) {
	app := fixtureApp(t)
	held := make(chan struct{})
	release := make(chan struct{})
	done := make(chan error, 1)
	go func() {
		done <- app.Store.Write(context.Background(), func(*sqlite.Tx) error {
			close(held)
			<-release
			return nil
		})
	}()
	deadline(t, held)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	request := contract.Intake{RepositoryID: "fixture", Goal: "Interrupted", Target: "checked_in", Priority: "normal", PublicationMode: "ready"}
	key := "interrupted"
	if _, err := app.Intake(ctx, request, &key); !errors.Is(err, context.Canceled) {
		t.Fatal("explicit interruption was detached", err)
	}
	close(release)
	if err := <-done; err != nil {
		t.Fatal(err)
	}
	if err := app.Store.Read(context.Background(), func(tx *sqlite.Tx) error {
		_, err := tx.Receipt("intake", key)
		if err != sql.ErrNoRows {
			t.Fatal("interrupted operation wrote a receipt", err)
		}
		return nil
	}); err != nil {
		t.Fatal(err)
	}
}

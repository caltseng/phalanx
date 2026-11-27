package httpapi

import (
	"bytes"
	"context"
	"database/sql"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/caltseng/phalanx/server/internal/application"
	"github.com/caltseng/phalanx/server/internal/contract"
	"github.com/caltseng/phalanx/server/internal/sqlite"
	"github.com/caltseng/phalanx/server/internal/testdb"
)

func TestApplicationResponsePreservesWireBytes(t *testing.T) {
	app := fixtureApp(t)
	request, issues := contract.ValidateIntake(map[string]any{
		"repository_id": "fixture", "goal": "<>&\u00e9\U0001f600\u2028\n",
	}, []string{"repository_id", "goal"})
	if len(issues) != 0 {
		t.Fatal(issues)
	}
	value, err := app.Intake(context.Background(), request, nil)
	if err != nil {
		t.Fatal(err)
	}
	canonical, err := contract.Canonical(value)
	if err != nil {
		t.Fatal(err)
	}
	previous, _, err := contract.Decode([]byte(canonical))
	if err != nil {
		t.Fatal(err)
	}
	expected := httptest.NewRecorder()
	writeJSON(expected, http.StatusOK, previous)
	body, ok := contract.ObjectValues(value)
	if !ok {
		t.Fatal("intake returned an encoded response instead of a value")
	}
	r := httptest.NewRequest("GET", "/v1/items/"+body["id"].(string), nil)
	r.Header.Set("Authorization", "Bearer owner")
	actual := httptest.NewRecorder()
	(&Handler{App: app, Owner: "owner", Runner: "runner"}).ServeHTTP(actual, r)
	if actual.Code != expected.Code || !bytes.Equal(actual.Body.Bytes(), expected.Body.Bytes()) {
		t.Fatalf("wire response changed:\nold %d %s\nnew %d %s",
			expected.Code, expected.Body.Bytes(), actual.Code, actual.Body.Bytes())
	}
}

func fixtureApp(t *testing.T) *application.App {
	app, _ := fixtureAppDatabase(t)
	return app
}

func responseJSON(t *testing.T, value any) []byte {
	t.Helper()
	text, err := contract.Canonical(value)
	if err != nil {
		t.Fatal(err)
	}
	return []byte(text)
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
		if err == nil && string(responseJSON(t, replayed)) != receipt.Response {
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

func TestAdmissionRefusalsKeepWorkerIndependentResponsesAndRecover(t *testing.T) {
	app := fixtureApp(t)
	held, release := make(chan struct{}), make(chan struct{})
	holderDone := make(chan error, 1)
	go func() {
		holderDone <- app.Store.Write(context.Background(), func(*sqlite.Tx) error {
			close(held)
			<-release
			return nil
		})
	}()
	deadline(t, held)
	unblock := sync.OnceFunc(func() {
		close(release)
		if err := <-holderDone; err != nil {
			t.Error(err)
		}
	})
	defer unblock()
	handler := &Handler{App: app, Owner: "owner", Runner: "runner"}
	server := httptest.NewServer(handler)
	defer server.Close()
	post := func() (*http.Response, error) {
		request, err := http.NewRequest("POST", server.URL+"/v1/items",
			strings.NewReader(`{"repository_id":"fixture","goal":"Admission refusal recovery"}`))
		if err != nil {
			return nil, err
		}
		request.Header.Set("Authorization", "Bearer owner")
		request.Header.Set("Content-Type", "application/json")
		request.Header.Set("Idempotency-Key", "admission-refusal")
		return server.Client().Do(request)
	}
	type reply struct {
		status int
		retry  string
		body   string
		err    error
	}
	replies := make(chan reply, 20)
	for range 20 {
		go func() {
			response, err := post()
			if err != nil {
				replies <- reply{err: err}
				return
			}
			body, err := io.ReadAll(response.Body)
			response.Body.Close()
			replies <- reply{response.StatusCode, response.Header.Get("Retry-After"), string(body), err}
		}()
	}
	for _, path := range []string{"/health", "/v1/owner/status"} {
		request := httptest.NewRequest("GET", path, nil)
		request.Header.Set("Authorization", "Bearer owner")
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, request)
		if response.Code != http.StatusOK {
			t.Fatal("storage admission blocked an independent response", response.Code, response.Body)
		}
	}
	for range 20 {
		select {
		case result := <-replies:
			if result.err != nil || result.status != http.StatusServiceUnavailable || result.retry != "1" ||
				!strings.Contains(result.body, "retry the same request") {
				t.Fatal("lost worker-independent admission refusal", result)
			}
		case <-time.After(15 * time.Second):
			t.Fatal("admission refusal did not complete")
		}
	}
	// Release the actual native gate; the same process/key must recover without prior effects.
	unblock()
	if err := app.Store.Read(context.Background(), func(tx *sqlite.Tx) error {
		_, err := tx.Receipt("intake", "admission-refusal")
		if err != sql.ErrNoRows {
			return fmt.Errorf("refusal wrote an immutable receipt: %v", err)
		}
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	var accepted []byte
	for range 2 {
		response, err := post()
		if err != nil {
			t.Fatal(err)
		}
		body, err := io.ReadAll(response.Body)
		response.Body.Close()
		if err != nil || response.StatusCode != http.StatusCreated {
			t.Fatal("same-process recovery failed", response.StatusCode, string(body), err)
		}
		if accepted != nil && !bytes.Equal(accepted, body) {
			t.Fatal("recovered-key replay changed the original response")
		}
		accepted = body
	}
}

// Package application owns intake/receipt orchestration and domain checks.
// HTTP, process launch, and driver error classification do not belong here.
package application

import (
	"context"
	"crypto/rand"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"time"

	"github.com/caltseng/phalanx/server/internal/contract"
	"github.com/caltseng/phalanx/server/internal/sqlite"
)

type Missing struct{ Message string }

func (e *Missing) Error() string { return e.Message }

type Conflict struct{ Message string }

func (e *Conflict) Error() string { return e.Message }

type Forbidden struct{ Message string }

func (e *Forbidden) Error() string { return e.Message }

type App struct {
	Store       *sqlite.Store
	Clock       func() time.Time
	ID          func() (string, error)
	afterIntent func() // test-only barrier after the intent commits, never wire/config input
}

func New(store *sqlite.Store) *App {
	return &App{Store: store, Clock: time.Now, ID: randomID}
}

func randomID() (string, error) {
	var value [16]byte
	if _, err := rand.Read(value[:]); err != nil {
		return "", err
	}
	value[6] = value[6]&0x0f | 0x40
	value[8] = value[8]&0x3f | 0x80
	return hex.EncodeToString(value[:]), nil
}

func timestamp(now time.Time) string {
	return now.UTC().Format("2006-01-02T15:04:05.000000+00:00")
}

func encoded(value any) (json.RawMessage, error) {
	text, err := contract.Canonical(value)
	return json.RawMessage(text), err
}

func (a *App) Register(ctx context.Context, request contract.Registration) (json.RawMessage, error) {
	var result json.RawMessage
	err := a.Store.Write(ctx, func(tx *sqlite.Tx) error {
		if err := tx.Register(request.ID, request.Remote, timestamp(a.Clock())); err != nil {
			return err
		}
		repository, err := tx.Repository(request.ID)
		if err == sql.ErrNoRows || (err == nil && repository.Remote != request.Remote) {
			return &Conflict{"Repository ID or remote is already registered"}
		}
		if err != nil {
			return err
		}
		result, err = encoded(repository)
		return err
	})
	return result, err
}

func (a *App) Repositories(ctx context.Context) (json.RawMessage, error) {
	var result json.RawMessage
	err := a.Store.Read(ctx, func(tx *sqlite.Tx) error {
		repositories, err := tx.Repositories()
		if err != nil {
			return err
		}
		result, err = encoded(repositories)
		return err
	})
	return result, err
}

func (a *App) Inspect(ctx context.Context, itemID string) (json.RawMessage, error) {
	var result json.RawMessage
	err := a.Store.Read(ctx, func(tx *sqlite.Tx) error {
		var err error
		result, err = inspect(tx, itemID)
		return err
	})
	return result, err
}

func inspect(tx *sqlite.Tx, itemID string) (json.RawMessage, error) {
	item, err := tx.Item(itemID)
	if err == sql.ErrNoRows {
		return nil, &Missing{"Work item not found"}
	}
	if err != nil {
		return nil, err
	}
	execution, err := tx.CurrentExecution(itemID)
	if err == sql.ErrNoRows {
		return nil, &Missing{"Work item not found"}
	}
	if err != nil {
		return nil, err
	}
	value, _, err := contract.Decode([]byte(item.Request))
	if err != nil {
		return nil, err
	}
	body, ok := contract.ObjectValues(value)
	if !ok {
		return nil, errors.New("Stored item request is not an object")
	}
	links, err := tx.Links(itemID)
	if err != nil {
		return nil, err
	}
	reply, err := tx.LatestReply(execution.ID)
	if err != nil {
		return nil, err
	}
	body["id"] = item.ID
	body["created_at"] = item.CreatedAt
	body["dependencies"] = links
	body["execution"] = execution
	body["last_reply"] = reply
	return encoded(body)
}

func (a *App) Intake(ctx context.Context, request contract.Intake, key *string) (json.RawMessage, error) {
	var result json.RawMessage
	err := a.Store.Write(ctx, func(tx *sqlite.Tx) error {
		var err error
		result, err = a.intake(tx, request, key)
		return err
	})
	return result, err
}

func (a *App) intake(tx *sqlite.Tx, request contract.Intake, key *string) (json.RawMessage, error) {
	material, err := request.Material()
	if err != nil {
		return nil, err
	}
	current, err := contract.Canonical(material)
	if err != nil {
		return nil, err
	}
	material["runner_affinity"] = nil
	compatible, err := contract.Canonical(material)
	if err != nil {
		return nil, err
	}
	if key != nil {
		receipt, err := tx.Receipt("intake", *key)
		if err == nil {
			if receipt.Digest != contract.Digest(compatible) && receipt.Digest != contract.Digest(current) {
				return nil, &Conflict{"Idempotency key was already used with a different request"}
			}
			// Do not inspect mutable item state or rewrite the receipt.
			return json.RawMessage(receipt.Response), nil
		}
		if err != sql.ErrNoRows {
			return nil, err
		}
	}
	result, err := a.create(tx, request)
	if err != nil {
		return nil, err
	}
	if key != nil {
		if err := tx.SaveReceipt("intake", *key, contract.Digest(compatible), string(result)); err != nil {
			return nil, err
		}
	}
	return result, nil
}

func (a *App) create(tx *sqlite.Tx, request contract.Intake) (json.RawMessage, error) {
	if _, err := tx.Repository(request.RepositoryID); err != nil {
		if err == sql.ErrNoRows {
			return nil, &Missing{"Repository not registered"}
		}
		return nil, err
	}
	itemID, err := a.ID()
	if err != nil {
		return nil, err
	}
	body, err := request.Material()
	if err != nil {
		return nil, err
	}
	delete(body, "dependencies")
	if request.Label == nil {
		delete(body, "label")
	}
	text, err := contract.Canonical(body)
	if err != nil {
		return nil, err
	}
	if err := tx.InsertItem(sqlite.Item{ID: itemID, RepositoryID: request.RepositoryID, Request: text, CreatedAt: timestamp(a.Clock())}); err != nil {
		return nil, err
	}
	if len(request.Dependencies) > 0 {
		if err := replaceDependencies(tx, itemID, request.Dependencies); err != nil {
			return nil, err
		}
	}
	executionID, err := a.ID()
	if err != nil {
		return nil, err
	}
	state, err := contract.Canonical(Start(Target(request.Target), PublicationMode(request.PublicationMode)))
	if err != nil {
		return nil, err
	}
	now := a.Clock()
	priority := map[string]int{"low": 0, "normal": 1, "urgent": 2}[request.Priority]
	if err := tx.InsertExecution(executionID, itemID, state, timestamp(now), request.RepositoryID, priority, now.UnixMilli(), nil); err != nil {
		return nil, err
	}
	unmet, err := unmetDependencies(tx, itemID)
	if err != nil {
		return nil, err
	}
	if len(unmet) > 0 {
		// Python update_wait uses ordinary insertion-order JSON, not sorted
		// canonical JSON. Preserve these stored bytes too.
		data, err := pythonOrderedJSON(unmet)
		if err != nil {
			return nil, err
		}
		if err := tx.DependencyWait(executionID, &data); err != nil {
			return nil, err
		}
	}
	return inspect(tx, itemID)
}

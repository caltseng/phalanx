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

type App struct {
	Store *sqlite.Store
	Clock func() time.Time
	ID    func() (string, error)
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
	var result json.RawMessage
	err = a.Store.Write(ctx, func(tx *sqlite.Tx) error {
		if key != nil {
			receipt, err := tx.Receipt("intake", *key)
			if err == nil {
				if receipt.Digest != contract.Digest(compatible) && receipt.Digest != contract.Digest(current) {
					return &Conflict{"Idempotency key was already used with a different request"}
				}
				// Do not inspect mutable item state or rewrite the receipt.
				result = json.RawMessage(receipt.Response)
				return nil
			}
			if err != sql.ErrNoRows {
				return err
			}
		}
		var err error
		result, err = a.create(tx, request)
		if err != nil {
			return err
		}
		if key != nil {
			return tx.SaveReceipt("intake", *key, contract.Digest(compatible), string(result))
		}
		return nil
	})
	return result, err
}

type initialState struct {
	Target               string  `json:"target"`
	Stage                string  `json:"stage"`
	Status               string  `json:"status"`
	Candidate            *string `json:"candidate"`
	PrimaryPR            *string `json:"primary_pr"`
	ReportDraft          *string `json:"report_draft"`
	Wait                 any     `json:"wait"`
	Stopping             *string `json:"stopping"`
	PendingOutput        *string `json:"pending_output"`
	ReadyRevision        any     `json:"ready_revision"`
	FinalOutput          any     `json:"final_output"`
	ReportReviewFindings *string `json:"report_review_findings"`
	Findings             *string `json:"findings"`
	PublicationMode      string  `json:"publication_mode"`
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
	seen := map[string]bool{}
	for _, dependency := range request.Dependencies {
		if dependency.UpstreamID == itemID {
			return nil, &Conflict{"A work item cannot depend on itself"}
		}
		if seen[dependency.UpstreamID] {
			return nil, &Conflict{"Duplicate prerequisite " + dependency.UpstreamID}
		}
		seen[dependency.UpstreamID] = true
		upstream, err := tx.CurrentExecution(dependency.UpstreamID)
		if err == sql.ErrNoRows {
			return nil, &Missing{"Work item not found"}
		}
		if err != nil {
			return nil, err
		}
		var state dependencyState
		if err := json.Unmarshal(upstream.State, &state); err != nil {
			return nil, err
		}
		if (state.Target == "report") != (dependency.Milestone == "report") {
			return nil, &Conflict{"Prerequisite " + dependency.UpstreamID + " has an incompatible output type"}
		}
		cycle, err := tx.Cycle(dependency.UpstreamID, itemID)
		if err != nil {
			return nil, err
		}
		if cycle {
			return nil, &Conflict{"Prerequisite " + dependency.UpstreamID + " would create a dependency cycle"}
		}
	}
	for _, dependency := range request.Dependencies {
		if err := tx.InsertLink(itemID, sqlite.Link{UpstreamID: dependency.UpstreamID, Milestone: dependency.Milestone}); err != nil {
			return nil, err
		}
	}
	executionID, err := a.ID()
	if err != nil {
		return nil, err
	}
	stage := "development"
	if request.Target == "report" {
		stage = "report_drafting"
	}
	state, err := contract.Canonical(initialState{Target: request.Target, Stage: stage, Status: "queued", PublicationMode: request.PublicationMode})
	if err != nil {
		return nil, err
	}
	now := a.Clock()
	priority := map[string]int{"low": 0, "normal": 1, "urgent": 2}[request.Priority]
	if err := tx.InsertExecution(executionID, itemID, state, timestamp(now), request.RepositoryID, priority, now.UnixMilli()); err != nil {
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

package application

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"time"

	"github.com/caltseng/phalanx/server/internal/contract"
	"github.com/caltseng/phalanx/server/internal/sqlite"
)

func replaceDependencies(tx *sqlite.Tx, itemID string, dependencies []contract.Prerequisite) error {
	if err := requireItem(tx, itemID); err != nil {
		return err
	}
	seen := map[string]bool{}
	links := []sqlite.Link{}
	for _, dependency := range dependencies {
		if dependency.UpstreamID == itemID {
			return &Conflict{"A work item cannot depend on itself"}
		}
		if seen[dependency.UpstreamID] {
			return &Conflict{"Duplicate prerequisite " + dependency.UpstreamID}
		}
		seen[dependency.UpstreamID] = true
		row, err := tx.CurrentExecution(dependency.UpstreamID)
		if err == sql.ErrNoRows {
			return &Missing{"Work item not found"}
		}
		if err != nil {
			return err
		}
		state, err := readState(row.State)
		if err != nil {
			return err
		}
		if (state.Target == ReportTarget) != (dependency.Milestone == "report") {
			return &Conflict{"Prerequisite " + dependency.UpstreamID + " has an incompatible output type"}
		}
		cycle, err := tx.Cycle(dependency.UpstreamID, itemID)
		if err != nil {
			return err
		}
		if cycle {
			return &Conflict{"Prerequisite " + dependency.UpstreamID + " would create a dependency cycle"}
		}
		links = append(links, sqlite.Link{UpstreamID: dependency.UpstreamID, Milestone: dependency.Milestone})
	}
	return tx.ReplaceLinks(itemID, links)
}

func requestObject(tx *sqlite.Tx, itemID string) (sqlite.Item, map[string]any, error) {
	item, err := tx.Item(itemID)
	if err != nil {
		return item, nil, err
	}
	value, _, err := contract.Decode([]byte(item.Request))
	if err != nil {
		return item, nil, err
	}
	body, ok := contract.ObjectValues(value)
	if !ok {
		return item, nil, errors.New("Stored item request is not an object")
	}
	return item, body, nil
}

func requestPriority(body map[string]any) (int, error) {
	value, exists := body["priority"]
	if !exists {
		value = "normal"
	}
	name, ok := value.(string)
	if !ok {
		return 0, errors.New("Stored priority is not text")
	}
	priority, ok := map[string]int{"low": 0, "normal": 1, "urgent": 2}[name]
	if !ok {
		return 0, errors.New("Unsupported stored priority")
	}
	return priority, nil
}

func (a *App) Steer(ctx context.Context, itemID, executionID string, request contract.Steering, key *string) (json.RawMessage, error) {
	var result json.RawMessage
	err := a.Store.Write(ctx, func(tx *sqlite.Tx) error {
		var err error
		result, err = replay(tx, "steer:"+itemID+":"+executionID, key, request.Payload, func() (json.RawMessage, error) {
			row, state, err := checkCurrent(tx, itemID, executionID, request.ExpectedVersion)
			if err != nil {
				return nil, err
			}
			_, targetEdit := request.Edits["target"]
			targetOnly := !Terminal(state.Status) && len(request.Edits) == 1 && targetEdit
			_, criteriaEdit := request.Edits["acceptance_criteria"]
			deliveryCriteria := false
			if state.Stage == Delivery && state.Status == WaitingForExternal && len(request.Edits) == 1 && criteriaEdit {
				active, err := tx.OpenAttempt(executionID)
				if err != nil {
					return nil, err
				}
				deliveryCriteria = active == nil
			}
			if (state.Status != Queued && !targetOnly && !deliveryCriteria) || state.Stopping != nil || state.PendingOutput != nil {
				return nil, &Conflict{"Steering requires queued work, a target-only change to open PR work, or acceptance-criteria-only changes during an attempt-free delivery wait. Resolve the active execution or rerun it first."}
			}
			if len(request.Edits) == 0 {
				return nil, &Conflict{"Steering requires at least one edit. Use an empty list to clear list fields."}
			}
			for name, value := range request.Edits {
				if name != "label" && value == nil {
					return nil, &Conflict{"Steering requires at least one edit. Use an empty list to clear list fields."}
				}
			}
			stateJSON := string(row.State)
			if targetEdit {
				target, ok := request.Edits["target"].(string)
				if !ok {
					return nil, errors.New("Steering target is not text")
				}
				if state.Target == ReportTarget || Target(target) == ReportTarget {
					return nil, &Conflict{"Steering can change only between ready_for_check_in and checked_in. A report and a PR are different outputs; file a new item instead."}
				}
				state.Target = Target(target)
				stateJSON, err = contract.Canonical(state)
				if err != nil {
					return nil, err
				}
			}
			_, body, err := requestObject(tx, itemID)
			if err != nil {
				return nil, err
			}
			previous := map[string]any{}
			for name, value := range request.Edits {
				if name == "dependencies" {
					before, err := tx.Links(itemID)
					if err != nil {
						return nil, err
					}
					previous[name] = before
					dependencies, ok := value.([]contract.Prerequisite)
					if !ok {
						return nil, errors.New("Steering dependencies are not a list")
					}
					if err := replaceDependencies(tx, itemID, dependencies); err != nil {
						return nil, err
					}
				} else {
					previous[name] = body[name]
					body[name] = value
				}
			}
			bodyJSON, err := contract.Canonical(body)
			if err != nil {
				return nil, err
			}
			priority, err := requestPriority(body)
			if err != nil {
				return nil, err
			}
			now := timestamp(a.Clock())
			if err := tx.SteerItem(itemID, bodyJSON, executionID, row.Version+1, stateJSON, now, priority); err != nil {
				return nil, err
			}
			details, err := contract.Canonical(map[string]any{"before": previous, "after": request.Edits, "steered_by": request.SteeredBy})
			if err != nil {
				return nil, err
			}
			if err := tx.RecordHistory(executionID, row.Version+1, nil, "steer", details, stateJSON, now); err != nil {
				return nil, err
			}
			if err := refreshWait(tx, itemID, executionID, time.Now().UnixMilli()); err != nil {
				return nil, err
			}
			return inspect(tx, itemID)
		})
		return err
	})
	return result, err
}

func (a *App) Rerun(ctx context.Context, itemID, executionID string, request contract.Rerun, key *string) (json.RawMessage, error) {
	var result json.RawMessage
	err := a.Store.Write(ctx, func(tx *sqlite.Tx) error {
		var err error
		result, err = replay(tx, "rerun:"+itemID+":"+executionID, key, request, func() (json.RawMessage, error) {
			_, state, err := checkCurrent(tx, itemID, executionID, request.ExpectedVersion)
			if err != nil {
				return nil, err
			}
			if !Terminal(state.Status) {
				return nil, &Conflict{"Only closed executions can be rerun"}
			}
			if (state.Target == ReportTarget) != (request.Target == "report") {
				return nil, &Conflict{"A rerun must preserve the requested output type"}
			}
			item, body, err := requestObject(tx, itemID)
			if err != nil {
				return nil, err
			}
			mode := ReadyMode
			if value, present := body["publication_mode"]; present {
				name, ok := value.(string)
				if !ok {
					return nil, errors.New("Stored publication mode is not text")
				}
				mode = PublicationMode(name)
			}
			if mode != ReadyMode && mode != DraftMode {
				return nil, errors.New("Unsupported stored publication mode")
			}
			priority, err := requestPriority(body)
			if err != nil {
				return nil, err
			}
			id, err := a.createExecution(tx, itemID, item.RepositoryID, Target(request.Target), mode, priority, &executionID)
			if err != nil {
				return nil, err
			}
			now := a.Clock()
			if err := refreshDependents(tx, itemID, "", now); err != nil {
				return nil, err
			}
			view, err := tx.Execution(itemID, id)
			if err != nil {
				return nil, err
			}
			return encoded(view)
		})
		return err
	})
	return result, err
}

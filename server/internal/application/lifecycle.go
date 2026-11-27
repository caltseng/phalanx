package application

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"strconv"
	"time"

	"github.com/caltseng/phalanx/server/internal/contract"
	"github.com/caltseng/phalanx/server/internal/sqlite"
)

func readState(data json.RawMessage) (State, error) {
	var defaults struct {
		Status          json.RawMessage `json:"status"`
		PublicationMode json.RawMessage `json:"publication_mode"`
	}
	if err := json.Unmarshal(data, &defaults); err != nil {
		return State{}, err
	}
	if bytes.Equal(bytes.TrimSpace(defaults.Status), []byte("null")) ||
		bytes.Equal(bytes.TrimSpace(defaults.PublicationMode), []byte("null")) {
		return State{}, errors.New("Invalid null stored execution default")
	}
	state := State{Status: Queued, PublicationMode: ReadyMode}
	if err := json.Unmarshal(data, &state); err != nil {
		return State{}, err
	}
	if state.Wait != nil {
		if _, err := newWait(state.Wait.Reason, state.Wait.Trigger); err != nil {
			return State{}, err
		}
	}
	switch state.Target {
	case ReportTarget, ReadyTarget, MergedTarget:
	default:
		return State{}, errors.New("Invalid stored execution target")
	}
	switch state.Stage {
	case Development, SelfReview, ReportDrafting, ReportReview, ReportStorage, Publication, Delivery:
	default:
		return State{}, errors.New("Invalid stored execution stage")
	}
	switch state.Status {
	case Queued, Running, WaitingForInput, WaitingForExternal, Completed, Failed, Cancelled:
	default:
		return State{}, errors.New("Invalid stored execution status")
	}
	if state.PublicationMode != ReadyMode && state.PublicationMode != DraftMode {
		return State{}, errors.New("Invalid stored publication mode")
	}
	for _, status := range []*Status{state.Stopping, state.PendingOutput} {
		if status != nil {
			switch *status {
			case Queued, Running, WaitingForInput, WaitingForExternal, Completed, Failed, Cancelled:
			default:
				return State{}, errors.New("Invalid stored execution intent")
			}
		}
	}
	if Terminal(state.Status) != (state.FinalOutput != nil) {
		return State{}, errors.New("Exactly one final output is required on terminal executions")
	}
	return state, nil
}

func checkCurrent(tx *sqlite.Tx, itemID, executionID string, version json.Number) (sqlite.Execution, State, error) {
	row, err := tx.CurrentExecution(itemID)
	if err == sql.ErrNoRows {
		return row, State{}, &Missing{"Work item not found"}
	}
	if err != nil {
		return row, State{}, err
	}
	if row.ID != executionID || strconv.FormatInt(row.Version, 10) != string(version) {
		return row, State{}, &Conflict{"Execution or state version has changed"}
	}
	state, err := readState(row.State)
	return row, state, err
}

func (a *App) apply(tx *sqlite.Tx, itemID string, row sqlite.Execution, state State, event Event,
	details any, options DecisionOptions, attemptID *string, retryAt *int64) (sqlite.Execution, error) {
	next, err := Decide(state, event, options)
	if err != nil {
		return sqlite.Execution{}, err
	}
	if event == Result {
		resultDetails, ok := details.(map[string]any)
		if !ok {
			return sqlite.Execution{}, errors.New("Result history details are not an object")
		}
		if state.Stage == SelfReview && next.Stage == Development && present(next.Findings) {
			question, err := stalledRework(tx, row.ID, next)
			if err != nil {
				return sqlite.Execution{}, err
			}
			if question != nil {
				resultDetails["outcome"], resultDetails["question"] = NeedsInput, question
				prompt, ok := question["prompt"].(string)
				if !ok {
					return sqlite.Execution{}, errors.New("Recovery question has no text prompt")
				}
				next, err = Decide(state, Result, DecisionOptions{Outcome: NeedsInput, Wait: &Wait{prompt, "owner reply"}})
				if err != nil {
					return sqlite.Execution{}, err
				}
			}
		}
		hasQuestion := resultDetails["question"] != nil
		if next.Status == WaitingForInput {
			if !hasQuestion && options.Outcome == NeedsInput {
				return sqlite.Execution{}, &Conflict{"A structured question and handoff are required for an input wait"}
			}
		} else if hasQuestion {
			return sqlite.Execution{}, &Conflict{"This result does not open an input wait"}
		}
	}
	stateJSON, err := contract.Canonical(next)
	if err != nil {
		return sqlite.Execution{}, err
	}
	detailsJSON, err := contract.Canonical(details)
	if err != nil {
		return sqlite.Execution{}, err
	}
	now := a.Clock()
	var readySince *int64
	if next.Status == Queued {
		value := now.UnixMilli()
		if retryAt != nil {
			value = *retryAt
		}
		readySince = &value
	}
	retryCount := row.RetryCount
	if next.Stage != state.Stage {
		retryCount = 0
	}
	if event == Retry || retryAt != nil {
		retryCount++
	}
	if err := tx.UpdateExecution(row.ID, row.Version, stateJSON, timestamp(now), readySince, retryAt, retryCount); err != nil {
		return sqlite.Execution{}, err
	}
	if err := tx.RecordHistory(row.ID, row.Version+1, attemptID, string(event), detailsJSON, stateJSON, timestamp(now)); err != nil {
		return sqlite.Execution{}, err
	}
	if state.PendingOutput == nil && next.PendingOutput != nil {
		if err := tx.EnqueueFinalization(row.ID, row.Version+1); err != nil {
			return sqlite.Execution{}, err
		}
	}
	if attemptID != nil && event != Claim {
		if err := tx.EndAttempt(*attemptID, timestamp(now)); err != nil {
			return sqlite.Execution{}, err
		}
	}
	if next.Status == Queued {
		if err := refreshWait(tx, itemID, row.ID, now.UnixMilli()); err != nil {
			return sqlite.Execution{}, err
		}
	}
	changed, err := evidenceChanged(state, next)
	if err != nil {
		return sqlite.Execution{}, err
	}
	if changed {
		if err := refreshDependents(tx, itemID, "", now); err != nil {
			return sqlite.Execution{}, err
		}
	}
	return tx.Execution(itemID, row.ID)
}

func replay(tx *sqlite.Tx, scope string, key *string, payload any, perform func() (any, error)) (any, error) {
	if key == nil {
		return perform()
	}
	material, err := contract.Canonical(payload)
	if err != nil {
		return nil, err
	}
	receipt, err := tx.Receipt(scope, *key)
	if err == nil {
		if receipt.Digest != contract.Digest(material) {
			return nil, &Conflict{"Idempotency key was already used with a different request"}
		}
		return storedResponse(receipt.Response)
	}
	if err != sql.ErrNoRows {
		return nil, err
	}
	result, err := perform()
	if err != nil {
		return nil, err
	}
	text, err := contract.Canonical(result)
	if err != nil {
		return nil, err
	}
	if err := tx.SaveReceipt(scope, *key, contract.Digest(material), text); err != nil {
		return nil, err
	}
	return result, nil
}

func (a *App) RequestStop(ctx context.Context, itemID, executionID string, request contract.Cancellation,
	event Event, key *string) (any, error) {
	var result any
	err := a.Store.Write(ctx, func(tx *sqlite.Tx) error {
		var err error
		scope := "cancel"
		if event == CloseFailure {
			scope = "close-failure"
		}
		result, err = replay(tx, scope+":"+itemID+":"+executionID, key, request, func() (any, error) {
			row, state, err := checkCurrent(tx, itemID, executionID, request.ExpectedVersion)
			if err != nil {
				return nil, err
			}
			details := map[string]any{"reason": request.Reason}
			updated, err := a.apply(tx, itemID, row, state, event, details, DecisionOptions{}, nil, nil)
			if err != nil {
				return nil, err
			}
			next, err := readState(updated.State)
			if err != nil {
				return nil, err
			}
			if next.Stopping != nil && state.Status != Running {
				outstanding, err := tx.HasOutstandingWork(executionID)
				if err != nil {
					return nil, err
				}
				if !outstanding {
					updated, err = a.apply(tx, itemID, updated, next, StopConfirmed, details, DecisionOptions{}, nil, nil)
					if err != nil {
						return nil, err
					}
				}
			}
			return responseValue(updated)
		})
		return err
	})
	if err != nil {
		return nil, err
	}
	return a.finishResponse(ctx, result)
}

func (a *App) RetryFinalization(ctx context.Context, itemID, executionID string, request contract.Versioned, key *string) (any, error) {
	var result any
	err := a.Store.Write(ctx, func(tx *sqlite.Tx) error {
		var err error
		result, err = replay(tx, "retry-finalization:"+itemID+":"+executionID, key, request, func() (any, error) {
			row, state, err := checkCurrent(tx, itemID, executionID, request.ExpectedVersion)
			if err != nil {
				return nil, err
			}
			retried, err := tx.RetryFinalization(executionID)
			if err != nil {
				return nil, err
			}
			if !retried {
				return nil, &Conflict{"Only stalled finalization can be manually retried"}
			}
			updated, err := a.apply(tx, itemID, row, state, Finalize, map[string]any{"owner_retry": true},
				DecisionOptions{Wait: &Wait{"Store the final Markdown report", "scheduled finalization retry"}}, nil, nil)
			if err != nil {
				return nil, err
			}
			return responseValue(updated)
		})
		return err
	})
	if err != nil {
		return nil, err
	}
	return a.finishResponse(ctx, result)
}

func (a *App) finishResponse(ctx context.Context, result any) (any, error) {
	body, ok := contract.ObjectValues(result)
	if !ok {
		return nil, errors.New("Execution response is not an object")
	}
	id, ok := body["id"].(string)
	if !ok {
		return nil, errors.New("Execution response has no id")
	}
	state, ok := contract.ObjectValues(body["state"])
	if !ok {
		return nil, errors.New("Execution response state is not an object")
	}
	if state["pending_output"] == nil {
		return result, nil
	}
	if a.afterIntent != nil {
		a.afterIntent()
	}
	finished, err := a.FinalizeAttempt(ctx, id)
	if err != nil {
		var unknown *sqlite.UnknownCommit
		if sqlite.IsBusy(err) || sqlite.IsDriverError(err) || errors.As(err, &unknown) {
			slog.Error("Finalization could not access the store; return committed pending intent and reconcile current state",
				"execution_id", id, "error", err)
			// Phase one is already committed. This is its explicit pending
			// snapshot, never a claimed final output or blind mutation replay.
			return result, nil
		}
		return nil, err
	}
	if finished == nil {
		return result, nil
	}
	return responseValue(finished)
}

func (a *App) FinalizeAttempt(ctx context.Context, executionID string) (*sqlite.Execution, error) {
	var result *sqlite.Execution
	err := a.Store.Write(ctx, func(tx *sqlite.Tx) error {
		itemID, err := tx.ExecutionItem(executionID)
		if err == sql.ErrNoRows {
			return nil
		}
		if err != nil {
			return err
		}
		row, err := tx.Execution(itemID, executionID)
		if err != nil {
			return err
		}
		result = &row
		// Python's existing job due times use wall time, independently of
		// lifecycle timestamp/ID injection in disposable adapters.
		now := time.Now().Unix()
		job, err := tx.DueFinalization(executionID, now)
		if err == sql.ErrNoRows {
			return nil
		}
		if err != nil {
			return err
		}
		state, err := readState(row.State)
		if err != nil {
			return err
		}
		err = tx.FinalizationSavepoint(func() error {
			updated, err := a.finish(tx, itemID, row, state, job)
			if err == nil {
				result = &updated
			}
			return err
		})
		if err != nil {
			var failure *sqlite.FinalizationFailure
			if !errors.As(err, &failure) {
				return err
			}
			reason, recognized := sqlite.FailureName(failure.Cause)
			var missing *Missing
			var conflict *Conflict
			if errors.As(failure.Cause, &missing) || errors.As(failure.Cause, &conflict) {
				reason, recognized = failure.Cause.Error(), true
			}
			if !recognized {
				return err
			}
			failures := job.Failures + 1
			var due *int64
			wait := &Wait{
				fmt.Sprintf("Final report storage stalled after %d failures (%s). Check database access and report integrity, then retry finalization.", failures, reason),
				"owner retry finalization",
			}
			if failures <= 4 {
				value := now + int64(1<<failures)
				due = &value
				wait = &Wait{"Final report storage failed (" + reason + "). Retrying automatically.", "scheduled finalization retry"}
			}
			if err := tx.RecordFinalizationFailure(row.ID, failures, due); err != nil {
				return err
			}
			updated, err := a.apply(tx, itemID, row, state, Finalize,
				map[string]any{"failure": reason, "failures": failures, "retry_at_unix": due},
				DecisionOptions{Wait: wait}, nil, nil)
			if err != nil {
				return err
			}
			result = &updated
		}
		return nil
	})
	return result, err
}

func (a *App) ProcessFinalizations(ctx context.Context) error {
	var due []string
	err := a.Store.Read(ctx, func(tx *sqlite.Tx) error {
		var err error
		due, err = tx.DueFinalizations(time.Now().Unix(), 32)
		return err
	})
	if err != nil {
		return err
	}
	var failures []error
	for _, executionID := range due {
		if _, err := a.FinalizeAttempt(ctx, executionID); err != nil {
			failures = append(failures, err)
		}
	}
	return errors.Join(failures...)
}

func (a *App) finish(tx *sqlite.Tx, itemID string, row sqlite.Execution, state State, job sqlite.FinalizationJob) (sqlite.Execution, error) {
	var markdown string
	var revision *string
	if state.PendingOutput == nil {
		return sqlite.Execution{}, &Conflict{"Only finalization may resume this execution"}
	}
	if *state.PendingOutput == Completed {
		revision = state.ReportDraft
		if revision == nil {
			return sqlite.Execution{}, &Missing{"Report draft not found"}
		}
		var err error
		markdown, err = tx.Draft(row.ID, *revision)
		if err == sql.ErrNoRows {
			return sqlite.Execution{}, &Missing{"Report draft not found"}
		}
		if err != nil {
			return sqlite.Execution{}, err
		}
		if contract.Digest(markdown) != *revision {
			return sqlite.Execution{}, &Conflict{"Reviewed report draft does not match its revision"}
		}
	} else {
		detailsJSON, err := tx.HistoryDetails(row.ID, job.IntentVersion)
		if err != nil {
			return sqlite.Execution{}, err
		}
		var details struct {
			Reason string `json:"reason"`
		}
		if err := json.Unmarshal([]byte(detailsJSON), &details); err != nil {
			return sqlite.Execution{}, err
		}
		attempted, err := tx.AttemptCounts(row.ID)
		if err != nil {
			return sqlite.Execution{}, err
		}
		title := "Failure"
		if *state.PendingOutput == Cancelled {
			title = "Cancellation"
		}
		recorded := func(value *string) string {
			if !present(value) {
				return "None recorded"
			}
			return *value
		}
		markdown = fmt.Sprintf("# %s report\n\nLast stage: %s\n\nReason: %s\n\nStages attempted (attempt counts): %s\n\n"+
			"Partial PR: %s\n\nCandidate: %s\n\nReport draft revision: %s\n\n"+
			"Next action: Review the execution timeline and any partial artifacts. "+
			"Resolve the reason for stopping before rerunning the item.\n",
			title, state.Stage, details.Reason, attempted, recorded(state.PrimaryPR), recorded(state.Candidate), recorded(state.ReportDraft))
	}
	existing, err := tx.Report(row.ID)
	if err == sql.ErrNoRows {
		if err := tx.InsertReport(row.ID, markdown); err != nil {
			return sqlite.Execution{}, err
		}
	} else if err != nil {
		return sqlite.Execution{}, err
	} else if existing != markdown {
		return sqlite.Execution{}, &Conflict{"Existing report differs from the intended final Markdown"}
	}
	output := &StoredReport{Reference: "/v1/items/" + itemID + "/executions/" + row.ID + "/report", DraftRevision: revision}
	updated, err := a.apply(tx, itemID, row, state, Finalize, map[string]any{"report": output.Reference},
		DecisionOptions{Evidence: Evidence{StoredReport: output}}, nil, nil)
	if err != nil {
		return sqlite.Execution{}, err
	}
	next, err := readState(updated.State)
	if err != nil {
		return sqlite.Execution{}, err
	}
	if Terminal(next.Status) {
		if err := tx.EndWriters(row.ID, string(next.Status), timestamp(a.Clock())); err != nil {
			return sqlite.Execution{}, err
		}
	}
	if err := tx.DeleteFinalization(row.ID); err != nil {
		return sqlite.Execution{}, err
	}
	return updated, nil
}

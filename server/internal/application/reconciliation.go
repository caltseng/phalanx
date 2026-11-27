package application

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/caltseng/phalanx/server/internal/contract"
	"github.com/caltseng/phalanx/server/internal/sqlite"
)

func itemBranch(itemID string) string { return "phalanx/" + contract.Digest(itemID) }

func (a *App) RecordObservation(ctx context.Context, itemID, executionID string, request contract.Observation) (json.RawMessage, error) {
	var result json.RawMessage
	err := a.Store.Write(ctx, func(tx *sqlite.Tx) error {
		var err error
		result, err = a.recordObservation(tx, itemID, executionID, request)
		return err
	})
	return result, err
}

func (a *App) recordObservation(tx *sqlite.Tx, itemID, executionID string, request contract.Observation) (json.RawMessage, error) {
	if err := requireExecution(tx, itemID, executionID); err != nil {
		return nil, err
	}
	row, err := tx.Execution(itemID, executionID)
	if err != nil {
		return nil, err
	}
	state, err := readState(row.State)
	if err != nil {
		return nil, err
	}
	var output *PullRequest
	if state.FinalOutput != nil {
		output = state.FinalOutput.PR
	}
	completedReady := state.Status == Completed && state.Stage == Delivery && state.Target == ReadyTarget &&
		state.ReadyRevision != nil && output != nil && !output.Draft && sameText(state.PrimaryPR, &output.URL) &&
		(!present(output.MergeCommit) || request.PR != nil && request.PR.Merged &&
			sameText(request.PR.MergeCommit, output.MergeCommit) && request.PR.HeadSHA == output.Revision.Head)
	if state.FinalOutput != nil && (!completedReady || request.Deployment != nil) &&
		(state.Status != Completed || request.Deployment == nil || request.PR == nil || output == nil ||
			!sameText(output.MergeCommit, &request.Deployment.Commit) || output.Revision.Head != request.PR.HeadSHA) {
		return nil, &Conflict{"Closed executions only accept ready-PR or delivered deployment observations"}
	}
	if completedReady && request.PR == nil {
		return nil, &Conflict{"A completed ready execution requires its primary PR observation"}
	}
	if request.ObservedAt.After(time.Now().UTC().Add(5 * time.Minute)) {
		return nil, &Conflict{"observed_at is in the future"}
	}
	url, head := "", ""
	targetBase := request.TargetBase
	if request.PR != nil {
		item, body, err := requestObject(tx, itemID)
		if err != nil {
			return nil, err
		}
		repository, err := tx.Repository(item.RepositoryID)
		if err != nil {
			return nil, err
		}
		name := githubRepository(repository.Remote)
		if name == nil {
			return nil, &Conflict{"GitHub observations require a registered github.com remote"}
		}
		if request.PR.HeadRef != itemBranch(itemID) {
			return nil, &Conflict{"The PR head branch must be the item branch"}
		}
		url = "https://github.com/" + *name + "/pull/" + string(request.PR.Number)
		if output != nil && !strings.EqualFold(output.URL, url) {
			return nil, &Conflict{"Observation must describe the delivered PR"}
		}
		if present(state.PrimaryPR) && !strings.EqualFold(*state.PrimaryPR, url) {
			return nil, &Conflict{"The PR must be the item's primary PR"}
		}
		if present(state.PrimaryPR) {
			url = *state.PrimaryPR
		}
		head = request.PR.HeadSHA
		if value, exists := body["target_branch"]; exists && value != nil && value != "" {
			text, ok := value.(string)
			if !ok {
				return nil, errors.New("Stored target branch is not text")
			}
			targetBase = &text
		}
		if request.Deployment != nil && request.PR.BaseRef != *targetBase {
			return nil, &Conflict{"Deployment must describe a merge into the item's target branch"}
		}
	}
	raw, err := encoded(request)
	if err != nil {
		return nil, err
	}
	value, _, err := contract.Decode(raw)
	if err != nil {
		return nil, err
	}
	payload, ok := contract.ObjectValues(value)
	if !ok {
		return nil, errors.New("Observation is not an object")
	}
	delete(payload, "observed_at")
	payload["target_base"] = targetBase
	if request.PR != nil {
		pull, ok := contract.ObjectValues(payload["pr"])
		if !ok {
			return nil, errors.New("Observed PR is not an object")
		}
		pull["url"] = url
	}
	text, err := contract.Canonical(payload)
	if err != nil {
		return nil, err
	}
	stamp := timestamp(request.ObservedAt.UTC())
	latest, err := tx.LatestObservationTime(executionID)
	if err != nil {
		return nil, err
	}
	recorded, err := tx.RecordObservation(executionID, url, head, contract.Digest(text), row.Version, stamp, text)
	if err != nil {
		return nil, err
	}
	checked := false
	if head != "" {
		checked, err = tx.HasLocalChecks(executionID, head)
		if err != nil {
			return nil, err
		}
	}
	if request.PR != nil && (latest == nil || stamp >= *latest || request.PR.Merged) {
		if err := a.advanceObservation(tx, itemID, row, state, url, request, *targetBase, stamp); err != nil {
			return nil, err
		}
	}
	return encoded(map[string]any{"recorded": recorded, "local_checks_recorded": checked})
}

func (a *App) advanceObservation(tx *sqlite.Tx, itemID string, row sqlite.Execution, state State, url string,
	body contract.Observation, targetBase, stamp string) error {
	pull := body.PR
	if state.Stage != Delivery || !sameText(state.PrimaryPR, &url) ||
		state.Stopping != nil && !(pull.Merged && pull.BaseRef == targetBase) ||
		state.PendingOutput != nil || pull.Merged && pull.MergeCommit == nil {
		return nil
	}
	var mergedAt *string
	if pull.MergedAt != nil {
		raw, err := pull.MergedAt.MarshalJSON()
		if err != nil {
			return err
		}
		var text string
		if err := json.Unmarshal(raw, &text); err != nil {
			return err
		}
		if strings.HasSuffix(text, "Z") {
			text = strings.TrimSuffix(text, "Z") + "+00:00"
		}
		mergedAt = &text
	}
	pr := &PullRequest{URL: url, Revision: Revision{pull.HeadSHA, pull.BaseSHA}, Draft: pull.Draft,
		Closed: pull.State == "closed", MergeMethod: pull.MergeMethod, MergedAt: mergedAt}
	if pull.Merged {
		pr.MergeCommit = pull.MergeCommit
	}
	ready := pull.State == "open" && !pull.Draft && (pull.Mergeable == nil || *pull.Mergeable) &&
		len(body.Feedback) == 0 && body.Checks != nil && (*body.Checks == "satisfied" || *body.Checks == "not_configured")
	options := DecisionOptions{Evidence: Evidence{PR: pr, PRReady: ready, OffBase: pull.BaseRef != targetBase,
		MergedHeadAncestor: pull.MergedHeadAncestor}}
	next, err := Decide(state, PRObserved, options)
	if err != nil {
		return err
	}
	first, err := contract.Canonical(state)
	if err != nil {
		return err
	}
	second, err := contract.Canonical(next)
	if err != nil || first == second {
		return err
	}
	if !pull.Merged && Terminal(next.Status) {
		handoff, err := tx.Handoff(row.ID)
		if err != nil {
			return err
		}
		rework, err := tx.WriterReworkOutstanding(row.ID)
		if err != nil {
			return err
		}
		if handoff != nil && *handoff != pull.HeadSHA || rework {
			return nil
		}
	}
	details := map[string]any{"pr": url, "pr_state": pull.State, "head": pull.HeadSHA, "observed_at": stamp}
	if pull.Merged {
		details["pr_state"] = "merged"
	}
	if next.Status == WaitingForInput {
		action := "Reopen it and reply to resume delivery, or cancel the item. Phalanx never opens a replacement PR"
		if pr.MergeCommit != nil {
			action = "A merge can't be undone. Rerun the item to deliver into " + targetBase + ", or cancel it"
		}
		details["question"] = map[string]any{"prompt": next.Wait.Reason + ". " + action + ".",
			"handoff": fmt.Sprintf("Item branch %s, head %s", itemBranch(itemID), pull.HeadSHA)}
	}
	if pr.MergeCommit != nil && Terminal(next.Status) {
		details["merge_commit"] = *pr.MergeCommit
		if state.Candidate != nil && *state.Candidate != pull.HeadSHA {
			details["head_drift"] = "Merged head differs from the recorded candidate " + *state.Candidate
		}
	}
	attempt, err := tx.OpenAttempt(row.ID)
	if err != nil {
		return err
	}
	updated, err := a.apply(tx, itemID, row, state, PRObserved, details, options, attempt, nil)
	if err != nil {
		return err
	}
	after, err := readState(updated.State)
	if err != nil {
		return err
	}
	if Terminal(after.Status) {
		return tx.EndWriters(row.ID, string(after.Status), timestamp(a.Clock()))
	}
	return nil
}

func (a *App) RecordLocalChecks(ctx context.Context, itemID, executionID string, request contract.LocalChecks) (json.RawMessage, error) {
	var result json.RawMessage
	err := a.Store.Write(ctx, func(tx *sqlite.Tx) error {
		if err := requireExecution(tx, itemID, executionID); err != nil {
			return err
		}
		row, err := tx.Execution(itemID, executionID)
		if err != nil {
			return err
		}
		state, err := readState(row.State)
		if err != nil {
			return err
		}
		if state.FinalOutput != nil {
			return &Conflict{"Execution is closed"}
		}
		observed, err := tx.HasObservedHead(executionID, request.Head)
		if err != nil {
			return err
		}
		if !observed {
			return &Conflict{"Observe the PR head before reporting its local checks"}
		}
		failures, err := contract.Canonical(request.Failures)
		if err != nil {
			return err
		}
		recorded, err := tx.RecordLocalChecks(executionID, request.Head, row.Version, timestamp(a.Clock()), failures)
		if err != nil {
			return err
		}
		result, err = encoded(map[string]any{"recorded": recorded})
		return err
	})
	return result, err
}

func (a *App) ReconcilerHeartbeat(ctx context.Context, runnerID string, request *contract.ReconcilerHeartbeat) (json.RawMessage, error) {
	var result json.RawMessage
	err := a.Store.Write(ctx, func(tx *sqlite.Tx) error {
		now := timestamp(a.Clock())
		if err := tx.ReconcilerHeartbeat(runnerID, now); err != nil {
			return err
		}
		if request != nil {
			for _, repositoryID := range request.DeploymentOrder {
				name := request.DeploymentChecks[repositoryID]
				rows, err := tx.DeploymentRetirements(repositoryID, name)
				if err != nil {
					return err
				}
				for _, row := range rows {
					value, _, err := contract.Decode([]byte(row.Payload))
					if err != nil {
						return err
					}
					body, ok := contract.ObjectValues(value)
					if !ok {
						return errors.New("Stored observation is not an object")
					}
					pr, ok := contract.ObjectValues(body["pr"])
					if !ok {
						return errors.New("Stored deployment observation has no PR")
					}
					deployment, ok := contract.ObjectValues(body["deployment"])
					if !ok {
						return errors.New("Stored deployment is not an object")
					}
					delete(pr, "url")
					deployment["state"], body["observed_at"] = "disabled", now
					observation, issues := contract.ValidateObservation(body, contract.ObjectKeys(body))
					if len(issues) != 0 {
						return fmt.Errorf("Stored deployment observation is invalid: %v", issues)
					}
					if _, err := a.recordObservation(tx, row.ItemID, row.ExecutionID, observation); err != nil {
						return err
					}
				}
			}
		}
		var err error
		result, err = encoded(map[string]any{"runner_id": runnerID, "last_seen": now})
		return err
	})
	return result, err
}

func (a *App) RetireReconciler(ctx context.Context, runnerID string) (json.RawMessage, error) {
	var result json.RawMessage
	err := a.Store.Write(ctx, func(tx *sqlite.Tx) error {
		retired, err := tx.RetireReconciler(runnerID)
		if err != nil {
			return err
		}
		result, err = encoded(map[string]any{"runner_id": runnerID, "retired": retired})
		return err
	})
	return result, err
}

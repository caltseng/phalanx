package application

import (
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/caltseng/phalanx/server/internal/contract"
	"github.com/caltseng/phalanx/server/internal/sqlite"
)

type projectedPull struct {
	contract.ObservedPull
	URL string `json:"url"`
}

type projectedObservation struct {
	PR           *projectedPull       `json:"pr"`
	Checks       *string              `json:"checks"`
	CheckReasons []string             `json:"check_reasons"`
	Feedback     []string             `json:"feedback"`
	MergedBranch bool                 `json:"merged_branch"`
	TargetBase   *string              `json:"target_base"`
	Deployment   *contract.Deployment `json:"deployment"`
}

func deploymentFlags(row sqlite.ObservationProjection, observation projectedObservation, now *time.Time) ([]FlagView, error) {
	result := []FlagView{}
	deployment := observation.Deployment
	if deployment == nil || deployment.State == "disabled" {
		return result, nil
	}
	expired := false
	var deadline time.Time
	if now != nil {
		closed, err := time.Parse(time.RFC3339Nano, row.ClosedAt)
		if err != nil {
			return nil, err
		}
		deadline = closed.Add(24 * time.Hour)
		expired = !now.Before(deadline)
	}
	subject := contract.FlagKey{ItemID: row.ItemID, ExecutionID: row.ExecutionID}
	if expired && deployment.State == "deployed" {
		return result, nil
	}
	if expired && deployment.State == "pending" {
		return []FlagView{serverFlag("deployment_missing", deployment.CheckName+
			" did not complete successfully within 24 hours of check-in on merge commit "+deployment.Commit,
			"The owner checks the deployment workflow on GitHub; automatic deployment polling has ended",
			pythonStamp(deadline), subject, true)}, nil
	}
	recovery := "Check the post-merge workflow on GitHub and rerun or repair it; reconciliation refreshes this flag"
	if deployment.State == "deployed" {
		recovery = "Informational: GitHub reported a successful deployment check"
	}
	if expired {
		recovery = "The owner checks the failed deployment on GitHub; automatic deployment polling has ended"
	}
	kind := "deployment_" + deployment.State
	if deployment.State == "deployed" {
		kind = "deployed"
	}
	return []FlagView{serverFlag(kind, deployment.CheckName+" on merge commit "+deployment.Commit+": "+deployment.State,
		recovery, row.ObservedAt, subject, false)}, nil
}

func firstPythonLine(value string) string {
	for index, character := range value {
		if character == '\n' || character == '\r' || character == '\v' || character == '\f' ||
			character >= 0x1c && character <= 0x1e || character == 0x85 || character == 0x2028 || character == 0x2029 {
			return value[:index]
		}
	}
	return value
}

func observedFlags(row sqlite.ObservationProjection, state State, observation projectedObservation) ([]FlagView, error) {
	result, err := deploymentFlags(row, observation, nil)
	if err != nil {
		return nil, err
	}
	pull := observation.PR
	subject := contract.FlagKey{ItemID: row.ItemID, ExecutionID: row.ExecutionID}
	if pull == nil {
		if observation.MergedBranch {
			result = append(result, serverFlag("unpublished_branch_merged", itemBranch(row.ItemID)+
				" is reachable from its base, but no PR exists",
				"A Captain confirms the work landed and closes the item, or reopens it", row.ObservedAt, subject, false))
		}
		return result, nil
	}
	if pull.URL == "" {
		return nil, errors.New("Stored observed PR has no URL")
	}
	if state.PublicationMode == DraftMode && pull.State == "open" && !pull.Draft {
		result = append(result, serverFlag("draft_required", pull.URL+" is ready, but this item requires draft-only publication",
			"The writer disables auto-merge and converts the PR to draft; never promote or merge it", row.ObservedAt, subject, true))
	}
	expected := state.Candidate
	if present(row.HandoffHead) {
		expected = row.HandoffHead
	}
	current := !present(expected) || pull.HeadSHA == *expected
	if !current {
		recovery := "Informational: the writer records the new head with its next result"
		recorded := ""
		if present(row.HandoffHead) {
			recorded = "publication handoff " + *row.HandoffHead
			recovery = "Publish the reviewed handoff; if superseded, reacquire Building and review the current head, " +
				"or stop and withdraw the writer to clear the handoff"
		} else {
			recorded = "recorded candidate " + *state.Candidate
			if state.PublicationMode == DraftMode {
				recovery = "Informational: delivery returns the writer to self-review at the new head"
			}
		}
		result = append(result, serverFlag("head_drift", pull.URL+" head "+pull.HeadSHA+" differs from the "+recorded,
			recovery, row.ObservedAt, subject, false))
	}
	if pull.State == "closed" && !pull.Merged {
		result = append(result, serverFlag("pr_closed", pull.URL+" was closed without merging",
			"A Captain asks the owner whether to reopen it; never open a replacement", row.ObservedAt, subject, false))
	}
	writerRecovery := "The writer acts on it, or a Captain relaunches the item"
	if current && pull.State == "open" && pull.Mergeable != nil && !*pull.Mergeable {
		result = append(result, serverFlag("merge_conflict", pull.URL+" conflicts with "+pull.BaseRef,
			writerRecovery, row.ObservedAt, subject, false))
	}
	target := "None"
	if observation.TargetBase != nil {
		target = *observation.TargetBase
	}
	if pull.State == "open" && (observation.TargetBase == nil || pull.BaseRef != target) {
		result = append(result, serverFlag("wrong_base", pull.URL+" merges into "+pull.BaseRef+", not the item's target "+target,
			"The writer retargets the PR; delivery waits until it does", row.ObservedAt, subject, false))
	}
	if current && observation.Checks != nil && *observation.Checks == "failed" {
		reasons := strings.Join(observation.CheckReasons, "; ")
		if reasons == "" {
			reasons = "required checks failed"
		}
		result = append(result, serverFlag("checks_failed", pull.URL+": "+reasons, writerRecovery, row.ObservedAt, subject, false))
	}
	if len(observation.Feedback) > 0 {
		plural := ""
		if len(observation.Feedback) != 1 {
			plural = "s"
		}
		result = append(result, serverFlag("review_feedback", fmt.Sprintf("%s has %d unresolved review item%s", pull.URL,
			len(observation.Feedback), plural), writerRecovery, row.ObservedAt, subject, false))
	}
	if present(row.FailuresJSON) {
		var failures []string
		if err := json.Unmarshal([]byte(*row.FailuresJSON), &failures); err != nil {
			return nil, err
		}
		if current && len(failures) > 0 {
			pieces := []string{}
			for _, failure := range failures {
				if failure == "" {
					return nil, errors.New("Stored local-check failure has no first line")
				}
				pieces = append(pieces, firstPythonLine(failure))
			}
			if row.CheckedAt == nil {
				return nil, errors.New("Stored local-check failure has no timestamp")
			}
			result = append(result, serverFlag("pre_pr_checks_failed", "Local checks failed on "+pull.HeadSHA+": "+
				strings.Join(pieces, "; "), writerRecovery, *row.CheckedAt, subject, false))
		}
	}
	return result, nil
}

func observationFlags(tx *sqlite.Tx, now time.Time, executionID string) ([]FlagView, error) {
	rows, err := tx.ObservationProjections(executionID)
	if err != nil {
		return nil, err
	}
	result := []FlagView{}
	for _, row := range rows {
		state, err := readState([]byte(row.StateJSON))
		if err != nil {
			return nil, err
		}
		var observation projectedObservation
		if err := json.Unmarshal([]byte(row.ObservationJSON), &observation); err != nil {
			return nil, err
		}
		var found []FlagView
		if state.FinalOutput != nil {
			if observation.Deployment == nil {
				continue
			}
			found, err = deploymentFlags(row, observation, &now)
		} else {
			found, err = observedFlags(row, state, observation)
			if err != nil {
				return nil, err
			}
			last, err := time.Parse(time.RFC3339Nano, row.LastObservedAt)
			if err != nil {
				return nil, err
			}
			if elapsedSeconds(now, last) >= 900 {
				for index := range found {
					found[index].Reason += " (last observed " + row.LastObservedAt + ")"
				}
				stale := serverFlag("observation_stale", "GitHub was last observed for this item at "+row.LastObservedAt,
					"A Captain checks the reconciler's pass log for this item's error and repairs it",
					pythonStamp(last.Add(15*time.Minute)), contract.FlagKey{ItemID: row.ItemID, ExecutionID: row.ExecutionID}, false)
				found = append([]FlagView{stale}, found...)
			}
		}
		if err != nil {
			return nil, err
		}
		result = append(result, found...)
	}
	return result, nil
}

func reconcilerStaleFlags(tx *sqlite.Tx, now time.Time) ([]FlagView, error) {
	rows, err := tx.ReconcilerHeartbeats()
	if err != nil {
		return nil, err
	}
	result := []FlagView{}
	for _, row := range rows {
		last, err := time.Parse(time.RFC3339Nano, row.LastSeen)
		if err != nil {
			return nil, err
		}
		if elapsedSeconds(now, last) >= 900 {
			result = append(result, serverFlag("reconciler_stale", "Reconciler "+row.RunnerID+" has sent no heartbeat since "+row.LastSeen,
				"A Captain restarts reconciler "+row.RunnerID+", reports it to the owner, or retires it "+
					"with `phx shift retire-reconciler` if it was decommissioned",
				pythonStamp(last.Add(15*time.Minute)), contract.FlagKey{}, false))
		}
	}
	return result, nil
}

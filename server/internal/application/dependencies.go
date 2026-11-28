package application

import (
	"github.com/caltseng/phalanx/server/internal/sqlite"
)

type resolved struct {
	UpstreamID       string    `json:"upstream_id"`
	Milestone        string    `json:"milestone"`
	ExecutionID      string    `json:"execution_id"`
	Reference        string    `json:"reference"`
	Revision         string    `json:"revision"`
	ReviewedRevision *Revision `json:"reviewed_revision"`
}

type unmet struct {
	UpstreamID  string  `json:"upstream_id"`
	Milestone   string  `json:"milestone"`
	ExecutionID *string `json:"execution_id"`
	Reason      string  `json:"reason"`
}

func unmetDependencies(tx *sqlite.Tx, itemID string) ([]unmet, error) {
	_, unmet, err := resolveDependencies(tx, itemID)
	return unmet, err
}

func resolveDependencies(tx *sqlite.Tx, itemID string) ([]resolved, []unmet, error) {
	links, err := tx.Dependencies(itemID)
	if err != nil {
		return nil, nil, err
	}
	result := []unmet{}
	resolved := []resolved{}
	for _, link := range links {
		if link.ExecutionID == nil {
			result = append(result, unmet{link.UpstreamID, link.Milestone, nil, "Prerequisite execution is missing. Repair or replace it."})
			continue
		}
		state, err := readState([]byte(*link.State))
		if err != nil {
			return nil, nil, err
		}
		var pr *PullRequest
		var report *StoredReport
		if state.FinalOutput != nil {
			pr, report = state.FinalOutput.PR, state.FinalOutput.Report
		}
		reason := "Waiting for prerequisite milestone " + link.Milestone
		if state.PendingOutput != nil && *state.PendingOutput == Completed {
			reason = "Waiting for prerequisite report finalization"
		} else if state.Status == Failed || state.Status == Cancelled || state.Stopping != nil || state.PendingOutput != nil {
			closing := state.Status
			if state.Stopping != nil {
				closing = *state.Stopping
			} else if state.PendingOutput != nil {
				closing = *state.PendingOutput
			}
			reason = "Prerequisite status is " + string(closing) + ". Rerun or replace the prerequisite."
		} else if link.Milestone == "report" {
			if state.Status == Completed && state.Target == ReportTarget && report != nil && present(report.DraftRevision) && link.ReportExists {
				resolved = append(resolved, resolvedDependency(link.Link, *link.ExecutionID, report.Reference, *report.DraftRevision, nil))
				continue
			}
		} else if link.Milestone == "checked_in" {
			if state.Status == Completed && pr != nil && present(pr.MergeCommit) {
				resolved = append(resolved, resolvedDependency(link.Link, *link.ExecutionID, pr.URL, *pr.MergeCommit, &pr.Revision))
				continue
			}
			if state.Status == Completed {
				if state.Stage == Delivery && state.ReadyRevision != nil && pr != nil && !pr.Draft {
					reason = "Waiting for prerequisite's primary PR to merge at its verified head or a descendant"
				} else {
					reason = "Prerequisite completed without check-in. Rerun it with check-in or replace it."
				}
			}
		} else if state.Status == Completed && pr != nil && !pr.Draft {
			resolved = append(resolved, resolvedDependency(link.Link, *link.ExecutionID, pr.URL, pr.Revision.Head, &pr.Revision))
			continue
		} else if state.Status == Completed && pr != nil && pr.Draft {
			reason = "Prerequisite completed as a draft, not ready for check-in. Rerun or replace it."
		} else if state.Stage == Delivery && state.ReadyRevision != nil && present(state.PrimaryPR) && state.Candidate != nil && state.ReadyRevision.Head == *state.Candidate {
			resolved = append(resolved, resolvedDependency(link.Link, *link.ExecutionID, *state.PrimaryPR, state.ReadyRevision.Head, state.ReadyRevision))
			continue
		}
		result = append(result, unmet{link.UpstreamID, link.Milestone, link.ExecutionID, reason})
	}
	return resolved, result, nil
}

func resolvedDependency(link sqlite.Link, executionID, reference, revision string, reviewed *Revision) resolved {
	return resolved{link.UpstreamID, link.Milestone, executionID, reference, revision, reviewed}
}

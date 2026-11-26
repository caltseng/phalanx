package application

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"strings"

	"github.com/caltseng/phalanx/server/internal/contract"
	"github.com/caltseng/phalanx/server/internal/sqlite"
)

type revision struct {
	Head string `json:"head"`
	Base string `json:"base"`
}

type dependencyState struct {
	Target        string    `json:"target"`
	Stage         string    `json:"stage"`
	Status        string    `json:"status"`
	Candidate     *string   `json:"candidate"`
	PrimaryPR     *string   `json:"primary_pr"`
	Stopping      *string   `json:"stopping"`
	PendingOutput *string   `json:"pending_output"`
	ReadyRevision *revision `json:"ready_revision"`
	FinalOutput   *struct {
		Reference     *string  `json:"reference"`
		DraftRevision *string  `json:"draft_revision"`
		URL           *string  `json:"url"`
		Revision      revision `json:"revision"`
		Draft         bool     `json:"draft"`
		MergeCommit   *string  `json:"merge_commit"`
	} `json:"final_output"`
}

type unmet struct {
	UpstreamID  string  `json:"upstream_id"`
	Milestone   string  `json:"milestone"`
	ExecutionID *string `json:"execution_id"`
	Reason      string  `json:"reason"`
}

func unmetDependencies(tx *sqlite.Tx, itemID string) ([]unmet, error) {
	links, err := tx.Links(itemID)
	if err != nil {
		return nil, err
	}
	result := []unmet{}
	for _, link := range links {
		row, err := tx.CurrentExecution(link.UpstreamID)
		if err == sql.ErrNoRows {
			result = append(result, unmet{link.UpstreamID, link.Milestone, nil, "Prerequisite execution is missing. Repair or replace it."})
			continue
		}
		if err != nil {
			return nil, err
		}
		var state dependencyState
		if err := json.Unmarshal(row.State, &state); err != nil {
			return nil, err
		}
		output := state.FinalOutput
		reason := "Waiting for prerequisite milestone " + link.Milestone
		if state.PendingOutput != nil && *state.PendingOutput == "completed" {
			reason = "Waiting for prerequisite report finalization"
		} else if state.Status == "failed" || state.Status == "cancelled" || state.Stopping != nil || state.PendingOutput != nil {
			closing := state.Status
			if state.Stopping != nil {
				closing = *state.Stopping
			} else if state.PendingOutput != nil {
				closing = *state.PendingOutput
			}
			reason = "Prerequisite status is " + closing + ". Rerun or replace the prerequisite."
		} else if link.Milestone == "report" {
			exists, err := tx.ReportExists(row.ID)
			if err != nil {
				return nil, err
			}
			if state.Status == "completed" && state.Target == "report" && output != nil && output.Reference != nil && output.DraftRevision != nil && *output.DraftRevision != "" && exists {
				continue
			}
		} else if link.Milestone == "checked_in" {
			if state.Status == "completed" && output != nil && output.URL != nil && output.MergeCommit != nil && *output.MergeCommit != "" {
				continue
			}
			if state.Status == "completed" {
				if state.Stage == "delivery" && state.ReadyRevision != nil && output != nil && output.URL != nil && !output.Draft {
					reason = "Waiting for prerequisite's primary PR to merge at its verified head or a descendant"
				} else {
					reason = "Prerequisite completed without check-in. Rerun it with check-in or replace it."
				}
			}
		} else if state.Status == "completed" && output != nil && output.URL != nil && !output.Draft {
			continue
		} else if state.Status == "completed" && output != nil && output.URL != nil && output.Draft {
			reason = "Prerequisite completed as a draft, not ready for check-in. Rerun or replace it."
		} else if state.Stage == "delivery" && state.ReadyRevision != nil && state.PrimaryPR != nil && state.Candidate != nil && state.ReadyRevision.Head == *state.Candidate {
			continue
		}
		id := row.ID
		result = append(result, unmet{link.UpstreamID, link.Milestone, &id, reason})
	}
	return result, nil
}

func pythonOrderedJSON(values []unmet) (string, error) {
	entries := make([]string, 0, len(values))
	for _, value := range values {
		upstream, err := contract.Canonical(value.UpstreamID)
		if err != nil {
			return "", err
		}
		milestone, err := contract.Canonical(value.Milestone)
		if err != nil {
			return "", err
		}
		execution, err := contract.Canonical(value.ExecutionID)
		if err != nil {
			return "", err
		}
		reason, err := contract.Canonical(value.Reason)
		if err != nil {
			return "", err
		}
		entries = append(entries, fmt.Sprintf(`{"upstream_id":%s,"milestone":%s,"execution_id":%s,"reason":%s}`, upstream, milestone, execution, reason))
	}
	return "[" + strings.Join(entries, ",") + "]", nil
}

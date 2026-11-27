package application

import (
	"errors"
	"fmt"
	"sort"
	"strings"
	"time"

	"github.com/caltseng/phalanx/server/internal/contract"
	"github.com/caltseng/phalanx/server/internal/sqlite"
)

type pickupCaptain struct {
	ID           string
	Repositories map[string]*string
	RawPickup    any
}

type pickupContext struct {
	AllCaptains []sqlite.Captain
	Live        map[string]sqlite.Captain
	Captains    []pickupCaptain
	Holds       map[string]string
	Capacity    sqlite.DispatchSettings
	Rework      *string
	Admission   map[Stage]*string
}

func liveSnapshot(tx *sqlite.Tx, now time.Time) ([]sqlite.Captain, map[string]sqlite.Captain, error) {
	rows, err := tx.AllCaptains()
	if err != nil {
		return nil, nil, err
	}
	live := map[string]sqlite.Captain{}
	for _, row := range rows {
		if row.State == "ended" {
			continue
		}
		gone, err := captainGone(row)
		if err != nil {
			return nil, nil, err
		}
		if now.Before(gone) {
			live[row.ID] = row
		}
	}
	return rows, live, nil
}

func pickupSnapshot(tx *sqlite.Tx, now time.Time) (pickupContext, error) {
	result := pickupContext{Live: map[string]sqlite.Captain{}, Captains: []pickupCaptain{},
		Holds: map[string]string{}, Admission: map[Stage]*string{}}
	rows, live, err := liveSnapshot(tx, now)
	if err != nil {
		return result, err
	}
	result.AllCaptains, result.Live = rows, live
	ids := make([]string, 0, len(result.Live))
	for id := range result.Live {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	for _, id := range ids {
		row := result.Live[id]
		if row.State != "active" {
			continue
		}
		value, _, err := contract.Decode([]byte(row.PickupJSON))
		if err != nil {
			return result, err
		}
		body, ok := contract.ObjectValues(value)
		if !ok {
			return result, errors.New("Stored Captain pickup is not an object")
		}
		repositories, ok := contract.ObjectValues(body["repositories"])
		if !ok {
			return result, errors.New("Stored Captain repositories are not an object")
		}
		scope := map[string]*string{}
		for repository, value := range repositories {
			var reason *string
			if value != nil {
				text, ok := value.(string)
				if !ok {
					return result, errors.New("Stored Captain readiness reason is not text")
				}
				reason = &text
			}
			scope[repository] = reason
		}
		result.Captains = append(result.Captains, pickupCaptain{ID: row.ID, Repositories: scope, RawPickup: value})
	}
	holds, err := tx.ActiveCaptainHolds(pythonStamp(now.Add(-30 * time.Minute)))
	if err != nil {
		return result, err
	}
	for _, hold := range holds {
		if _, live := result.Live[hold.CaptainID]; live {
			result.Holds[hold.ExecutionID] = hold.Reason
		}
	}
	result.Capacity, err = tx.DispatchSettings()
	if err != nil {
		return result, err
	}
	result.Rework, err = tx.FirstRework(time.Now().UnixMilli())
	if err != nil {
		return result, err
	}
	for _, stage := range []Stage{Development, SelfReview, ReportDrafting, ReportReview, ReportStorage, Publication, Delivery} {
		reason := ""
		if result.Capacity.Paused {
			reason = "Dispatch is paused"
		} else if result.Capacity.ActiveWriters >= result.Capacity.MaxWriters {
			reason = fmt.Sprintf("Total capacity exhausted: max_writers=%d", result.Capacity.MaxWriters)
		} else if needsBuilding(stage) {
			result.Admission[stage] = buildingReason(result.Capacity, result.Rework, "")
			continue
		}
		var wait *string
		if reason != "" {
			wait = &reason
		}
		result.Admission[stage] = wait
	}
	return result, nil
}

type PickupDecision struct {
	Eligible   bool     `json:"eligible"`
	Code       string   `json:"code"`
	Reason     *string  `json:"reason"`
	CaptainIDs []string `json:"captain_ids,omitempty"`
	Trigger    string   `json:"trigger"`
}

func blockedPickup(code, reason, trigger string) PickupDecision {
	return PickupDecision{Code: code, Reason: &reason, Trigger: trigger}
}

func pickupDecision(row sqlite.QueueProjection, now time.Time, current pickupContext) (PickupDecision, error) {
	state, err := readState([]byte(row.StateJSON))
	if err != nil {
		return PickupDecision{}, err
	}
	if state.Stopping != nil || state.PendingOutput != nil {
		return blockedPickup("closing", "Execution is stopping or finalizing", "Finish the pending finalization"), nil
	}
	if current.Capacity.Paused {
		return blockedPickup("paused", "Dispatch is paused", "The owner resumes dispatch"), nil
	}
	if row.DependencyWait != nil {
		value, _, err := contract.Decode([]byte(*row.DependencyWait))
		if err != nil {
			return PickupDecision{}, err
		}
		links, ok := value.([]any)
		if !ok {
			return PickupDecision{}, errors.New("Stored dependency wait is not a list")
		}
		reason := "Persisted dependency wait requires reconciliation"
		if len(links) > 0 {
			pieces := []string{}
			for _, value := range links {
				link, ok := contract.ObjectValues(value)
				if !ok {
					return PickupDecision{}, errors.New("Stored dependency wait entry is not an object")
				}
				id, ok := link["upstream_id"].(string)
				if !ok {
					return PickupDecision{}, errors.New("Stored dependency upstream id is not text")
				}
				message, ok := link["reason"].(string)
				if !ok {
					return PickupDecision{}, errors.New("Stored dependency reason is not text")
				}
				pieces = append(pieces, id+": "+message)
			}
			reason = "Waiting on dependency " + strings.Join(pieces, "; ")
		}
		return blockedPickup("dependency", reason, "Prerequisite milestone or dependency reconciliation"), nil
	}
	nowMS := now.UnixMicro() / 1000
	if row.RetryAt != nil && *row.RetryAt > nowMS {
		return blockedPickup("retry", fmt.Sprintf("Retry scheduled for %d ms since epoch", *row.RetryAt), "The retry delay elapses"), nil
	}
	if row.ReadySince == nil {
		return blockedPickup("not_ready", "Execution has no ready scheduling timestamp", "A Captain reconciles the execution's scheduling state"), nil
	}
	if *row.ReadySince > nowMS {
		return blockedPickup("not_ready", fmt.Sprintf("Execution scheduled for %d ms since epoch", *row.ReadySince), "The scheduling delay elapses"), nil
	}
	if hold := current.Holds[row.ID]; hold != "" {
		return blockedPickup("held", hold, "The Captain clears the hold or it expires"), nil
	}
	captains, unknown := []pickupCaptain{}, []string{}
	for _, captain := range current.Captains {
		if len(captain.Repositories) == 0 {
			unknown = append(unknown, captain.ID)
		} else {
			captains = append(captains, captain)
		}
	}
	if len(captains) == 0 && len(unknown) > 0 {
		return blockedPickup("uninitialized", "Captain pickup scope/readiness is uninitialized",
			"An updated Captain heartbeat publishes the current charter and readiness"), nil
	}
	if len(captains) == 0 {
		return blockedPickup("no_captain", "No live active Captain with pickup authority", "Start or heartbeat an authorized Captain"), nil
	}
	scoped := []pickupCaptain{}
	for _, captain := range captains {
		if _, exists := captain.Repositories[row.RepositoryID]; exists {
			scoped = append(scoped, captain)
		}
	}
	if len(scoped) == 0 {
		if len(unknown) > 0 {
			return blockedPickup("uninitialized", "Repository has no reported pickup scope; Captain scope/readiness is uninitialized: "+strings.Join(unknown, ", "),
				"An updated Captain heartbeat publishes the current charter and readiness"), nil
		}
		return blockedPickup("out_of_scope", "Repository "+row.RepositoryID+" is outside active Captain charter scope",
			"The owner assigns a Captain whose charter includes this repository"), nil
	}
	reasons, available := []string{}, []string{}
	for _, captain := range scoped {
		if reason := captain.Repositories[row.RepositoryID]; reason != nil {
			reasons = append(reasons, *reason)
		} else {
			available = append(available, captain.ID)
		}
	}
	if len(available) == 0 {
		return blockedPickup("repository_blocked", strings.Join(uniqueStrings(reasons), "; "),
			"Resolve the checkout mapping or owner trust blocker; heartbeat readiness"), nil
	}
	if waiting := current.Admission[Stage(row.Stage)]; waiting != nil {
		return blockedPickup("capacity", *waiting, "Capacity becomes available; a Captain retries admission"), nil
	}
	return PickupDecision{Eligible: true, Code: "eligible", CaptainIDs: available,
		Trigger: "A Captain launches this item's writer"}, nil
}

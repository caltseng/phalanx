package application

import (
	"encoding/json"
	"errors"
	"strings"
)

type Stage string
type Status string
type Target string
type PublicationMode string
type Event string
type Outcome string

const (
	Development    Stage = "development"
	SelfReview     Stage = "self_review"
	ReportDrafting Stage = "report_drafting"
	ReportReview   Stage = "report_review"
	ReportStorage  Stage = "report_storage"
	Publication    Stage = "publication"
	Delivery       Stage = "delivery"

	Queued             Status = "queued"
	Running            Status = "running"
	WaitingForInput    Status = "waiting_for_input"
	WaitingForExternal Status = "waiting_for_external"
	Completed          Status = "completed"
	Failed             Status = "failed"
	Cancelled          Status = "cancelled"

	ReportTarget Target = "report"
	ReadyTarget  Target = "ready_for_check_in"
	MergedTarget Target = "checked_in"

	ReadyMode PublicationMode = "ready"
	DraftMode PublicationMode = "draft"

	Claim         Event = "claim"
	Result        Event = "result"
	ReplyEvent    Event = "reply"
	Retry         Event = "retry"
	Cancel        Event = "cancel"
	CloseFailure  Event = "close_failure"
	StopConfirmed Event = "stop_confirmed"
	PRObserved    Event = "pr_observed"
	Finalize      Event = "finalize"

	Succeeded       Outcome = "succeeded"
	ChangesRequired Outcome = "changes_required"
	NeedsInput      Outcome = "needs_input"
	FailureOutcome  Outcome = "failed"
)

func Terminal(status Status) bool {
	return status == Completed || status == Failed || status == Cancelled
}

type Revision struct {
	Head string `json:"head"`
	Base string `json:"base"`
}

type PullRequest struct {
	URL         string   `json:"url"`
	Revision    Revision `json:"revision"`
	Draft       bool     `json:"draft"`
	MergeCommit *string  `json:"merge_commit"`
	Closed      bool     `json:"closed"`
	MergeMethod *string  `json:"merge_method"`
	MergedAt    *string  `json:"merged_at"`
}

type StoredReport struct {
	Reference     string  `json:"reference"`
	DraftRevision *string `json:"draft_revision"`
}

type FinalOutput struct {
	PR     *PullRequest
	Report *StoredReport
}

func (output FinalOutput) MarshalJSON() ([]byte, error) {
	value, err := output.JSONValue()
	if err != nil {
		return nil, err
	}
	return json.Marshal(value)
}

func (output FinalOutput) JSONValue() (any, error) {
	if (output.PR == nil) == (output.Report == nil) {
		return nil, errors.New("Exactly one final output variant is required")
	}
	if output.PR != nil {
		return output.PR, nil
	}
	return output.Report, nil
}

func (output *FinalOutput) UnmarshalJSON(data []byte) error {
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(data, &fields); err != nil {
		return err
	}
	*output = FinalOutput{}
	if _, ok := fields["reference"]; ok {
		output.Report = &StoredReport{}
		return json.Unmarshal(data, output.Report)
	}
	output.PR = &PullRequest{}
	return json.Unmarshal(data, output.PR)
}

type Wait struct {
	Reason  string `json:"reason"`
	Trigger string `json:"trigger"`
}

type State struct {
	Target               Target          `json:"target"`
	Stage                Stage           `json:"stage"`
	Status               Status          `json:"status"`
	Candidate            *string         `json:"candidate"`
	PrimaryPR            *string         `json:"primary_pr"`
	ReportDraft          *string         `json:"report_draft"`
	Wait                 *Wait           `json:"wait"`
	Stopping             *Status         `json:"stopping"`
	PendingOutput        *Status         `json:"pending_output"`
	ReadyRevision        *Revision       `json:"ready_revision"`
	FinalOutput          *FinalOutput    `json:"final_output"`
	ReportReviewFindings *string         `json:"report_review_findings"`
	Findings             *string         `json:"findings"`
	PublicationMode      PublicationMode `json:"publication_mode"`
}

func Start(target Target, mode PublicationMode) State {
	stage := Development
	if target == ReportTarget {
		stage = ReportDrafting
	}
	return State{Target: target, Stage: stage, Status: Queued, PublicationMode: mode}
}

type Evidence struct {
	Candidate          *string       `json:"candidate"`
	PR                 *PullRequest  `json:"pr"`
	ReviewedCandidate  *string       `json:"reviewed_candidate"`
	ReportDraft        *string       `json:"report_draft"`
	ReviewedReport     *string       `json:"reviewed_report"`
	StoredReport       *StoredReport `json:"stored_report"`
	ReportReviewNotes  *string       `json:"report_review_notes"`
	Findings           *string       `json:"findings"`
	PRReady            bool          `json:"pr_ready"`
	OffBase            bool          `json:"off_base"`
	PublishedPR        *string       `json:"published_pr"`
	MergedHeadAncestor *string       `json:"merged_head_ancestor"`
}

type DecisionOptions struct {
	Outcome      Outcome  `json:"outcome"`
	Evidence     Evidence `json:"evidence"`
	Wait         *Wait    `json:"wait"`
	RetryAllowed bool     `json:"retry_allowed"`
}

func present(value *string) bool { return value != nil && *value != "" }

func sameText(a, b *string) bool {
	return a != nil && b != nil && *a == *b
}

func queue(state State, stage Stage) State {
	if stage != "" {
		state.Stage = stage
	}
	state.Status, state.Wait = Queued, nil
	return state
}

func waiting(state State, status Status, wait *Wait) (State, error) {
	if wait == nil {
		return State{}, &Conflict{"Waiting requires a reason and resume trigger"}
	}
	state.Status, state.Wait = status, wait
	return state, nil
}

func closeState(state State, status Status, output FinalOutput) (State, error) {
	if !Terminal(status) {
		return State{}, &Conflict{"Closure requires a terminal status"}
	}
	if status != Completed || state.Target == ReportTarget {
		if output.Report == nil || strings.TrimSpace(output.Report.Reference) == "" {
			return State{}, &Conflict{"Closure requires a stored Markdown report"}
		}
	} else if output.PR == nil {
		return State{}, &Conflict{"Closure requires a verified PR"}
	}
	state.Status, state.FinalOutput = status, &output
	state.Wait, state.Stopping, state.PendingOutput = nil, nil, nil
	return state, nil
}

func finalizeReport(state State, status Status, report *StoredReport) (State, error) {
	if report != nil {
		return closeState(state, status, FinalOutput{Report: report})
	}
	state.Stopping, state.PendingOutput = nil, &status
	return waiting(state, WaitingForExternal, &Wait{"Store the final Markdown report", "retry finalization"})
}

func deliveryWait(state State) *Wait {
	if state.PublicationMode == DraftMode {
		return &Wait{"Verify the primary PR is an open draft at the reviewed candidate and target base", "reconciler observation"}
	}
	return &Wait{"GitHub checks, reviews and merge of the primary PR", "reconciler observation"}
}

func prObserved(state State, evidence Evidence) (State, error) {
	pr := evidence.PR
	if state.Stage != Delivery || state.Target == ReportTarget || pr == nil || pr.URL == "" ||
		pr.Revision.Head == "" || pr.Revision.Base == "" || state.PrimaryPR == nil || *state.PrimaryPR != pr.URL {
		return State{}, &Conflict{"A PR observation requires the PR this execution's writer published, in delivery"}
	}
	if state.Status == Completed {
		var output *PullRequest
		if state.FinalOutput != nil {
			output = state.FinalOutput.PR
		}
		if state.Target != ReadyTarget || output == nil || output.Draft || present(output.MergeCommit) ||
			state.ReadyRevision == nil || !pr.Closed || pr.Draft || !present(pr.MergeCommit) ||
			!present(pr.MergedAt) || evidence.OffBase ||
			(pr.Revision.Head != output.Revision.Head &&
				(evidence.MergedHeadAncestor == nil || *evidence.MergedHeadAncestor != output.Revision.Head)) {
			return state, nil
		}
		state.Candidate, state.ReadyRevision, state.FinalOutput = &pr.Revision.Head, &pr.Revision, &FinalOutput{PR: pr}
		return state, nil
	}
	if present(pr.MergeCommit) && evidence.OffBase {
		state.ReadyRevision = nil
		return waiting(state, WaitingForInput,
			&Wait{pr.URL + " was merged into a branch other than the item's target", "owner reply"})
	}
	if present(pr.MergeCommit) {
		if pr.Draft {
			return State{}, &Conflict{"A verified merge cannot be a draft"}
		}
		state.Candidate, state.ReadyRevision = &pr.Revision.Head, &pr.Revision
		return closeState(state, Completed, FinalOutput{PR: pr})
	}
	if state.Status != WaitingForExternal {
		return state, nil
	}
	if state.PublicationMode == DraftMode && !pr.Closed {
		if state.Candidate == nil || pr.Revision.Head != *state.Candidate {
			state.Candidate, state.ReadyRevision = &pr.Revision.Head, nil
			return queue(state, SelfReview), nil
		}
		state.ReadyRevision = nil
		if pr.Draft && !evidence.OffBase {
			return closeState(state, Completed, FinalOutput{PR: pr})
		}
		return state, nil
	}
	if state.PublicationMode != DraftMode {
		state.Candidate = &pr.Revision.Head
	}
	if pr.Closed {
		state.ReadyRevision = nil
		return waiting(state, WaitingForInput, &Wait{pr.URL + " was closed without merging", "owner reply"})
	}
	if !evidence.PRReady || evidence.OffBase {
		state.ReadyRevision = nil
		return state, nil
	}
	state.ReadyRevision = &pr.Revision
	if state.Target == ReadyTarget {
		return closeState(state, Completed, FinalOutput{PR: pr})
	}
	return state, nil
}

func successful(state State, evidence Evidence) (State, error) {
	switch state.Stage {
	case Development:
		if !present(evidence.Candidate) {
			return State{}, &Conflict{"Development requires a recoverable candidate"}
		}
		state.Candidate, state.Findings = evidence.Candidate, nil
		return queue(state, SelfReview), nil
	case Publication:
		if !present(evidence.PublishedPR) {
			return State{}, &Conflict{"Publication requires the published PR's URL"}
		}
		state.Stage, state.PrimaryPR = Delivery, evidence.PublishedPR
		return waiting(state, WaitingForExternal, deliveryWait(state))
	case Delivery:
		return waiting(state, WaitingForExternal, deliveryWait(state))
	case SelfReview:
		if !present(state.Candidate) || !sameText(state.Candidate, evidence.ReviewedCandidate) {
			return State{}, &Conflict{"Self-review evidence must match the candidate"}
		}
		return queue(state, Publication), nil
	case ReportDrafting:
		if !present(evidence.ReportDraft) {
			return State{}, &Conflict{"Drafting requires a recoverable report"}
		}
		state.ReportDraft, state.ReportReviewFindings = evidence.ReportDraft, nil
		return queue(state, ReportReview), nil
	case ReportReview:
		if !present(state.ReportDraft) || !sameText(state.ReportDraft, evidence.ReviewedReport) {
			return State{}, &Conflict{"Review must verify the current report draft"}
		}
		return queue(state, ReportStorage), nil
	case ReportStorage:
		if evidence.StoredReport == nil {
			if !present(state.ReportDraft) || !sameText(state.ReportDraft, evidence.ReportDraft) {
				return State{}, &Conflict{"Storage requires the current reviewed report draft"}
			}
			return finalizeReport(state, Completed, nil)
		}
		if !present(state.ReportDraft) || !sameText(state.ReportDraft, evidence.StoredReport.DraftRevision) {
			return State{}, &Conflict{"Storage must persist the reviewed report draft"}
		}
		return closeState(state, Completed, FinalOutput{Report: evidence.StoredReport})
	default:
		return State{}, &Conflict{"Unsupported successful stage result"}
	}
}

func changes(state State, evidence Evidence) (State, error) {
	switch state.Stage {
	case Publication, Delivery:
		return State{}, &Conflict{"Push fixes to the open PR; publication and delivery take no changes_required"}
	case ReportReview:
		if !present(state.ReportDraft) || !sameText(state.ReportDraft, evidence.ReportDraft) {
			return State{}, &Conflict{"Findings must match the report draft"}
		}
		state.ReportReviewFindings = evidence.ReportReviewNotes
		return queue(state, ReportDrafting), nil
	case SelfReview:
		if !present(state.Candidate) || !sameText(state.Candidate, evidence.Candidate) {
			return State{}, &Conflict{"Findings must match the candidate"}
		}
		state.Findings = evidence.Findings
		return queue(state, Development), nil
	default:
		return State{}, &Conflict{"Changes-required requires a self-review or report review"}
	}
}

func Decide(state State, event Event, options DecisionOptions) (State, error) {
	evidence := options.Evidence
	if state.Status == Completed && event == PRObserved {
		return prObserved(state, evidence)
	}
	if Terminal(state.Status) {
		return State{}, &Conflict{"A closed execution must be rerun as a new execution"}
	}
	if state.PendingOutput != nil {
		if event != Finalize {
			return State{}, &Conflict{"Only finalization may resume this execution"}
		}
		if evidence.StoredReport == nil && options.Wait != nil {
			state.Wait = options.Wait
			return state, nil
		}
		if *state.PendingOutput == Completed && evidence.StoredReport != nil &&
			(state.Stage != ReportStorage || !sameText(state.ReportDraft, evidence.StoredReport.DraftRevision)) {
			return State{}, &Conflict{"Finalization requires the reviewed report revision"}
		}
		return finalizeReport(state, *state.PendingOutput, evidence.StoredReport)
	}
	if state.Stopping != nil {
		if event == PRObserved && evidence.PR != nil && present(evidence.PR.MergeCommit) && !evidence.OffBase {
			return prObserved(state, evidence)
		}
		if event != StopConfirmed {
			return State{}, &Conflict{"Reconcile outstanding effects before closing"}
		}
		return finalizeReport(state, *state.Stopping, evidence.StoredReport)
	}
	if event == PRObserved {
		return prObserved(state, evidence)
	}
	if event == Cancel || event == CloseFailure {
		status := Cancelled
		if event == CloseFailure {
			status = Failed
		}
		if state.Status == Running || state.Status == WaitingForExternal || state.Stage == Delivery {
			state.Stopping = &status
			return waiting(state, WaitingForExternal,
				&Wait{"Reconcile the worker and outstanding effects", "stop confirmed"})
		}
		return finalizeReport(state, status, evidence.StoredReport)
	}
	switch event {
	case Claim:
		if state.Status != Queued {
			return State{}, &Conflict{"Only queued work can be claimed"}
		}
		state.Status = Running
		return state, nil
	case ReplyEvent:
		if state.Status != WaitingForInput {
			return State{}, &Conflict{"No input wait is active"}
		}
		return queue(state, ""), nil
	case Retry:
		if state.Status != Running && state.Status != WaitingForInput {
			return State{}, &Conflict{"Retry requires an active attempt or input wait"}
		}
		if options.RetryAllowed {
			return queue(state, ""), nil
		}
		return waiting(state, WaitingForInput, options.Wait)
	}
	if event == Result && state.Stage == Delivery && state.Status == WaitingForExternal && options.Outcome == NeedsInput {
		return waiting(state, WaitingForInput, options.Wait)
	}
	if event != Result || state.Status != Running {
		return State{}, &Conflict{"Stage results require a running attempt"}
	}
	switch options.Outcome {
	case Succeeded:
		return successful(state, evidence)
	case ChangesRequired:
		return changes(state, evidence)
	case FailureOutcome:
		if options.RetryAllowed {
			return queue(state, ""), nil
		}
		return waiting(state, WaitingForInput, options.Wait)
	case NeedsInput:
		return waiting(state, WaitingForInput, options.Wait)
	default:
		return State{}, &Conflict{"Missing stage outcome"}
	}
}

package application

import (
	"encoding/json"
	"testing"
)

func pointer[T any](value T) *T { return &value }

func TestMergeWinsOverOutstandingStop(t *testing.T) {
	state := Start(MergedTarget, ReadyMode)
	state.Stage, state.Status = Delivery, WaitingForExternal
	state.PrimaryPR = pointer("https://github.com/example/fixture/pull/1")
	state.Stopping = pointer(Cancelled)
	pr := &PullRequest{URL: *state.PrimaryPR, Revision: Revision{"new-head", "main"},
		MergeCommit: pointer("merge"), Closed: true, MergedAt: pointer("2026-01-02")}
	next, err := Decide(state, PRObserved, DecisionOptions{Evidence: Evidence{PR: pr}})
	if err != nil || next.Status != Completed || next.Stopping != nil || next.FinalOutput.PR != pr {
		t.Fatal("verified primary merge did not win over unconfirmed stop", next, err)
	}
	next, err = Decide(state, PRObserved, DecisionOptions{Evidence: Evidence{PR: pr, OffBase: true}})
	if err == nil {
		t.Fatal("wrong-base merge bypassed stop reconciliation", next)
	}
}

func TestFinalizationAndDraftRevisionFence(t *testing.T) {
	state := Start(ReportTarget, ReadyMode)
	state.Stage, state.Status, state.ReportDraft = ReportStorage, Running, pointer("reviewed")
	next, err := Decide(state, Result, DecisionOptions{Outcome: Succeeded, Evidence: Evidence{ReportDraft: pointer("reviewed")}})
	if err != nil || next.PendingOutput == nil || *next.PendingOutput != Completed || next.FinalOutput != nil {
		t.Fatal("storage closed before report persistence", next, err)
	}
	if _, err := Decide(next, Claim, DecisionOptions{}); err == nil {
		t.Fatal("finalization intent permitted a new attempt")
	}
	report := &StoredReport{Reference: "/report", DraftRevision: pointer("other")}
	if _, err := Decide(next, Finalize, DecisionOptions{Evidence: Evidence{StoredReport: report}}); err == nil {
		t.Fatal("unreviewed report revision finalized")
	}
	report.DraftRevision = pointer("reviewed")
	closed, err := Decide(next, Finalize, DecisionOptions{Evidence: Evidence{StoredReport: report}})
	if err != nil || closed.Status != Completed || closed.PendingOutput != nil ||
		closed.FinalOutput == nil || closed.FinalOutput.Report != report {
		t.Fatal("reviewed persistent report did not close", closed, err)
	}
	data, err := json.Marshal(closed)
	if err != nil {
		t.Fatal(err)
	}
	var restored State
	if err := json.Unmarshal(data, &restored); err != nil ||
		restored.FinalOutput == nil || restored.FinalOutput.Report == nil ||
		!sameText(restored.ReportDraft, restored.FinalOutput.Report.DraftRevision) {
		t.Fatal("report state did not roundtrip", err, string(data))
	}
}

func TestChangedDraftHeadQueuesSameExecutionForReview(t *testing.T) {
	state := Start(MergedTarget, DraftMode)
	state.Stage, state.Status = Delivery, WaitingForExternal
	state.Candidate, state.PrimaryPR = pointer("old"), pointer("https://github.com/example/fixture/pull/1")
	pr := &PullRequest{URL: *state.PrimaryPR, Revision: Revision{"new", "main"}, Draft: true}
	next, err := Decide(state, PRObserved, DecisionOptions{Evidence: Evidence{PR: pr}})
	if err != nil || next.Stage != SelfReview || next.Status != Queued ||
		!sameText(next.PrimaryPR, state.PrimaryPR) || *next.Candidate != "new" || next.FinalOutput != nil {
		t.Fatal("changed draft head delivered without exact-head review", next, err)
	}
}

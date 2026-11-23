import unittest
from dataclasses import replace

from phalanx.transitions import (
    Bypass, Event, Evidence, Outcome, PullRequest, Revision, Stage,
    Status, StoredReport, Target, Wait, decide, start,
)


REV = Revision("candidate", "base")
DRAFT = PullRequest("https://example.com/pull/1", REV, draft=True)
READY = replace(DRAFT, draft=False)
GATES = Evidence(pr=READY, ci_and_previews=REV, independent_review=REV)
WAIT = Wait("Await verification", "scheduled recheck")


def result(state, evidence=Evidence(), outcome=Outcome.SUCCEEDED, **kwargs):
    if state.status is Status.QUEUED:
        state = decide(state, Event.CLAIM)
    return decide(state, Event.RESULT, outcome=outcome, evidence=evidence, **kwargs)


def promotion(target=Target.READY, bypass=None):
    state = result(start(target), Evidence(candidate=REV.head))
    state = result(state, Evidence(pr=DRAFT))
    state = result(state, Evidence(reviewed_candidate=REV.head))
    return result(state, Evidence(prevalidated_candidate=REV.head, bypass=bypass))


def ready(target=Target.READY):
    """A ready PR at reconciliation with a passing independent review."""
    state = result(promotion(target), Evidence(pr=READY, draft_checks=REV))
    state = result(state, Evidence(pr=READY, ci_and_previews=REV))
    state = result(state, Evidence(pr=READY, ci_and_previews=REV))
    return result(state, Evidence(pr=READY, independent_review=REV))


class TransitionTests(unittest.TestCase):
    def test_draft_first_and_ready_only(self):
        state = result(start(Target.READY), Evidence(candidate=REV.head))
        self.assertEqual((state.stage, state.status), (Stage.PR_CREATION, Status.QUEUED))
        with self.assertRaises(ValueError):
            result(state, Evidence(pr=READY))
        self.assertEqual(result(state, Evidence(pr=DRAFT)).primary_pr, DRAFT.url)
        state = ready()
        self.assertEqual(state.stage, Stage.PR_RECONCILIATION)
        state = result(state, GATES)
        self.assertEqual((state.status, state.final_output), (Status.COMPLETED, READY))

    def test_draft_gate_and_post_promotion_ci_after_bypass(self):
        with self.assertRaises(ValueError):
            promotion(bypass=Bypass(("test",), "urgent", ""))
        state = promotion(bypass=Bypass(("test",), "urgent", "repo policy"))
        with self.assertRaises(ValueError):
            result(state, Evidence(pr=READY))
        state = decide(decide(state, Event.CLAIM), Event.EXTERNAL_WAIT, wait=WAIT)
        self.assertEqual((state.status, state.wait), (Status.WAITING_FOR_EXTERNAL, WAIT))
        state = decide(state, Event.RECHECK)
        state = result(state, Evidence(pr=READY, draft_checks=REV))
        with self.assertRaises(ValueError):
            result(state, Evidence(pr=READY))
        state = result(state, Evidence(pr=READY), Outcome.CHANGES_REQUIRED)
        self.assertEqual(state.stage, Stage.PR_RECONCILIATION)

    def test_current_review_is_reused_after_checks(self):
        state = result(promotion(), Evidence(pr=READY, draft_checks=REV))
        self.assertEqual(result(state, GATES).stage, Stage.PR_RECONCILIATION)

    def test_ready_pr_is_revised_through_conflict_failed_check_and_feedback(self):
        """Acceptance: post-promotion fixes never return the PR to draft or development."""
        state = ready()
        seen = {state.stage}
        head = REV.head
        for round_, findings in enumerate(["The PR conflicts with main", "Required checks did not pass",
                                           "New actionable review feedback"]):
            observed = replace(READY, revision=Revision(head, REV.base))
            state = result(state, Evidence(pr=observed, findings=findings), Outcome.CHANGES_REQUIRED)
            self.assertEqual((state.stage, state.findings, state.primary_pr),
                             (Stage.PR_REVISION, findings, READY.url))
            with self.assertRaises(ValueError):
                result(state, Evidence(pr=observed, candidate=head))
            head = f"revised-{round_}"
            revised = replace(READY, revision=Revision(head, REV.base))
            state = result(state, Evidence(pr=revised, candidate=head))
            self.assertEqual((state.stage, state.candidate, state.findings),
                             (Stage.PR_VALIDATION, head, None))
            state = result(state, Evidence(pr=revised, ci_and_previews=revised.revision))
            seen.add(state.stage)
            state = result(state, Evidence(pr=revised, ci_and_previews=revised.revision))
            self.assertEqual(state.stage, Stage.ADVERSARIAL_REVIEW)
            state = result(state, Evidence(pr=revised, independent_review=revised.revision))
            seen.update({Stage.PR_REVISION, state.stage})
        self.assertFalse(seen & {Stage.DEVELOPMENT, Stage.PR_PROMOTION, Stage.PR_CREATION})
        done = result(state, Evidence(pr=revised, ci_and_previews=revised.revision,
                                      independent_review=revised.revision))
        self.assertEqual((done.status, done.final_output), (Status.COMPLETED, revised))

    def test_revision_escalates_new_scope_and_reconciles_a_lost_push(self):
        state = result(ready(), Evidence(pr=READY, findings="fix"), Outcome.CHANGES_REQUIRED)
        lost = replace(READY, revision=Revision("foreign", REV.base))
        reconciling = result(state, Evidence(pr=lost, candidate="mine"))
        self.assertEqual((reconciling.stage, reconciling.candidate), (Stage.PR_RECONCILIATION, "mine"))
        escalated = result(state, Evidence(candidate=REV.head, findings="needs a redesign"),
                           Outcome.CHANGES_REQUIRED)
        self.assertEqual((escalated.stage, escalated.findings), (Stage.DEVELOPMENT, "needs a redesign"))
        self.assertEqual(result(escalated, Evidence(candidate="new")).stage, Stage.SELF_REVIEW)

    def test_promotion_failures_and_check_in_conflicts_route_to_their_fixers(self):
        state = result(promotion(), Evidence(pr=READY, findings="draft check failed"), Outcome.CHANGES_REQUIRED)
        self.assertEqual(state.stage, Stage.DEVELOPMENT)
        state = result(ready(Target.MERGED), replace(GATES, merge_authorized=True))
        state = result(state, Evidence(pr=READY, findings="conflict"), Outcome.CHANGES_REQUIRED)
        self.assertEqual(state.stage, Stage.PR_REVISION)

    def test_reconciliation_rejects_stale_gates_and_rereviews_a_new_head(self):
        for revision in [Revision("outside", REV.base), Revision(REV.head, "new-base")]:
            with self.subTest(revision=revision), self.assertRaises(ValueError):
                result(ready(), replace(GATES, pr=replace(READY, revision=revision)))
        moved = Revision(REV.head, "new-base")
        state = result(ready(), Evidence(pr=replace(READY, revision=moved), ci_and_previews=moved,
                                         independent_review=REV))
        self.assertEqual(state.stage, Stage.ADVERSARIAL_REVIEW)

    def test_merge_left_to_owner_and_ambiguous_merge_recovery(self):
        state = result(ready(Target.MERGED), GATES)
        self.assertEqual((state.status, state.target, state.final_output),
                         (Status.COMPLETED, Target.READY, READY))
        state = result(ready(Target.MERGED), replace(GATES, merge_authorized=True))
        self.assertEqual(state.stage, Stage.CHECK_IN)
        state = decide(decide(state, Event.CLAIM), Event.EXTERNAL_WAIT, wait=WAIT)
        merged = replace(READY, merge_commit="merged", closed=True)
        with self.assertRaises(ValueError):
            decide(state, Event.MERGE_VERIFIED,
                   evidence=Evidence(pr=replace(merged, revision=Revision(REV.head, "other"))))
        state = decide(state, Event.MERGE_VERIFIED, evidence=Evidence(pr=merged))
        self.assertEqual((state.status, state.final_output), (Status.COMPLETED, merged))

    def test_closed_pr_requires_a_decision(self):
        state = result(ready(), Evidence(pr=replace(READY, closed=True)),
                       wait=Wait("PR closed without merge", "owner reply"))
        self.assertEqual((state.status, state.primary_pr), (Status.WAITING_FOR_INPUT, READY.url))

    def test_writer_publishes_and_github_observations_deliver(self):
        """A writer skips the runner stages; review rework returns through development."""
        state = result(start(Target.MERGED), Evidence(candidate=REV.head), direct=True)
        self.assertEqual(state.stage, Stage.SELF_REVIEW)
        rework = result(state, Evidence(candidate=REV.head, findings="Fix"), Outcome.CHANGES_REQUIRED, direct=True)
        self.assertEqual(rework.stage, Stage.DEVELOPMENT)
        state = result(state, Evidence(reviewed_candidate=REV.head), direct=True)
        self.assertEqual(state.stage, Stage.PUBLICATION)
        running = decide(state, Event.CLAIM)
        self.assertEqual(decide(running, Event.PR_OBSERVED, evidence=Evidence(pr=READY, pr_ready=True)), running)
        with self.assertRaises(ValueError):
            decide(running, Event.RESULT, outcome=Outcome.CHANGES_REQUIRED, evidence=Evidence(findings="x"))
        waiting = decide(running, Event.RESULT, outcome=Outcome.SUCCEEDED, direct=True)
        self.assertEqual((waiting.stage, waiting.status), (Stage.DELIVERY, Status.WAITING_FOR_EXTERNAL))
        observed = decide(waiting, Event.PR_OBSERVED, evidence=Evidence(pr=READY, pr_ready=True))
        self.assertEqual((observed.status, observed.primary_pr, observed.ready_revision),
                         (Status.WAITING_FOR_EXTERNAL, READY.url, REV))
        stale = decide(observed, Event.PR_OBSERVED, evidence=Evidence(pr=READY))
        self.assertIsNone(stale.ready_revision)
        with self.assertRaises(ValueError):
            decide(observed, Event.PR_OBSERVED, evidence=Evidence(pr=replace(READY, url="https://example.com/pull/2")))
        merged = replace(READY, revision=Revision("squashed-head", "base"), merge_commit="merge", closed=True)
        done = decide(observed, Event.PR_OBSERVED, evidence=Evidence(pr=merged))
        self.assertEqual((done.status, done.final_output, done.candidate),
                         (Status.COMPLETED, merged, "squashed-head"))
        with self.assertRaises(ValueError):
            decide(ready(), Event.PR_OBSERVED, evidence=Evidence(pr=merged))

    def test_cancel_reconciles_in_flight_merge(self):
        state = result(ready(Target.MERGED), replace(GATES, merge_authorized=True))
        state = decide(decide(state, Event.CLAIM), Event.CANCEL)
        self.assertEqual(state.stopping, Status.CANCELLED)
        with self.assertRaises(ValueError):
            decide(state, Event.RECHECK)
        merged = replace(READY, merge_commit="merged")
        state = decide(state, Event.MERGE_VERIFIED, evidence=Evidence(pr=merged))
        self.assertEqual((state.status, state.final_output), (Status.COMPLETED, merged))

    def test_retry_interrupted_draft_creation_and_reuse_matching_pr(self):
        state = result(start(Target.READY), Evidence(candidate=REV.head))
        running = decide(state, Event.CLAIM)
        state = decide(running, Event.RETRY, retry_allowed=True)
        self.assertEqual((state.stage, state.status), (Stage.PR_CREATION, Status.QUEUED))
        with self.assertRaises(ValueError):
            decide(state, Event.RESULT, outcome=Outcome.SUCCEEDED, evidence=Evidence(pr=DRAFT))
        self.assertEqual(result(state, Evidence(pr=DRAFT)).primary_pr, DRAFT.url)

    def test_publication_blocker_adopts_the_existing_pr(self):
        state = result(start(Target.READY), Evidence(candidate=REV.head))
        foreign = replace(DRAFT, revision=Revision("outside", REV.base))
        for pr, findings, stage in [(DRAFT, None, Stage.SELF_REVIEW),
                                    (foreign, "integrate outside", Stage.DEVELOPMENT)]:
            with self.subTest(pr=pr):
                adopted = result(state, Evidence(pr=pr, findings=findings), Outcome.CHANGES_REQUIRED)
                self.assertEqual((adopted.stage, adopted.primary_pr, adopted.candidate),
                                 (stage, pr.url, REV.head))
        closed = result(state, Evidence(pr=replace(DRAFT, closed=True)), Outcome.CHANGES_REQUIRED,
                        wait=Wait("PR closed", "owner reply"))
        self.assertEqual((closed.status, closed.primary_pr), (Status.WAITING_FOR_INPUT, DRAFT.url))
        with self.assertRaises(ValueError):
            result(state, Evidence(pr=foreign), Outcome.CHANGES_REQUIRED)

    def test_exhausted_retry_waits_for_a_reply_at_the_same_stage(self):
        state = decide(start(Target.REPORT), Event.CLAIM)
        state = decide(state, Event.RETRY, wait=Wait("Recovery exhausted", "owner reply"))
        self.assertEqual(state.status, Status.WAITING_FOR_INPUT)
        state = decide(state, Event.REPLY)
        self.assertEqual((state.stage, state.status), (Stage.REPORT_DRAFTING, Status.QUEUED))

    def test_report_review_loop_and_storage_retry(self):
        state = result(start(Target.REPORT), Evidence(report_draft="v1"))
        state = result(state, Evidence(report_draft="v1"), Outcome.CHANGES_REQUIRED)
        state = result(state, Evidence(report_draft="v2"))
        with self.assertRaises(ValueError):
            result(state, Evidence(reviewed_report="v1"))
        state = result(state, Evidence(reviewed_report="v2"))
        state = result(state, outcome=Outcome.FAILED, retry_allowed=True)
        self.assertEqual(state.stage, Stage.REPORT_STORAGE)
        with self.assertRaises(ValueError):
            result(state, Evidence(stored_report=StoredReport("stored", "v1")))
        report = StoredReport("stored", "v2")
        state = result(state, Evidence(stored_report=report))
        self.assertEqual((state.status, state.final_output), (Status.COMPLETED, report))
        with self.assertRaises(ValueError):
            decide(state, Event.CLAIM)

    def test_unclaimed_cancel_waits_for_disposition_storage(self):
        state = decide(start(Target.READY), Event.CANCEL)
        self.assertEqual((state.status, state.final_output), (Status.WAITING_FOR_EXTERNAL, None))
        self.assertEqual(decide(state, Event.FINALIZE), state)
        with self.assertRaises(ValueError):
            decide(state, Event.RECHECK)
        report = StoredReport("disposition")
        state = decide(state, Event.FINALIZE, evidence=Evidence(stored_report=report))
        self.assertEqual((state.status, state.final_output), (Status.CANCELLED, report))

    def test_running_failure_requires_reconciliation_and_stored_report(self):
        state = decide(start(Target.READY), Event.CLAIM)
        state = decide(state, Event.CLOSE_FAILURE)
        self.assertEqual(state.stopping, Status.FAILED)
        with self.assertRaises(ValueError):
            decide(state, Event.FINALIZE, evidence=Evidence(stored_report=StoredReport("report")))
        report = StoredReport("report")
        state = decide(state, Event.STOP_CONFIRMED, evidence=Evidence(stored_report=report))
        self.assertEqual((state.status, state.final_output), (Status.FAILED, report))

    def test_open_or_closed_state_cannot_violate_final_output_invariant(self):
        state = start(Target.REPORT)
        with self.assertRaises(ValueError):
            replace(state, status=Status.COMPLETED)
        with self.assertRaises(ValueError):
            replace(state, final_output=StoredReport("premature"))


if __name__ == "__main__":
    unittest.main()

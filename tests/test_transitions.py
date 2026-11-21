import unittest
from dataclasses import replace

from phalanx.transitions import (
    Bypass, Event, Evidence, Issue, Outcome, PullRequest, Revision, Stage,
    Status, StoredReport, Target, Wait, decide, start,
)


REV = Revision("candidate", "base")
DRAFT = PullRequest("https://example.com/pull/1", REV, draft=True)
READY = replace(DRAFT, draft=False)
GATES = Evidence(pr=READY, ci_and_previews=REV, independent_review=REV,
                 actionable_feedback_cleared=REV, approvals_and_mergeability=REV)
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
    state = result(promotion(target), Evidence(pr=READY, draft_checks=REV))
    state = result(state, Evidence(pr=READY, ci_and_previews=REV))
    state = result(state, Evidence(pr=READY, independent_review=REV))
    return result(state, GATES)


class TransitionTests(unittest.TestCase):
    def test_draft_first_and_ready_only(self):
        state = result(start(Target.READY), Evidence(candidate=REV.head))
        self.assertEqual((state.stage, state.status), (Stage.PR_CREATION, Status.QUEUED))
        with self.assertRaises(ValueError):
            result(state, Evidence(pr=READY))
        self.assertEqual(result(state, Evidence(pr=DRAFT)).primary_pr, DRAFT.url)
        state = ready()
        self.assertEqual(state.stage, Stage.READY_FOR_CHECK_IN)
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

    def test_new_blocker_and_conflict_fix_preserve_primary(self):
        state = result(ready(), replace(GATES, issue=Issue.PR_BLOCKER))
        self.assertEqual(state.stage, Stage.PR_RECONCILIATION)
        state = result(state, Evidence(pr=READY, candidate="fixed"))
        self.assertEqual((state.stage, state.candidate, state.primary_pr),
                         (Stage.SELF_REVIEW, "fixed", READY.url))
        with self.assertRaises(ValueError):
            result(state, Evidence(reviewed_candidate=REV.head))
        state = result(state, Evidence(candidate="fixed"), Outcome.CHANGES_REQUIRED)
        state = result(state, Evidence(candidate="fixed-again"))
        self.assertEqual(state.stage, Stage.SELF_REVIEW)

    def test_new_head_returns_to_reconciliation_and_new_base_invalidates_gates(self):
        for revision, next_stage in [
            (Revision("outside", REV.base), Stage.PR_RECONCILIATION),
            (Revision(REV.head, "new-base"), Stage.PR_RECONCILIATION),
        ]:
            with self.subTest(revision=revision):
                observation = replace(GATES, pr=replace(READY, revision=revision))
                state = result(ready(), observation)
                self.assertEqual(state.stage, next_stage)
                if revision.head == REV.head:
                    self.assertEqual(result(state, observation).stage, Stage.PR_VALIDATION)

    def test_authorization_wait_and_ambiguous_merge_recovery(self):
        state = result(ready(Target.MERGED), GATES, wait=Wait("Authorize check-in", "reply"))
        self.assertEqual(state.status, Status.WAITING_FOR_INPUT)
        state = decide(state, Event.REPLY)
        state = result(state, replace(GATES, merge_authorized=True))
        self.assertEqual(state.stage, Stage.CHECK_IN)
        state = decide(decide(state, Event.CLAIM), Event.EXTERNAL_WAIT, wait=WAIT)
        merged = replace(READY, merge_commit="merged", closed=True)
        with self.assertRaises(ValueError):
            decide(state, Event.MERGE_VERIFIED,
                   evidence=Evidence(pr=replace(merged, revision=Revision(REV.head, "other"))))
        state = decide(state, Event.MERGE_VERIFIED, evidence=Evidence(pr=merged))
        self.assertEqual((state.status, state.final_output), (Status.COMPLETED, merged))

    def test_explicit_ready_only_disposition_rechecks_gates(self):
        state = result(ready(Target.MERGED), GATES, wait=Wait("Authorize check-in", "reply"))
        state = decide(state, Event.ACCEPT_READY_ONLY)
        self.assertEqual(state.target, Target.READY)
        self.assertEqual(result(state, Evidence(pr=READY)).stage, Stage.PR_RECONCILIATION)
        self.assertEqual(result(state, GATES).final_output, READY)

    def test_closed_pr_requires_a_decision(self):
        state = result(ready(), Evidence(pr=replace(READY, closed=True)),
                       wait=Wait("PR closed without merge", "owner reply"))
        self.assertEqual((state.status, state.primary_pr), (Status.WAITING_FOR_INPUT, READY.url))

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

    def test_publication_conflict_preserves_discovered_matching_pr(self):
        state = result(start(Target.READY), Evidence(candidate=REV.head))
        for pr, reconciliation in [
            (READY, Evidence(pr=READY)),
            (replace(DRAFT, revision=Revision("outside", REV.base)),
             Evidence(pr=replace(DRAFT, revision=Revision("outside", REV.base)),
                      candidate="outside")),
        ]:
            with self.subTest(pr=pr):
                conflict = Evidence(pr=pr, issue=Issue.PR_BLOCKER,
                                    matching_publication_pr=pr.url)
                discovered = result(state, conflict, Outcome.CHANGES_REQUIRED)
                self.assertEqual((discovered.stage, discovered.primary_pr, discovered.candidate),
                                 (Stage.PR_RECONCILIATION, pr.url, REV.head))
                recovered = result(discovered, reconciliation)
                self.assertEqual((recovered.stage, recovered.primary_pr),
                                 (Stage.SELF_REVIEW, pr.url))

    def test_publication_conflict_rejects_unrelated_or_unverified_pr(self):
        state = result(start(Target.READY), Evidence(candidate=REV.head))
        unrelated = replace(READY, url="https://example.com/pull/2")
        for matched_url in [None, READY.url]:
            with self.subTest(matched_url=matched_url), self.assertRaises(ValueError):
                result(state, Evidence(pr=unrelated, issue=Issue.PR_BLOCKER,
                                       matching_publication_pr=matched_url),
                       Outcome.CHANGES_REQUIRED)

    def test_feedback_must_be_cleared_for_current_revision(self):
        ready_state = ready(Target.MERGED)
        reconciliation = result(ready_state, replace(GATES, issue=Issue.PR_BLOCKER))
        check_in = result(ready_state, replace(GATES, merge_authorized=True))
        for clearance in [None, Revision(REV.head, "old-base"), Revision("old-head", REV.base)]:
            with self.subTest(clearance=clearance):
                evidence = replace(GATES, actionable_feedback_cleared=clearance,
                                   merge_authorized=True)
                blocked = result(reconciliation, evidence,
                                 wait=Wait("Resolve actionable feedback", "owner reply"))
                self.assertEqual((blocked.stage, blocked.status),
                                 (Stage.PR_RECONCILIATION, Status.WAITING_FOR_INPUT))
                for state in [ready_state, check_in]:
                    deferred = result(state, evidence)
                    self.assertEqual(deferred.stage, Stage.PR_RECONCILIATION)
                    self.assertIsNone(deferred.final_output)

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

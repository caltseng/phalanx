import unittest
from dataclasses import replace

from phalanx.transitions import (
    Event, Evidence, Outcome, PublicationMode, PullRequest, Revision, Stage,
    Status, StoredReport, Target, Wait, decide, start,
)


REV = Revision("candidate", "base")
DRAFT = PullRequest("https://example.com/pull/1", REV, draft=True)
READY = replace(DRAFT, draft=False)
WAIT = Wait("Await verification", "scheduled recheck")


def result(state, evidence=Evidence(), outcome=Outcome.SUCCEEDED, **kwargs):
    if state.status is Status.QUEUED:
        state = decide(state, Event.CLAIM)
    return decide(state, Event.RESULT, outcome=outcome, evidence=evidence, **kwargs)


class TransitionTests(unittest.TestCase):
    def test_delivery_owner_question_retains_scope_and_cannot_turn_a_miss_into_success(self):
        state = result(start(Target.MERGED), Evidence(candidate=REV.head))
        state = result(state, Evidence(reviewed_candidate=REV.head))
        state = result(state, Evidence(published_pr=READY.url))
        wait = Wait("Owner: explicit memory contract missed; choose optimization or a revised contract",
                    "owner reply")
        asked = decide(state, Event.RESULT, outcome=Outcome.NEEDS_INPUT, wait=wait)
        self.assertEqual(asked, replace(state, status=Status.WAITING_FOR_INPUT, wait=wait))
        self.assertEqual(decide(asked, Event.PR_OBSERVED, evidence=Evidence(pr=READY, pr_ready=True)), asked)
        with self.assertRaises(ValueError):
            decide(asked, Event.RESULT, outcome=Outcome.NEEDS_INPUT, wait=wait)
        with self.assertRaises(ValueError):
            decide(state, Event.RESULT, outcome=Outcome.SUCCEEDED)
        stopping = decide(state, Event.CANCEL)
        with self.assertRaises(ValueError):
            decide(stopping, Event.RESULT, outcome=Outcome.NEEDS_INPUT, wait=wait)
        resumed = decide(asked, Event.REPLY)
        self.assertEqual((resumed.stage, resumed.status, resumed.target, resumed.candidate),
                         (Stage.DELIVERY, Status.QUEUED, Target.MERGED, REV.head))
        self.assertEqual(result(resumed), state)

    def test_draft_delivery_preserves_the_reviewed_head_across_close_and_reopen(self):
        state = start(Target.MERGED, PublicationMode.DRAFT)
        state = result(state, Evidence(candidate=REV.head))
        state = result(state, Evidence(reviewed_candidate=REV.head))
        state = result(state, Evidence(published_pr=DRAFT.url))
        closed = replace(DRAFT, closed=True, revision=Revision("foreign", REV.base))
        state = decide(state, Event.PR_OBSERVED, evidence=Evidence(pr=closed))
        self.assertEqual((state.status, state.candidate), (Status.WAITING_FOR_INPUT, REV.head))
        state = result(decide(state, Event.REPLY))
        moved = decide(state, Event.PR_OBSERVED, evidence=Evidence(pr=replace(closed, closed=False)))
        self.assertEqual((moved.stage, moved.status, moved.candidate, moved.ready_revision),
                         (Stage.SELF_REVIEW, Status.QUEUED, "foreign", None))
        wrong = decide(state, Event.PR_OBSERVED, evidence=Evidence(pr=replace(DRAFT, draft=False)))
        self.assertEqual(wrong.status, Status.WAITING_FOR_EXTERNAL)
        self.assertIsNone(wrong.ready_revision)
        done = decide(wrong, Event.PR_OBSERVED, evidence=Evidence(pr=DRAFT))
        self.assertEqual((done.status, done.final_output), (Status.COMPLETED, DRAFT))
        self.assertIsNone(done.ready_revision)
        # A real merge remains a merge, never mislabeled a successful draft publication.
        merged = replace(READY, closed=True, merge_commit="merge", revision=Revision("new-head", REV.base))
        done = decide(state, Event.PR_OBSERVED, evidence=Evidence(pr=merged))
        self.assertEqual((done.status, done.candidate, done.final_output.merge_commit),
                         (Status.COMPLETED, "new-head", "merge"))


    def test_writer_publishes_and_github_observations_deliver(self):
        """PR results follow one direct lifecycle; review rework returns through development."""
        state = result(start(Target.MERGED), Evidence(candidate=REV.head))
        self.assertEqual(state.stage, Stage.SELF_REVIEW)
        rework = result(state, Evidence(candidate=REV.head, findings="Fix"), Outcome.CHANGES_REQUIRED)
        self.assertEqual(rework.stage, Stage.DEVELOPMENT)
        state = result(state, Evidence(reviewed_candidate=REV.head))
        self.assertEqual(state.stage, Stage.PUBLICATION)
        running = decide(state, Event.CLAIM)
        with self.assertRaises(ValueError):
            decide(running, Event.PR_OBSERVED, evidence=Evidence(pr=READY, pr_ready=True))
        with self.assertRaises(ValueError):
            decide(running, Event.RESULT, outcome=Outcome.CHANGES_REQUIRED, evidence=Evidence(findings="x"))
        with self.assertRaises(ValueError):
            decide(running, Event.RESULT, outcome=Outcome.SUCCEEDED)
        waiting = decide(running, Event.RESULT, outcome=Outcome.SUCCEEDED,
                         evidence=Evidence(published_pr=READY.url))
        self.assertEqual((waiting.stage, waiting.status, waiting.primary_pr),
                         (Stage.DELIVERY, Status.WAITING_FOR_EXTERNAL, READY.url))
        observed = decide(waiting, Event.PR_OBSERVED, evidence=Evidence(pr=READY, pr_ready=True))
        self.assertEqual((observed.status, observed.ready_revision), (Status.WAITING_FOR_EXTERNAL, REV))
        stale = decide(observed, Event.PR_OBSERVED, evidence=Evidence(pr=READY))
        self.assertIsNone(stale.ready_revision)
        with self.assertRaises(ValueError):
            decide(observed, Event.PR_OBSERVED, evidence=Evidence(pr=replace(READY, url="https://example.com/pull/2")))
        retargeted = decide(observed, Event.PR_OBSERVED, evidence=Evidence(pr=READY, pr_ready=True, off_base=True))
        self.assertIsNone(retargeted.ready_revision)
        merged = replace(READY, revision=Revision("squashed-head", "base"), merge_commit="merge", closed=True,
                         merge_method="squash_or_rebase", merged_at="2026-11-24T06:33:57+00:00")
        elsewhere = decide(observed, Event.PR_OBSERVED, evidence=Evidence(pr=merged, off_base=True))
        self.assertEqual((elsewhere.status, elsewhere.final_output), (Status.WAITING_FOR_INPUT, None))
        done = decide(observed, Event.PR_OBSERVED, evidence=Evidence(pr=merged))
        self.assertEqual((done.status, done.stage, done.final_output, done.candidate),
                         (Status.COMPLETED, Stage.DELIVERY, merged, "squashed-head"))
        with self.assertRaises(ValueError):
            decide(start(Target.READY), Event.PR_OBSERVED, evidence=Evidence(pr=merged))


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
            decide(state, Event.CLAIM)
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

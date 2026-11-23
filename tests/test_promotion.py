from dataclasses import replace
from unittest import TestCase
from unittest.mock import Mock

from phalanx.github import GitHubError, PullRequest, UncertainGitHubWrite
from phalanx.promotion import DraftChecks, promote
from phalanx.transitions import Revision
from phalanx.validation import CheckKind, CheckOutcome, GateState, Phase, PublishedEvidence, RequiredCheck
from phalanx.workspaces import _item_branch


HEAD = "a" * 40
BASE = "b" * 40
MOVED = "c" * 40
URL = "https://github.com/example/repo/pull/12"
PULL = PullRequest(
    number=12, node_id="node", url=URL, state="open", draft=True, merged=False,
    head_sha=HEAD, head_ref=_item_branch("item"), head_owner="example",
    head_repository="example/repo", base_sha=BASE, base_ref="main",
    base_repository="example/repo", merge_commit_sha=None, mergeable=True,
)
READY = replace(PULL, draft=False)
GATE = RequiredCheck("draft preview", CheckKind.PREVIEW)
EVIDENCE = PublishedEvidence(
    URL, Revision(HEAD, BASE), HEAD, Phase.DRAFT, GATE.name, GATE.kind, CheckOutcome.PASSED,
)


class PromotionTest(TestCase):
    def setUp(self):
        self.github = Mock(repository="example/repo")
        self.github.get_pull_request.return_value = PULL
        self.collect = Mock(return_value=DraftChecks((GATE,), (EVIDENCE,)))
        self.claim_current = Mock(return_value=True)

    def promote(self):
        return promote(self.github, item_id="item", number=12, base_ref="main",
                       revision=Revision(HEAD, BASE), collect=self.collect,
                       claim_current=self.claim_current)

    def test_gate_pending_failed_or_unknown_avoids_promotion(self):
        for checks, gate in (
            (DraftChecks((GATE,)), GateState.WAITING),
            (DraftChecks((GATE,), (replace(EVIDENCE, tested_sha=MOVED),)), GateState.WAITING),
            (DraftChecks((GATE,), (replace(EVIDENCE, outcome=CheckOutcome.FAILED),)), GateState.FAILED),
            (DraftChecks(None), GateState.BLOCKED),
        ):
            with self.subTest(gate=gate):
                self.collect.return_value = checks
                result = self.promote()
                self.assertEqual(result.status, "gates_unresolved")
                self.assertEqual(result.gates.state, gate)
                self.github.mark_ready.assert_not_called()

    def test_lost_promotion_response_gets_verified_and_ready_is_not_replayed(self):
        self.github.get_pull_request.side_effect = [PULL, PULL, READY, READY, READY]
        self.github.mark_ready.side_effect = UncertainGitHubWrite("mark ready", "no response")
        self.assertEqual(self.promote().pull, READY)
        self.assertEqual(self.promote().status, "verified")
        self.github.mark_ready.assert_called_once_with(PULL.node_id)

    def test_outside_push_before_or_after_mark_ready_never_verifies(self):
        for observations, write_count in (
            ([PULL, replace(PULL, head_sha=MOVED)], 0),
            ([PULL, PULL, replace(READY, head_sha=MOVED)], 1),
        ):
            with self.subTest(write_count=write_count):
                self.github.mark_ready.reset_mock()
                self.github.get_pull_request.side_effect = observations
                self.assertEqual(self.promote().status, "revision_changed")
                self.assertEqual(self.github.mark_ready.call_count, write_count)

    def test_retarget_or_new_conflict_stops_promotion(self):
        for changed in (replace(PULL, base_ref="release"), replace(PULL, head_ref="unrelated"),
                        replace(PULL, mergeable=False)):
            with self.subTest(changed=changed):
                self.github.get_pull_request.return_value = changed
                self.assertEqual(self.promote().status, "revision_changed")
        self.collect.assert_not_called()
        self.github.mark_ready.assert_not_called()

    def test_expired_claim_during_gate_collection_avoids_write(self):
        def lose_claim(_):
            self.claim_current.return_value = False
            return DraftChecks(())

        self.collect.side_effect = lose_claim
        self.assertEqual(self.promote().status, "claim_lost")
        self.github.mark_ready.assert_not_called()

    def test_explicitly_unconfigured_gate_can_promote(self):
        self.collect.return_value = DraftChecks(())
        self.github.get_pull_request.side_effect = [PULL, PULL, READY]
        result = self.promote()
        self.assertEqual(result.status, "verified")
        self.assertEqual(result.gates.state, GateState.NOT_CONFIGURED)

    def test_failed_mutation_preserves_provider_reason_if_still_draft(self):
        self.github.mark_ready.side_effect = GitHubError("mark ready", "permission denied", 403)
        result = self.promote()
        self.assertEqual(result.status, "provider_pending")
        self.assertIn("permission denied", result.reason)
        self.github.mark_ready.assert_called_once()

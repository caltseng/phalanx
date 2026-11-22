import unittest
from dataclasses import replace

from phalanx.transitions import Revision
from phalanx.validation import (
    CheckKind,
    CheckOutcome,
    GateResult,
    GateState,
    Phase,
    PreCheck,
    PreEvidence,
    PreOutcome,
    PublishedCandidate,
    PublishedEvidence,
    RequiredCheck,
    evaluate_pre_validation,
    evaluate_published_checks,
)


class PreValidationTests(unittest.TestCase):
    def test_current_pass_and_policy_authorized_dispositions(self):
        requirements = (
            PreCheck("unit"),
            PreCheck("platform", allow_inapplicable=True),
            PreCheck("smoke", allow_skip=True),
        )
        observations = (
            PreEvidence("head", "unit", PreOutcome.PASSED),
            PreEvidence("head", "platform", PreOutcome.INAPPLICABLE, "Windows only", "repo policy"),
            PreEvidence("head", "smoke", PreOutcome.SKIPPED, "urgent fix", "dispatch and repo policy"),
        )
        self.assertEqual(evaluate_pre_validation("head", requirements, observations).state, GateState.SATISFIED)
        self.assertEqual(evaluate_pre_validation("new", requirements, observations).state, GateState.WAITING)

    def test_skip_needs_permission_reason_and_authorization(self):
        evidence = PreEvidence("head", "smoke", PreOutcome.SKIPPED, "urgent fix", "dispatch")
        for requirement, result in (
            (PreCheck("smoke"), evidence),
            (PreCheck("smoke", allow_skip=True), replace(evidence, reason=" ")),
            (PreCheck("smoke", allow_skip=True), replace(evidence, authorization="")),
        ):
            with self.subTest(requirement=requirement, result=result):
                self.assertEqual(evaluate_pre_validation("head", (requirement,), (result,)).state, GateState.BLOCKED)

    def test_failure_does_not_pass(self):
        requirement = (PreCheck("unit", allow_skip=True),)
        failure = PreEvidence("head", "unit", PreOutcome.FAILED)
        self.assertEqual(evaluate_pre_validation("head", requirement, (failure,)).state, GateState.FAILED)


class PublishedValidationTests(unittest.TestCase):
    def setUp(self):
        self.current = PublishedCandidate("https://example.test/pr/1", Revision("head", "base"), False, "head", "merge")
        self.check = RequiredCheck("build", CheckKind.CHECK_RUN, app_id=17)
        self.evidence = PublishedEvidence(
            self.current.pr_url, self.current.revision, "head", Phase.READY,
            "build", CheckKind.CHECK_RUN, CheckOutcome.PASSED, app_id=17,
        )

    def evaluate(self, required, *observations):
        return evaluate_published_checks(self.current, Phase.READY, required, observations)

    def test_empty_unknown_missing_pending_and_failure_are_distinct(self):
        self.assertEqual(self.evaluate(()).state, GateState.NOT_CONFIGURED)
        self.assertEqual(self.evaluate(None).state, GateState.BLOCKED)
        missing = self.evaluate((self.check,))
        pending = self.evaluate((self.check,), replace(self.evidence, outcome=CheckOutcome.PENDING))
        self.assertEqual(missing.state, GateState.WAITING)
        self.assertEqual(pending.state, GateState.WAITING)
        self.assertNotEqual(missing.reasons, pending.reasons)
        self.assertEqual(
            self.evaluate((self.check,), replace(self.evidence, outcome=CheckOutcome.FAILED)).state,
            GateState.FAILED,
        )

    def test_matching_requires_name_app_phase_pr_head_base_and_tested_sha(self):
        self.assertEqual(self.evaluate((self.check,), self.evidence).state, GateState.SATISFIED)
        for fields in (
            {"name": "lint"}, {"app_id": 18}, {"app_id": None}, {"phase": Phase.DRAFT},
            {"pr_url": "https://example.test/pr/2"}, {"revision": Revision("old", "base")},
            {"revision": Revision("head", "old")}, {"tested_sha": "old"},
        ):
            with self.subTest(fields=fields):
                self.assertEqual(self.evaluate((self.check,), replace(self.evidence, **fields)).state, GateState.WAITING)

    def test_unexpected_pr_head_blocks_even_with_no_checks_configured(self):
        current = replace(self.current, candidate="expected")
        result = evaluate_published_checks(current, Phase.READY, (), ())
        self.assertEqual(result.state, GateState.BLOCKED)

    def test_merge_candidate_does_not_fall_back_to_head(self):
        requirement = (replace(self.check, use_merge_sha=True),)
        merge_result = replace(self.evidence, tested_sha="merge")
        self.assertEqual(self.evaluate(requirement, self.evidence).state, GateState.WAITING)
        self.assertEqual(self.evaluate(requirement, merge_result).state, GateState.SATISFIED)
        for current in (
            replace(self.current, merge_sha=None),
            replace(self.current, merge_sha="new"),
            replace(self.current, revision=Revision("head", "new-base")),
        ):
            with self.subTest(current=current):
                self.assertEqual(
                    evaluate_published_checks(current, Phase.READY, requirement, (merge_result,)).state,
                    GateState.WAITING,
                )

    def test_same_named_status_and_run_each_need_to_pass(self):
        status = RequiredCheck("build", CheckKind.STATUS)
        status_result = replace(self.evidence, kind=CheckKind.STATUS, app_id=None)
        requirements = (self.check, status)
        self.assertEqual(self.evaluate(requirements, self.evidence).state, GateState.WAITING)
        self.assertEqual(self.evaluate(requirements, self.evidence, status_result).state, GateState.SATISFIED)
        self.assertEqual(
            self.evaluate(requirements, self.evidence, replace(status_result, outcome=CheckOutcome.FAILED)).state,
            GateState.FAILED,
        )

    def test_neutral_and_skipped_follow_check_policy(self):
        for outcome in (CheckOutcome.NEUTRAL, CheckOutcome.SKIPPED):
            with self.subTest(outcome=outcome):
                result = replace(self.evidence, outcome=outcome)
                self.assertEqual(self.evaluate((self.check,), result).state, GateState.SATISFIED)
                self.assertEqual(
                    self.evaluate((replace(self.check, require_execution=True),), result).state,
                    GateState.FAILED,
                )

    def test_draft_preview_is_separate_from_ready_ci(self):
        draft = replace(self.current, draft=True)
        requirement = (RequiredCheck("smoke", CheckKind.PREVIEW),)
        preview = replace(self.evidence, phase=Phase.DRAFT, name="smoke", kind=CheckKind.PREVIEW)
        self.assertEqual(
            evaluate_published_checks(draft, Phase.DRAFT, requirement, (preview,)).state, GateState.SATISFIED,
        )
        self.assertEqual(
            evaluate_published_checks(draft, Phase.READY, (self.check,), (self.evidence,)).state, GateState.WAITING,
        )
        self.assertEqual(
            evaluate_published_checks(self.current, Phase.DRAFT, requirement, (preview,)).state, GateState.SATISFIED,
        )
        self.assertEqual(
            evaluate_published_checks(self.current, Phase.DRAFT, requirement, ()),
            GateResult(GateState.SATISFIED, skipped=("smoke (preview)",)),
        )
        self.assertEqual(self.evaluate((self.check,), preview).state, GateState.WAITING)

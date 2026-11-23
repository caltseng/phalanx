from dataclasses import asdict
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from phalanx.candidate_local import CandidateLocal
from phalanx.local_checks import LocalCheck
from phalanx.github import PullRequest
from phalanx.github_checks import collect_github_checks
from phalanx.intake_models import ExternalWait, Result
from phalanx.validation_policy import PublishedValidationPolicy
from phalanx.transitions import Revision
from phalanx.validation import (
    CheckKind, GateResult, GateState, Phase, PreOutcome, PublishedCandidate, RequiredCheck,
    evaluate_published_checks,
)
from phalanx.validation_policy import ValidationPolicyError, load_validation_policy
from phalanx.validation_stages import _pre_validation, run
from phalanx.workspaces import any_file_lock


class ValidationPolicyTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name).resolve() / "validation.json"
        self.repo = {"local_checks": [], "draft_checks": [], "ready_checks": []}

    def load(self, repo=None):
        self.path.write_text(json.dumps({
            "version": 1, "repositories": {"repo": self.repo if repo is None else repo},
        }), encoding="utf-8")
        return load_validation_policy(self.path, "repo")

    @staticmethod
    def command(**changes):
        return {"name": "tests", "argv": [sys.executable, "-I", "-c", "pass"], **changes}

    @staticmethod
    def bypass(**changes):
        return {"name": "platform", "allow_skip": True, "bypass": {
            "outcome": "skipped", "reason": "Urgent scoped change",
            "authorization": "Operator policy section 3",
        }, **changes}

    def test_loads_existing_consumer_types_and_exact_revision_requirements(self):
        self.repo.update(
            local_checks=[self.command(timeout_seconds=120), self.bypass()],
            draft_checks=[{"name": "draft-build", "kind": "check_run", "app_id": 42,
                           "use_merge_sha": True, "require_execution": True}],
            ready_checks=[{"name": "deploy-smoke", "kind": "preview", "require_execution": True}],
        )
        policy = self.load()
        self.assertIsInstance(policy.local_checks[0], LocalCheck)
        self.assertEqual(policy.local_checks[0].argv, (sys.executable, "-I", "-c", "pass"))
        self.assertEqual(policy.local_checks[0].timeout_seconds, 120)
        self.assertEqual(policy.draft_checks, (
            RequiredCheck("draft-build", CheckKind.CHECK_RUN, 42, True, True),
        ))
        self.assertIsInstance(policy.ready, PublishedValidationPolicy)
        self.assertEqual(asdict(policy.ready.required[0]), {
            "name": "deploy-smoke", "kind": CheckKind.PREVIEW, "app_id": None,
            "use_merge_sha": False, "require_execution": True,
        })
        self.assertIsNone(policy.ready.read_preview)
        bypass = policy.local_checks[1]
        self.assertEqual((bypass.disposition, bypass.reason, bypass.authorization),
                         (PreOutcome.SKIPPED, "Urgent scoped change", "Operator policy section 3"))

    def test_unavailable_policy_never_becomes_explicit_empty(self):
        empty = self.load()
        self.assertEqual((empty.local_checks, empty.draft_checks, empty.ready.required), ((), (), ()))
        unavailable = self.load(dict.fromkeys(self.repo))
        self.assertEqual((unavailable.local_checks, unavailable.draft_checks,
                          unavailable.ready.required), (None, None, None))
        with self.assertRaisesRegex(ValidationPolicyError, "No validation policy"):
            load_validation_policy(self.path, "unregistered")
        self.path.unlink()
        with self.assertRaisesRegex(ValidationPolicyError, "Repair the trusted"):
            load_validation_policy(self.path, "repo")
        with self.assertRaisesRegex(ValidationPolicyError, "absolute"):
            load_validation_policy(Path("validation.json"), "repo")
        with self.assertRaises(ValidationPolicyError):
            self.load({"local_checks": [], "ready_checks": []})

    def test_invalid_types_and_unknown_settings_fail_closed(self):
        invalid = [
            {"local_checks": [self.command(timeout_seconds=value)]}
            for value in (True, False, "60", 1.5, 0, -1, 86401)
        ] + [
            {"local_checks": [self.command(allow_skip=value)]} for value in (1, "true", None)
        ] + [
            {"local_checks": [self.command(argv=value)]}
            for value in ([], "python", [True], ["relative.exe"], [sys.executable, "\x00"])
        ] + [
            {"local_checks": [self.command(name=value)]} for value in ("", " tests", "tests ", True)
        ] + [
            {"ready_checks": [{"name": "ci", "kind": "check_run", "app_id": value}]}
            for value in (True, 0, "42", 1.5)
        ] + [
            {"ready_checks": [{"name": "ci", "kind": "check_run", "use_merge_sha": 1}]},
            {"draft_checks": [{"name": "ci", "kind": "check_run", "require_execution": "true"}]},
            {"ready_checks": [{"name": "ci", "kind": "unknown"}]},
            {"ready_checks": [{"name": "legacy", "kind": "status", "app_id": 42}]},
            {"ready_checks": [{"name": "preview", "kind": "preview", "url": "https://example.test"}]},
            {"preview": {"reader": "checkout.py"}},
            {"draft_fallback": "skip"},
            {"local_checks": [self.command(shell=True)]},
        ]
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(ValidationPolicyError):
                self.load({**self.repo, **changes})

    def test_bypass_requires_matching_per_check_permission_reason_and_authority(self):
        for bypass in (
            self.bypass(allow_skip=False),
            self.bypass(argv=[sys.executable]),
            self.bypass(argv=None),
            self.bypass(timeout_seconds=60),
            {"name": "platform", "bypass": {
                "outcome": "skipped", "reason": "Urgency", "authorization": "Owner",
            }},
            {"name": "platform", "allow_skip": True, "bypass": {
                "outcome": "inapplicable", "reason": "Different platform", "authorization": "Owner",
            }},
            {"name": "platform", "allow_skip": True, "bypass": {
                "outcome": "passed", "reason": "Urgency", "authorization": "Owner",
            }},
        ):
            with self.subTest(bypass=bypass), self.assertRaises(ValidationPolicyError):
                self.load({**self.repo, "local_checks": [bypass]})
        for field in ("reason", "authorization"):
            for value in ("", " ", None, True):
                bypass = self.bypass()
                bypass["bypass"][field] = value
                with self.subTest(field=field, value=value), self.assertRaises(ValidationPolicyError):
                    self.load({**self.repo, "local_checks": [bypass]})
        inapplicable = self.bypass(allow_skip=False, allow_inapplicable=True)
        inapplicable["bypass"]["outcome"] = "inapplicable"
        self.assertEqual(self.load({**self.repo, "local_checks": [inapplicable]}).local_checks[0].disposition,
                         PreOutcome.INAPPLICABLE)

    def test_duplicate_and_overlapping_identities_rejected_but_distinct_sources_retained(self):
        run = {"name": "ci", "kind": "check_run", "app_id": 42}
        for changes in (
            {"local_checks": [self.command(), self.command()]},
            {"draft_checks": [run, {**run, "use_merge_sha": True}]},
            {"ready_checks": [run, {**run, "app_id": None}]},
            {"ready_checks": [{**run, "app_id": None}, run]},
            {"ready_checks": [{**run, "name": " ci"}]},
            {"local_checks": [self.command(name=f"test-{n}") for n in range(51)]},
            {"ready_checks": [{**run, "name": f"ci-{n}"} for n in range(101)]},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationPolicyError):
                self.load({**self.repo, **changes})
        policy = self.load({**self.repo, "ready_checks": [
            run, {**run, "app_id": 43}, {"name": "ci", "kind": "status"},
        ]})
        self.assertEqual(len(policy.ready.required), 3)

    def test_whole_file_is_strict_and_other_repository_cannot_hide_invalid_policy(self):
        for raw in (
            '{"version":true,"repositories":{}}',
            '{"version":2,"repositories":{}}',
            '{"version":1,"repositories":{},"extra":1}',
            '{"version":1,"version":1,"repositories":{}}',
            '{"version":1,"repositories":{"repo":{},"repo":{}}}',
            '{"version":NaN,"repositories":{}}',
            '{"version":Infinity,"repositories":{}}',
            '{"version":1,"repositories":{"not/a/registered/id":{}}}',
            '[]', '{', '\ufeff{}',
            json.dumps({"version": 1, "repositories": {"repo": self.repo, "other": {
                **self.repo, "local_checks": [self.command(timeout_seconds=True)],
            }}}),
        ):
            self.path.write_text(raw, encoding="utf-8")
            with self.subTest(raw=raw), self.assertRaises(ValidationPolicyError):
                load_validation_policy(self.path, "repo")
        self.path.write_bytes(b"\xff")
        with self.assertRaises(ValidationPolicyError):
            load_validation_policy(self.path, "repo")
        self.path.write_bytes(b" " * (1024 * 1024 + 1))
        with self.assertRaisesRegex(ValidationPolicyError, "1 MiB"):
            load_validation_policy(self.path, "repo")

    def test_required_preview_without_adapter_blocks_and_ready_pr_skips_missing_draft_evidence(self):
        self.repo.update(draft_checks=[{"name": "draft", "kind": "check_run"}],
                         ready_checks=[{"name": "smoke", "kind": "preview"}])
        policy = self.load()
        pull = PullRequest(7, "node", "https://example.test/pr/7", "open", False, False,
                           "a" * 40, "topic", "owner", "owner/repo", "b" * 40,
                           "main", "owner/repo", None, True)
        github = Mock()
        github.get_pull_request.return_value = pull
        github.get_branch_tip.return_value = pull.base_sha
        checks = collect_github_checks(github, pull, Phase.READY, policy.ready.required,
                                       read_preview=policy.ready.read_preview)
        self.assertEqual(checks.gate.state, GateState.BLOCKED)
        self.assertIn("No preview reader", checks.gate.reasons[0])
        current = PublishedCandidate(pull.url, Revision(pull.head_sha, pull.base_sha),
                                     False, pull.head_sha)
        draft = evaluate_published_checks(current, Phase.DRAFT, policy.draft_checks, ())
        self.assertEqual(draft, GateResult(GateState.SATISFIED, skipped=("draft (check_run)",)))

    def test_loaded_bypass_flows_through_existing_candidate_result_without_weakening_ci(self):
        self.repo.update(local_checks=[self.bypass()],
                         ready_checks=[{"name": "ci", "kind": "check_run"}])
        policy = self.load()
        candidate = "a" * 40
        execution = {"id": "execution", "version": 3, "state": {
            "stage": "pre_pr_validation", "status": "running", "candidate": candidate,
        }}
        api = Mock()
        api.call.return_value = {"execution": execution}
        workspace = Mock()
        workspace.inspect.return_value = Mock(dirty=False, head=candidate)
        operation = CandidateLocal(api, workspace, {
            "item_id": "item", "attempt_id": "attempt", "execution": execution,
            "request": {"repository_id": "repo"},
        }, runner_id="desk", lease="secret")
        with patch("phalanx.candidate_local.run_check") as run:
            body = operation.pre_validate(policy.local_checks)
        run.assert_not_called()
        validated = Result.model_validate(body)
        self.assertEqual(validated.prevalidated_candidate, candidate)
        self.assertEqual(validated.local_checks[0].model_dump(mode="json"), {
            "name": "platform", "outcome": "skipped", "reason": "Urgent scoped change",
            "authorization": "Operator policy section 3",
        })
        current = PublishedCandidate("https://example.test/pr/1", Revision(candidate, "b" * 40),
                                     False, candidate)
        self.assertEqual(evaluate_published_checks(current, Phase.READY, policy.ready.required, ()).state,
                         GateState.WAITING)
        self.assertEqual(operation.pre_validate(None)["outcome"], "needs_input")
        self.assertEqual(operation.pre_validate(())["outcome"], "succeeded")

    def test_busy_local_check_capacity_waits_before_running_checks(self):
        self.repo.update(local_checks=[self.command()])
        policy = self.load()
        root = self.path.parent
        record = {}
        runner = SimpleNamespace(root=root, config={"local_check_capacity": 1},
                                 _ensure_reservation=Mock(), _send_submission=Mock(return_value="execution"),
                                 _transaction=lambda save: save(record, None))
        snapshot = {"lease": "secret", "claim": {"attempt_id": "attempt", "execution": {
            "version": 3, "state": {"stage": "pre_pr_validation", "candidate": "a" * 40},
        }}}
        slot = [root / "local-check-0.lock"]

        def validate(checks):
            with any_file_lock(slot) as held:
                self.assertIsNone(held)
            return {"outcome": "succeeded"}

        local = Mock()
        local.pre_validate.side_effect = validate
        with patch("phalanx.validation_stages._candidate", return_value=local), \
                patch("phalanx.validation_stages._policy", return_value=policy):
            with any_file_lock(slot) as held:
                self.assertEqual(held, slot[0])
                sent = run(runner, snapshot)
            local.pre_validate.assert_not_called()
            local._inspect.assert_not_called()
            self.assertEqual(sent["status"], "pre_pr_validation_submitted")
            waiting = runner._send_submission.call_args.args[1]
            self.assertIs(waiting, record["submission"])
            self.assertEqual(runner._send_submission.call_args.args[2], "pre_pr_validation:attempt:3")
            self.assertEqual(waiting["route"], "external-wait")
            self.assertIn("local check capacity", waiting["reason"])
            self.assertEqual(ExternalWait.model_validate(waiting["body"]).gate, "local_check_capacity")
            self.assertEqual(_pre_validation(runner, snapshot), {"body": {"outcome": "succeeded"}})
        local.pre_validate.assert_called_once_with(policy.local_checks)
        with any_file_lock(slot) as held:
            self.assertEqual(held, slot[0])

from dataclasses import asdict
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from phalanx.local_checks import LocalCheck
from phalanx.github import PullRequest
from phalanx.github_checks import collect_github_checks
from phalanx.validation_policy import PublishedValidationPolicy
from phalanx.transitions import Revision
from phalanx.validation import (
    CheckKind, GateResult, GateState, Phase, PreOutcome, PublishedCandidate, RequiredCheck,
    evaluate_published_checks,
)
from phalanx.validation_policy import ValidationPolicyError, load_validation_policy


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

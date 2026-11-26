from dataclasses import asdict
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from phalanx.local_checks import LocalCheck, run_check
from phalanx.validation import CheckKind, PreCheck, PreOutcome, RequiredCheck
from phalanx.validation_policy import (
    PublishedValidationPolicy, ValidationPolicyError, load_validation_policy,
)


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

    def test_loads_existing_consumer_types_and_exact_revision_requirements(self):
        self.repo.update(
            local_checks=[self.command(timeout_seconds=120)],
            draft_checks=[{"name": "draft-build", "kind": "check_run", "app_id": 42,
                           "use_merge_sha": True, "require_execution": True}],
            ready_checks=[{"name": "deploy-smoke", "kind": "check_run", "require_execution": True}],
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
            "name": "deploy-smoke", "kind": CheckKind.CHECK_RUN, "app_id": None,
            "use_merge_sha": False, "require_execution": True,
        })

    def test_local_check_returns_outcome_and_failure_reason(self):
        for script, outcome, reason in (
            ("pass", PreOutcome.PASSED, ""),
            ("import sys; print('broken'); sys.exit(3)", PreOutcome.FAILED,
             "Exited with status 3\n\nOutput tail:\nbroken"),
        ):
            with self.subTest(script=script):
                check = LocalCheck(PreCheck("tests"), (sys.executable, "-I", "-c", script))
                self.assertEqual(run_check(check, self.path.parent), (outcome, reason))

    def test_local_check_timeout_preserves_failure_detection_and_cleanup(self):
        check = LocalCheck(PreCheck("tests"), (sys.executable, "-I", "-c", "pass"))
        for output, detail in ((b"still running\n", ", no failure identified"),
                               (b"FAIL: test_example\n", " with failures")):
            process = Mock()
            process.wait.side_effect = subprocess.TimeoutExpired(check.argv, check.timeout_seconds)

            def launch(*args, **kwargs):
                kwargs["stdout"].write(output)
                return process

            with self.subTest(output=output), \
                    patch("phalanx.local_checks.subprocess.Popen", side_effect=launch), \
                    patch("phalanx.local_checks.kill_tree") as cleanup:
                outcome, reason = run_check(check, self.path.parent)
                self.assertEqual(outcome, PreOutcome.FAILED)
                self.assertEqual(reason, f"Timed out after 60 seconds{detail}\n\n"
                                 f"Output tail:\n{output.decode().strip()}")
                cleanup.assert_called_once_with(process)

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
            {"local_checks": [self.command(**{field: True})]}
            for field in ("allow_skip", "allow_inapplicable")
        ] + [
            {"local_checks": [self.command(argv=value)]}
            for value in (None, [], "python", [True], ["relative.exe"], [sys.executable, "\x00"])
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
            {"ready_checks": [{"name": "preview", "kind": "preview"}]},
            {"preview": {"reader": "checkout.py"}},
            {"draft_fallback": "skip"},
            {"local_checks": [self.command(shell=True)]},
            {"local_checks": [{"name": "tests"}]},
            {"local_checks": [self.command(bypass={
                "outcome": "skipped", "reason": "Urgency", "authorization": "Owner",
            })]},
        ]
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(ValidationPolicyError):
                self.load({**self.repo, **changes})

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

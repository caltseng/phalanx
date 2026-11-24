import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from phalanx.api_client import RunnerAPI
from phalanx.runner_errors import RunnerError
from phalanx.runner_preflight import preflight
from test_bootstrap import RUNNER, ServerFixture


class RunnerPreflightTests(ServerFixture, unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()

    def test_unreachable_api_and_old_git_are_actionable(self):
        with patch.object(RunnerAPI, "call", side_effect=RunnerError("API unavailable")), \
                self.assertRaisesRegex(RunnerError, "start the API server"):
            preflight({"base_url": "http://127.0.0.1:9"}, "token")
        old = subprocess.CompletedProcess(["git"], 0, stdout="git version 2.30.1\n")
        with patch("phalanx.runner_preflight.subprocess.run", return_value=old), \
                self.assertRaisesRegex(RunnerError, "Install Git 2.36 or later on PATH; found git version 2.30.1"):
            preflight({"base_url": "http://127.0.0.1:9"}, "token")

    def test_ready_runner_passes_against_live_api(self):
        self.start_server(self.directory / "server.sqlite3")
        preflight({"base_url": self.url}, RUNNER)
        with self.assertRaisesRegex(RunnerError, "HTTP 401 for /repositories; start the API server"):
            preflight({"base_url": self.url}, "wrong")

    def test_broken_validation_policy_blocks_before_claims(self):
        self.start_server(self.directory / "server.sqlite3")
        policy = self.directory / "policy.json"
        config = {"base_url": self.url, "validation_policy": str(policy),
                  "repositories": {"repo": {}, "other": {}}}
        policy.write_text(json.dumps({"version": 1, "repositories": {"repo": {
            "local_checks": [], "draft_checks": [], "ready_checks": None}}}), encoding="utf-8")
        preflight(config, RUNNER)
        policy.write_text("{", encoding="utf-8")
        with self.assertRaisesRegex(RunnerError, "Repair the trusted validation policy"):
            preflight(config, RUNNER)

    def test_check_timing_must_be_bounded_whole_numbers(self):
        self.start_server(self.directory / "server.sqlite3")
        preflight({"base_url": self.url, "check_recheck_seconds": 5, "check_wait_limit_seconds": 60,
                   "local_check_capacity": 2}, RUNNER)
        for key, value in (("check_recheck_seconds", 4), ("check_recheck_seconds", 86401),
                           ("check_recheck_seconds", 10.5), ("check_recheck_seconds", True),
                           ("check_recheck_seconds", "30"), ("check_wait_limit_seconds", 59),
                           ("check_wait_limit_seconds", None), ("local_check_capacity", 0),
                           ("local_check_capacity", 65), ("local_check_capacity", True)):
            with self.subTest(key=key, value=value), self.assertRaisesRegex(RunnerError, key):
                preflight({"base_url": self.url, key: value}, RUNNER)


if __name__ == "__main__":
    unittest.main()

import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from phalanx.__main__ import main
from phalanx.attended import RunnerAPI
from phalanx.runner_errors import RunnerError
from phalanx.runner_preflight import preflight
from test_bootstrap import RUNNER, ServerFixture


class RunnerPreflightTests(ServerFixture, unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.config = self.directory / "runner.json"
        self.config.write_text(json.dumps({"runner_id": "desk", "base_url": "http://127.0.0.1:9",
                                           "repositories": {}}), encoding="utf-8")

    def run_cli(self, *args, token=None):
        calls = []
        def call(api, route, *rest, **options):
            calls.append(route)
            raise RunnerError(f"Runner API response unavailable for {route}")
        environment = {key: value for key, value in os.environ.items() if key != "PHALANX_RUNNER_TOKEN"}
        if token:
            environment["PHALANX_RUNNER_TOKEN"] = token
        argv = ["phalanx", args[0], "--config", str(self.config), "--state", str(self.directory / "state"),
                *args[1:]]
        with (patch.dict(os.environ, environment, clear=True), patch.object(RunnerAPI, "call", call),
              patch.dict(sys.modules, {"pydantic": None}), patch("sys.argv", argv),
              patch("sys.stderr") as stderr, self.assertRaises(SystemExit) as stopped):
            for module in ("phalanx.draft_stage", "phalanx.pr_models"):
                sys.modules.pop(module, None)
            main()
        self.assertEqual(stopped.exception.code, 1)
        return "".join(call.args[0] for call in stderr.write.call_args_list), calls

    def test_missing_token_or_package_stops_captain_and_prepare_before_any_claim(self):
        for command in (["captain"], ["attended", "prepare"]):
            with self.subTest(command=command):
                message, calls = self.run_cli(*command)
                self.assertIn("Runner preflight failed", message)
                self.assertIn("Missing package pydantic", message)
                self.assertIn("Set PHALANX_RUNNER_TOKEN", message)
                self.assertEqual(calls, [])

    def test_unreachable_api_and_old_git_are_actionable(self):
        message, calls = self.run_cli("captain", token="token")
        self.assertIn("start the API server, check base_url http://127.0.0.1:9", message)
        self.assertEqual(calls, ["/repositories"])
        old = subprocess.CompletedProcess(["git"], 0, stdout="git version 2.30.1\n")
        with patch("phalanx.runner_preflight.subprocess.run", return_value=old), \
                self.assertRaisesRegex(RunnerError, "Install Git 2.36 or later on PATH; found git version 2.30.1"):
            preflight({"base_url": "http://127.0.0.1:9"}, "token")

    def test_ready_runner_passes_against_live_api(self):
        self.start_server(self.directory / "server.sqlite3")
        preflight({"base_url": self.url}, RUNNER)
        with self.assertRaisesRegex(RunnerError, "HTTP 401 for /repositories; start the API server"):
            preflight({"base_url": self.url}, "wrong")


if __name__ == "__main__":
    unittest.main()

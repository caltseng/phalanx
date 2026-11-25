from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from phalanx.store import migrate

from cli_runner import run_cli


class CLIRunnerTest(unittest.TestCase):
    def test_help_and_parser_errors_keep_separate_streams_and_exit_codes(self):
        help_result = run_cli(["--help"])
        self.assertEqual(help_result.returncode, 0)
        self.assertIn("usage: python -m phalanx", help_result.stdout)
        self.assertEqual(help_result.stderr, "")
        invalid = run_cli(["not-a-command"])
        self.assertEqual(invalid.returncode, 2)
        self.assertEqual(invalid.stdout, "")
        self.assertIn("invalid choice", invalid.stderr)

    def test_api_error_is_reported_without_leaking_environment_between_calls(self):
        argv, stdout, stderr = sys.argv, sys.stdout, sys.stderr
        environment = dict(os.environ)
        result = run_cli(["inspect", "item"], env={"PHALANX_OWNER_TOKEN": ""})
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "phalanx: PHALANX_OWNER_TOKEN is required for this command\n")
        self.assertEqual(dict(os.environ), environment)
        self.assertIs(sys.argv, argv)
        self.assertIs(sys.stdout, stdout)
        self.assertIs(sys.stderr, stderr)

    def test_relative_paths_use_temporary_cwd_and_restore_it_after_failure(self):
        cwd = Path.cwd()
        with TemporaryDirectory() as temporary:
            migrate(Path(temporary) / "queue.sqlite3")
            arguments = ["server", "backup", "backup.sqlite3"]
            environment = {"PHALANX_DB": "queue.sqlite3"}
            result = run_cli(arguments, env=environment, cwd=temporary)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((Path(temporary) / "backup.sqlite3").is_file())
            result = run_cli(arguments, env=environment, cwd=temporary)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stderr, "phalanx: backup.sqlite3\n")
            self.assertEqual(Path.cwd(), cwd)

    def test_unexpected_errors_propagate_and_restore_process_state(self):
        argv, cwd = sys.argv, Path.cwd()
        environment = dict(os.environ)
        output, errors = StringIO(), StringIO()
        with TemporaryDirectory() as temporary:
            with (redirect_stdout(output), redirect_stderr(errors),
                  patch("cli_runner.main", side_effect=RuntimeError("unexpected")),
                  self.assertRaisesRegex(RuntimeError, "unexpected")):
                run_cli([], env={"PHALANX_OWNER_TOKEN": "temporary"}, cwd=temporary)
            self.assertEqual(Path.cwd(), cwd)
        self.assertEqual(dict(os.environ), environment)
        self.assertIs(sys.argv, argv)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(errors.getvalue(), "")

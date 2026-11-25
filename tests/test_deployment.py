from argparse import Namespace
from contextlib import nullcontext, redirect_stdout
from datetime import timedelta
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import Mock, patch
from urllib.error import URLError
from urllib.request import Request, urlopen

from phalanx import deployment, shift
from phalanx.__main__ import main
from phalanx.muster import MusterError
from phalanx.workspaces import any_file_lock
from timing import WAIT_SECONDS


class DeploymentTest(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.enterContext(patch.dict(os.environ))
        for key in list(os.environ):
            if key.startswith("PHALANX_"):
                del os.environ[key]

    def install(self):
        home = deployment.initialize(self.root / "legacy-muster")
        (home / "private.env").write_text(
            (home / "private.env.example").read_text(encoding="utf-8")
            + "PHALANX_OWNER_TOKEN=" + "o" * 32 + "\n"
            + "PHALANX_RUNNER_TOKEN=" + "r" * 32 + "\n"
            + "PHALANX_GITHUB_TOKEN=example-test-credential\n", encoding="utf-8")
        return home

    def test_new_default_layout_and_no_live_state_or_credentials_created(self):
        with patch.object(deployment.Path, "home", return_value=self.root):
            home = deployment.initialize(port=8765)
        self.assertEqual(home, self.root / ".phalanx_muster")
        config = json.loads((home / "deployment.json").read_text())
        self.assertEqual(config["server"]["port"], 8765)
        self.assertEqual(Path(config["reconciler_state"]), home / "reconciler")
        self.assertFalse((home / "private.env").exists())
        self.assertFalse((home / "db").exists())
        self.assertFalse((home / "worktrees").exists())
        example = (home / "private.env.example").read_text()
        self.assertIn(str(home / "db" / "phalanx.sqlite3"), example)
        self.assertIn("PHALANX_API_URL=http://127.0.0.1:8765", example)

    def test_init_charter_is_accepted_after_configuring_repositories(self):
        home = self.root / "new-install"
        args = Namespace(deploy_command="init", home=home, api_url=None, port=8000)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(deployment.run_deploy(args), 0)
        path = home / "charter.toml"
        path.write_text(path.read_text(encoding="utf-8").replace(
            "repositories = []", 'repositories = ["repo"]'), encoding="utf-8")
        config = json.loads((home / "shift.json").read_text(encoding="utf-8"))
        config["repositories"] = {"repo": {"checkout": str(self.root / "checkout")}}
        charter = shift.load_charter(config)
        self.assertEqual(charter["repositories"], ["repo"])
        self.assertEqual(charter["shift"], {"max_hours": 4})
        now = shift.datetime.now(shift.UTC)
        captain = {"created_at": now.isoformat(), "wake_count": 1000}
        for count in (4, 5, 6):
            with self.subTest(count=count):
                bounds = shift.shift_bounds(dict(captain, compaction_count=count), charter, now)
                self.assertEqual(bounds["handoff_due"], [])
                bounds = shift.shift_bounds(captain, charter, now + timedelta(hours=4))
                self.assertEqual(bounds["handoff_due"], ["shift age reached 4 h"])

    def test_init_refuses_existing_directory_without_touching_contents(self):
        home = self.install()
        before = {path.name: path.read_bytes() for path in home.iterdir()}
        with self.assertRaises(FileExistsError):
            deployment.initialize(home)
        with patch.dict(os.environ, {"PHALANX_HOME": str(home)}):
            with self.assertRaises(FileExistsError):
                deployment.initialize()
        self.assertEqual({path.name: path.read_bytes() for path in home.iterdir()}, before)
        with self.assertRaisesRegex(ValueError, "absolute"):
            deployment.initialize(Path("relative"))
        with self.assertRaises(ValueError):
            deployment.initialize(self.root / "invalid", port=0)
        self.assertFalse((self.root / "invalid").exists())
        with self.assertRaisesRegex(ValueError, "without credentials") as caught:
            deployment.initialize(self.root / "invalid", api_url="https://user:never-print@example.test")
        self.assertNotIn("never-print", str(caught.exception))
        self.assertFalse((self.root / "invalid").exists())

    def test_legacy_json_environment_and_explicit_paths_are_preserved(self):
        home = self.install()
        path = home / "deployment.json"
        config = json.loads(path.read_text())
        legacy = home / "private" / "env.json"
        legacy.parent.mkdir()
        legacy.write_text(json.dumps({
            "PHALANX_DB": str(home / "original.sqlite3"),
            "PHALANX_RUNNER_TOKEN": "private-value",
            "PHALANX_API_URL": "http://127.0.0.1:8765",
        }))
        config["environment_file"] = str(legacy)
        config["reconciler_state"] = str(home / "original-scratch")
        path.write_text(json.dumps(config))
        os.environ["PHALANX_RUNNER_TOKEN"] = "inherited-value"
        actual, env = deployment.configuration(path)
        self.assertEqual(actual, config)
        self.assertEqual(env["PHALANX_DB"], str(home / "original.sqlite3"))
        self.assertEqual(env["PHALANX_RUNNER_TOKEN"], "inherited-value")
        self.assertNotIn("PHALANX_HOME", env)
        self.assertNotIn("PHALANX_DB", os.environ)
        for invalid in (
            '{"UNKNOWN": "do-not-print-this"}',
            '{"PHALANX_RUNNER_TOKEN":"do-not-print-this","PHALANX_RUNNER_TOKEN":"duplicate"}',
            '{"PHALANX_RUNNER_TOKEN":null}',
        ):
            legacy.write_text(invalid)
            with self.subTest(environment=invalid), self.assertRaises(MusterError) as caught:
                deployment.configuration(path)
            self.assertNotIn("do-not-print-this", str(caught.exception))

    def test_server_refuses_missing_database_and_duplicate_owner(self):
        home = self.install()
        config, env = deployment.configuration(home / "deployment.json")
        database = Path(env["PHALANX_DB"])
        with patch.object(deployment.subprocess, "call") as call:
            with self.assertRaisesRegex(ValueError, "missing"):
                deployment.run_service(config, env, "server")
            call.assert_not_called()
            self.assertFalse(database.parent.exists())
            database.parent.mkdir()
            database.write_bytes(b"existing database identity")
            with any_file_lock([database.with_name(database.name + ".service.lock")]):
                with self.assertRaisesRegex(ValueError, "already owns"):
                    deployment.run_service(config, env, "server", serve=Mock())
            call.assert_not_called()
            with self.assertRaisesRegex(ValueError, "only for a new"):
                deployment.run_service(config, env, "server", initialize_database=True)
            self.assertEqual(database.read_bytes(), b"existing database identity")

    def test_service_and_cli_delegate_arguments_environment_and_exit_code(self):
        home = self.install()
        config, env = deployment.configuration(home / "deployment.json")
        with patch.object(deployment.subprocess, "call", return_value=7) as call:
            self.assertEqual(deployment.run_service(config, env, "reconciler", once=True), 7)
            self.assertEqual(call.call_args.args[0], [
                sys.executable, "-P", "-m", "phalanx", "reconcile", "--config",
                config["reconciler_config"], "--state", config["reconciler_state"], "--once"])
            self.assertEqual(call.call_args.kwargs["env"], env)
            serve = Mock()
            self.assertEqual(deployment.run_service(
                config, env, "server", initialize_database=True, serve=serve), 0)
            serve.assert_called_once_with(host="127.0.0.1", port=8000)
            with patch("sys.argv", ["phalanx", "deploy", "cli", "--config",
                                   str(home / "deployment.json"), "--", "shift", "status"]):
                with self.assertRaises(SystemExit) as caught:
                    main()
            self.assertEqual(caught.exception.code, 7)
            self.assertEqual(call.call_args.args[0][-2:], ["shift", "status"])
            self.assertNotIn("PHALANX_RUNNER_TOKEN", os.environ)

    def test_database_removed_during_lock_acquisition_is_not_recreated(self):
        home = self.install()
        config, env = deployment.configuration(home / "deployment.json")
        database = Path(env["PHALANX_DB"])
        database.parent.mkdir()
        database.write_bytes(b"existing database")

        def acquire(paths):
            database.unlink()
            return nullcontext(paths[0])

        serve = Mock()
        with patch.object(deployment, "any_file_lock", side_effect=acquire):
            with self.assertRaisesRegex(ValueError, "disappeared before startup"):
                deployment.run_service(config, env, "server", serve=serve)
        serve.assert_not_called()
        self.assertFalse(database.exists())

    def test_read_only_check_has_no_service_or_database_side_effects(self):
        home = self.install()
        args = Namespace(deploy_command="check", config=home / "deployment.json", home=None)
        with patch.object(deployment.subprocess, "call") as call:
            with self.assertRaisesRegex(ValueError, "must exist"):
                deployment.run_deploy(args)
            database = home / "db" / "phalanx.sqlite3"
            database.parent.mkdir()
            database.write_bytes(b"identity")
            before = {str(p): p.read_bytes() for p in home.rglob("*") if p.is_file()}
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(deployment.run_deploy(args), 0)
            self.assertNotIn("example-test-credential", output.getvalue())
            self.assertEqual({str(p): p.read_bytes() for p in home.rglob("*") if p.is_file()}, before)
            call.assert_not_called()

    def test_invalid_config_and_environment_never_launch_anything(self):
        home = self.install()
        path = home / "deployment.json"
        config = json.loads(path.read_text())
        for invalid in (
            {**config, "environment_file": "relative.env"},
            {**config, "server": {"host": "0.0.0.0", "port": 8000}},
            {**config, "server": {"host": "127.0.0.1", "port": True}},
            {**config, "token": "never-display"},
        ):
            path.write_text(json.dumps(invalid))
            with self.subTest(config=invalid), self.assertRaises(ValueError) as caught:
                deployment.configuration(path)
            self.assertNotIn("never-display", str(caught.exception))
        path.write_text('{"environment_file":"first","environment_file":"never-display"}')
        with self.assertRaisesRegex(ValueError, "Invalid deployment configuration") as caught:
            deployment.configuration(path)
        self.assertNotIn("never-display", str(caught.exception))

    def test_isolated_server_bootstrap_identity_restart_and_single_owner(self):
        home = self.install()
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        path = home / "deployment.json"
        config = json.loads(path.read_text())
        config["server"]["port"] = port
        path.write_text(json.dumps(config))
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
        command = [sys.executable, "-m", "phalanx", "deploy", "run",
                   "--config", str(path), "server"]
        url = f"http://127.0.0.1:{port}"
        for first in (True, False):
            with (home / "service-test.log").open("w+") as log:
                process = subprocess.Popen(
                    [*command, *(["--initialize-database"] if first else [])],
                    env=env, stdout=log, stderr=log)
                try:
                    deadline = time.monotonic() + WAIT_SECONDS
                    while True:
                        if process.poll() is not None:
                            log.seek(0)
                            self.fail(log.read())
                        try:
                            with urlopen(url + "/health", timeout=1) as response:
                                self.assertEqual(json.load(response), {"status": "ok"})
                            break
                        except (OSError, URLError):
                            if time.monotonic() >= deadline:
                                log.seek(0)
                                self.fail("Isolated deployment server did not become healthy:\n" + log.read())
                            time.sleep(0.1)
                    request = Request(url + "/v1/muster/status",
                                      headers={"Authorization": "Bearer " + "r" * 32})
                    with urlopen(request, timeout=WAIT_SECONDS) as response:
                        snapshot = json.load(response)
                    self.assertEqual(snapshot["identity"]["home"], str(home))
                    self.assertEqual(snapshot["writers"], [])
                    duplicate = subprocess.run(command, env=env, capture_output=True,
                                               text=True, timeout=WAIT_SECONDS)
                    self.assertNotEqual(duplicate.returncode, 0)
                    self.assertIn("already owns", duplicate.stderr)
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=30)
        self.assertTrue((home / "db" / "phalanx.sqlite3").is_file())


@unittest.skipUnless(os.name == "nt", "Windows prepared npm environment wrapper")
class ChecksWrapperTest(unittest.TestCase):
    def test_normalized_hash_link_target_arguments_and_exit_status(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = root / "prepared environment"
            tree = root / "worktree"
            environment.mkdir()
            tree.mkdir()
            (environment / "node_modules").mkdir()
            lock = '{"lockfileVersion":3}\n'
            (environment / "lock.sha256").write_text(hashlib.sha256(lock.encode()).hexdigest())
            (tree / "package-lock.json").write_bytes(lock.replace("\n", "\r\n").encode())
            probe = tree / "probe.py"
            probe.write_text(
                "import os,sys\nprint(os.environ['PLAYWRIGHT_BROWSERS_PATH'])\n"
                "print(sys.argv[1])\nsys.exit(7)\n")
            script = Path(__file__).resolve().parents[1] / "scripts" / "checks.ps1"
            command = ["powershell", "-NoProfile", "-File", str(script),
                       "-EnvironmentRoot", str(environment), sys.executable, str(probe), "two words"]
            result = subprocess.run(command, cwd=tree, capture_output=True, text=True)
            self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
            self.assertIn(str(environment / "ms-playwright"), result.stdout)
            self.assertIn("two words", result.stdout)
            self.assertEqual((tree / "node_modules").resolve(), (environment / "node_modules").resolve())
            (tree / "package-lock.json").write_text("changed")
            result = subprocess.run(command, cwd=tree, capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn("stale", result.stderr)
            (tree / "package-lock.json").write_text(lock)
            (tree / "node_modules").rmdir()
            (tree / "node_modules").mkdir()
            (tree / "node_modules" / "preserve").write_text("local state")
            result = subprocess.run(command, cwd=tree, capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn("refusing to replace", result.stderr)
            self.assertEqual((tree / "node_modules" / "preserve").read_text(), "local state")


if __name__ == "__main__":
    unittest.main()

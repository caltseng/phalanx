from argparse import Namespace
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
import hashlib
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from phalanx import muster, reconciler, shift
from phalanx.__main__ import main
from phalanx.store import write_transaction

from test_bootstrap import OWNER, RUNNER, ServerFixture


class LocatorTest(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / "muster"
        self.home.mkdir()
        self.locator = self.root / ".phalanx" / "muster.json"
        self.locator.parent.mkdir()
        self.enterContext(patch("phalanx.muster.Path.home", return_value=self.root))
        self.enterContext(patch.dict(os.environ))
        for key in ("PHALANX_HOME", "PHALANX_SHIFT_CONFIG", "PHALANX_RUNNER_TOKEN", "PHALANX_GITHUB_TOKEN"):
            os.environ.pop(key, None)

    def locate(self, text=None):
        self.locator.write_text(text or json.dumps({"home": str(self.home)}), encoding="utf-8")

    def files(self):
        (self.home / "shift.json").write_text(json.dumps({
            "base_url": "http://127.0.0.1:8000", "runtime": "any", "worktrees": str(self.root / "trees"),
            "repositories": {"repo": {"checkout": str(self.root / "checkout")}},
        }), encoding="utf-8")
        (self.home / "charter.toml").write_text('goal = "test"\n', encoding="utf-8")
        (self.home / "reconciler.json").write_text(json.dumps({
            "base_url": "http://127.0.0.1:8000", "reconciler_id": "test", "repositories": {},
        }), encoding="utf-8")
        (self.home / "private.env").write_text(
            f'PHALANX_RUNNER_TOKEN="{RUNNER}"\nPHALANX_GITHUB_TOKEN=github-secret\n', encoding="utf-8")

    def test_exact_resolution_order_and_no_fallback_from_invalid_override(self):
        self.locate()
        self.assertEqual(muster.resolve_home(), self.home)
        other = self.root / "other"
        other.mkdir()
        os.environ["PHALANX_HOME"] = str(other)
        self.assertEqual(muster.resolve_home(), other)
        self.assertEqual(muster.resolve_home(str(self.home)), self.home)
        os.environ["PHALANX_HOME"] = str(self.root / "absent")
        with self.assertRaisesRegex(muster.MusterError, "missing.*absent"):
            muster.resolve_home()
        self.assertEqual(muster.resolve_home(self.home), self.home)

    def test_missing_locator_never_creates_anything(self):
        before = set(self.root.rglob("*"))
        with self.assertRaisesRegex(muster.MusterError, r"muster.json.*PHALANX_HOME"):
            muster.resolve_home()
        self.assertEqual(set(self.root.rglob("*")), before)

    def test_invalid_and_ambiguous_locators(self):
        for value in ("not json", "[]", "null", '{"home": 3}', '{"home": ""}',
                      '{"home": "relative"}', '{"homes": ["a", "b"]}',
                      '{"home":"a","home":"b"}', '{"home":"a", "other":"b"}'):
            with self.subTest(value=value):
                self.locate(value)
                with self.assertRaises(muster.MusterError):
                    muster.resolve_home()
        for value in ("", "relative"):
            with self.subTest(override=value), self.assertRaises(muster.MusterError):
                muster.resolve_home(value)

    def test_derived_paths_private_env_and_missing_component(self):
        self.files()
        root, path = muster.direct_config("shift", self.home)
        self.assertEqual(path, self.home / "shift.json")
        self.assertEqual(shift.load_config(path, home=root)["charter"], str(self.home / "charter.toml"))
        self.assertEqual(os.environ["PHALANX_RUNNER_TOKEN"], RUNNER)
        os.environ["PHALANX_RUNNER_TOKEN"] = "already-set"
        muster.load_environment(self.home)
        self.assertEqual(os.environ["PHALANX_RUNNER_TOKEN"], "already-set")
        config = json.loads(path.read_text(encoding="utf-8"))
        for value in (None, "", "elsewhere.toml"):
            with self.subTest(charter=value):
                path.write_text(json.dumps({**config, "charter": value}), encoding="utf-8")
                with self.assertRaisesRegex(shift.ShiftError, "must use muster charter"):
                    shift.load_config(path, home=root)
        (self.home / "charter.toml").unlink()
        with self.assertRaisesRegex(muster.MusterError, "missing charter.toml"):
            muster.direct_config("shift", self.home)

    def test_invalid_environment_is_atomic_and_does_not_expose_values(self):
        self.files()
        secret = "never-print-this"
        for invalid in (f"PHALANX_RUNNER_TOKEN={secret}", f"UNKNOWN={secret}",
                        f"PHALANX_GITHUB_TOKEN='{secret}", "PHALANX_GITHUB_TOKEN="):
            with self.subTest(invalid=invalid):
                (self.home / "private.env").write_text(
                    f"PHALANX_RUNNER_TOKEN={secret}\n{invalid}", encoding="utf-8")
                with self.assertRaises(muster.MusterError) as caught:
                    muster.load_environment(self.home)
                self.assertNotIn(secret, str(caught.exception))
                self.assertNotIn("PHALANX_RUNNER_TOKEN", os.environ)

    def test_status_json_and_identity_mismatch_and_unavailable_api(self):
        self.files()
        response = {"status": "ok", "identity": {"home": str(self.home)},
                    "captains": [], "writers": [], "reconcilers": [], "dispatch_paused": False}
        with patch.object(muster.APIClient, "call", return_value=response):
            output = io.StringIO()
            with redirect_stdout(output):
                muster.run_muster(Namespace(home=str(self.home)))
            self.assertEqual(json.loads(output.getvalue())["identity"], response["identity"])
            self.assertNotIn(RUNNER, output.getvalue())
        response["identity"]["home"] = str(self.root)
        with patch.object(muster.APIClient, "call", return_value=response):
            with self.assertRaisesRegex(muster.MusterError, "identity does not match"):
                muster.run_muster(Namespace(home=str(self.home)))
        with patch.object(muster.APIClient, "call", side_effect=muster.APIError(RUNNER)):
            with self.assertRaises(muster.MusterError) as caught:
                muster.run_muster(Namespace(home=str(self.home)))
            self.assertNotIn(RUNNER, str(caught.exception))

    def test_command_defaults_and_explicit_config_compatibility(self):
        self.files()
        self.locate()
        for argv in (["phalanx", "shift", "--home", str(self.home), "charter"],
                     ["phalanx", "shift", "charter", "--home", str(self.home)]):
            with self.subTest(argv=argv), patch("sys.argv", argv), patch.object(shift, "load_charter",
                    return_value={"revision": "test"}), redirect_stdout(io.StringIO()):
                main()
        with patch.object(reconciler, "Reconciler") as worker:
            worker.return_value.run.return_value = {"errors": []}
            with patch("sys.argv", ["phalanx", "reconcile", "--once"]), redirect_stdout(io.StringIO()):
                main()
            self.assertEqual(worker.call_args.args[1], self.home / "reconciler")
            self.assertEqual(worker.call_args.args[0], {
                "base_url": "http://127.0.0.1:8000", "reconciler_id": "test", "repositories": {}})
            self.assertEqual(worker.call_args.args[2].base_url, "http://127.0.0.1:8000")
            self.assertEqual(worker.call_args.args[2].token, RUNNER)
        with patch.object(muster, "resolve_home", side_effect=AssertionError("must not discover")):
            with patch("sys.argv", ["phalanx", "shift", "charter", "--config",
                                   str(self.home / "shift.json")]), patch.object(
                    shift, "load_config", return_value={}), patch.object(
                    shift, "load_charter", return_value={}), redirect_stdout(io.StringIO()):
                main()
            with patch.object(reconciler, "Reconciler") as worker:
                worker.return_value.run.return_value = {"errors": []}
                with patch("sys.argv", ["phalanx", "reconcile", "--config",
                        str(self.home / "reconciler.json"), "--state", str(self.root / "scratch"), "--once"]):
                    main()
                self.assertEqual(worker.call_args.args[1], self.root / "scratch")
        for argv in (["phalanx", "muster", "--home", str(self.home), "status"],
                     ["phalanx", "muster", "status", "--home", str(self.home)]):
            with patch("sys.argv", argv), patch("phalanx.__main__.run_muster") as run:
                main()
                self.assertEqual(run.call_args.args[0].home, str(self.home))

    def test_reconciler_config_requires_its_identity_url_and_mappings(self):
        self.files()
        config_path = self.home / "reconciler.json"
        valid = json.loads(config_path.read_text(encoding="utf-8"))
        for invalid in (
            [], {}, {**valid, "base_url": 1}, {**valid, "reconciler_id": " "},
            {**valid, "reconciler_id": None}, {**valid, "repositories": []},
        ):
            config_path.write_text(json.dumps(invalid), encoding="utf-8")
            with self.subTest(config=invalid), self.assertRaisesRegex(
                    reconciler.ReconcilerError, "base_url, reconciler_id and repositories"):
                reconciler.run_reconcile(Namespace(config=config_path, state=self.root / "scratch"))


class StatusAPITest(ServerFixture, unittest.TestCase):
    def test_authenticated_read_only_snapshot_and_cli(self):
        with self.subTest("live server"):
            directory = self.enterContext(TemporaryDirectory())
            home = Path(directory).resolve()
            path = home / "db.sqlite3"
            self.start_server(path, PHALANX_HOME=str(home))
            self.assertEqual(self.get("/v1/muster/status")[0], 401)
            self.assertEqual(self.request("/v1/repositories", OWNER, {
                "id": "repo", "remote": "https://github.com/example/repo.git"})[0], 200)
            _, item = self.request("/v1/items", OWNER, {
                "repository_id": "repo", "goal": "report", "target": "report"})
            _, captain = self.request("/v1/captains", RUNNER, {
                "session_ref": "captain-session", "charter_revision": "revision"},
                headers={"Idempotency-Key": "captain"})
            token = "writer-secret-token" * 2
            code, registered = self.request(
                f"/v1/items/{item['id']}/executions/{item['execution']['id']}/writer", RUNNER,
                {"captain_id": captain["id"], "runtime": "any", "nonce": "nonce-for-writer1",
                 "token_digest": hashlib.sha256(token.encode()).hexdigest(),
                 "expected_version": item["execution"]["version"]})
            self.assertEqual(code, 200, registered)
            now = datetime.now(UTC)
            with write_transaction(path) as db:
                db.execute("INSERT INTO reconciler_heartbeats VALUES (?, ?)", ("fresh", now.isoformat()))
                db.execute("INSERT INTO reconciler_heartbeats VALUES (?, ?)",
                           ("stale", (now - timedelta(hours=1)).isoformat()))
                db.execute("UPDATE dispatch_settings SET paused=1 WHERE id=1")
                db.execute("INSERT INTO captains (id, session_ref, charter_revision, state, created_at, heartbeat_at) "
                           "VALUES ('old', 'old-session', 'revision', 'active', ?, ?)",
                           ((now - timedelta(hours=1)).isoformat(),) * 2)
            code, snapshot = self.get("/v1/muster/status", RUNNER)
            self.assertEqual(code, 200, snapshot)
            self.assertEqual(snapshot["identity"], {"home": str(home)})
            self.assertEqual([row["id"] for row in snapshot["captains"]], [captain["id"]])
            self.assertEqual(len(snapshot["writers"]), 1)
            self.assertEqual({row["runner_id"]: row["stale"] for row in snapshot["reconcilers"]},
                             {"fresh": False, "stale": True})
            self.assertTrue(snapshot["dispatch_paused"])
            self.assertNotIn("token", json.dumps(snapshot))
            self.assertEqual(self.get("/v1/muster/status", token)[0], 403)
            (home / "shift.json").write_text(json.dumps({
                "base_url": self.url, "runtime": "any", "worktrees": str(home / "trees"),
                "repositories": {"repo": {"checkout": str(home / "checkout")}}}), encoding="utf-8")
            (home / "charter.toml").write_text("", encoding="utf-8")
            (home / "private.env").write_text(f"PHALANX_RUNNER_TOKEN={RUNNER}", encoding="utf-8")
            with patch.dict(os.environ, {"PHALANX_RUNNER_TOKEN": RUNNER}):
                output = io.StringIO()
                with redirect_stdout(output):
                    muster.run_muster(Namespace(home=str(home)))
                self.assertEqual(json.loads(output.getvalue())["status"], "ok")
            self.assertEqual(self.get("/v1/muster/status", OWNER)[1]["writers"], snapshot["writers"])


if __name__ == "__main__":
    unittest.main()

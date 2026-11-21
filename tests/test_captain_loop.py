from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event, Lock
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from phalanx.attended import AttendedRunner, RunnerAPI, RunnerError
from phalanx.captain_adapter import CodexCLIAdapter, CommandTransport
from phalanx.captain_loop import CaptainLoop
from phalanx.workspaces import WorkspaceManager
from test_bootstrap import OWNER, RUNNER, ServerFixture


class QueueAPI:
    def __init__(self, checkout):
        self.checkout = checkout
        self.requests, self.renewed, self.submissions = [], [], []
        self.claims = {}
        self.waiting = ["first", "second"]
        self.stages = {}
        self.expired = []
        self.paused = False
        self.drop_claim = False
        self.drop_result = False
        self.lock = Lock()

    def call(self, route, body=None, *, lease=None, key=None):
        with self.lock:
            if route == "/repositories":
                return [{"id": "repo", "remote": str(self.checkout)}]
            if route.endswith("/recovery"):
                return self.expired.copy()
            if route == "/dispatch/claim":
                self.requests.append((body.copy(), lease))
                if lease not in self.claims:
                    if self.paused:
                        return {"status": "paused"}
                    if not self.waiting:
                        return {"status": "no_eligible_work"}
                    item = self.waiting.pop(0)
                    self.claims[lease] = {
                        "status": "claimed", "item_id": item, "attempt_id": str(uuid4()),
                        "lease_expires_at": time.time() + 90,
                        "request": {"repository_id": "repo", "goal": "Write a report", "target": "report"},
                        "execution": {"id": item + "-execution", "version": 3,
                                      "state": {"stage": self.stages.get(item, "report_drafting"),
                                                "status": "running"}},
                    }
                if self.drop_claim:
                    self.drop_claim = False
                    raise RunnerError("lost claim response")
                return self.claims[lease]
            if route.startswith("/items/") and not route.endswith("/result"):
                item = route.split("/")[2]
                return {"execution": next(row["execution"] for row in self.claims.values() if row["item_id"] == item)}
            if route.endswith("/renew"):
                row = self.claims[lease]
                self.renewed.append(row["item_id"])
                return {"attempt_id": row["attempt_id"], "lease_expires_at": time.time() + 90}
            if route.endswith("/result"):
                self.submissions.append((body, lease, key))
                if self.drop_result:
                    self.drop_result = False
                    raise RunnerError("lost result response")
                return {"version": 4}
            raise AssertionError(route)


class NativeFixture:
    def __init__(self):
        self.launches = []
        self.waits = []
        self.threads = {}
        self.unknown = set()
        self.fail_launch = False
        self.gate = None
        self.gated_slot = 1

    def __call__(self, operation, arguments):
        if operation == "resolve_project":
            return arguments.copy()
        if operation == "create_thread":
            self.launches.append(arguments)
            receipt = {"marker": arguments["marker"], "host_id": arguments["host_id"],
                       "thread_id": str(uuid4()), "started_at": datetime.now(timezone.utc).isoformat()}
            self.threads[arguments["marker"]] = receipt
            if self.fail_launch:
                raise RunnerError("ambiguous desktop launch")
            return receipt
        if operation == "wait_threads":
            self.waits.append(arguments)
            if self.gate and any(ref["slot"] == self.gated_slot for ref in arguments["references"]):
                self.gate.wait(5)
            rows = []
            for ref in arguments["references"]:
                receipt = self.threads.get(ref["marker"])
                if receipt:
                    rows.append({
                        **receipt, "project_id": ref["project_id"], "assignment": ref["assignment"],
                        "state": "unknown" if ref["slot"] in self.unknown else "active",
                    })
            return {"observed_at": datetime.now(timezone.utc).isoformat(), "threads": rows,
                    "discovery_complete": True,
                    "cursors": {ref["marker"]: "next-page" for ref in arguments["references"]}}
        raise AssertionError(operation)


class CaptainLoopTests(ServerFixture, unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.checkout = self.directory / "checkout"
        self.checkout.mkdir()
        for args in (("init", "-b", "main"), ("remote", "add", "origin", str(self.checkout)),
                     ("commit", "--allow-empty", "-m", "Base"),
                     ("update-ref", "refs/remotes/origin/main", "HEAD")):
            subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.test", *args],
                           cwd=self.checkout, capture_output=True, check=True)
        self.root = self.directory / "runner"
        self.config = {
            "runner_id": "desk", "base_url": "http://127.0.0.1:8000", "capacity": 2,
            "repositories": {"repo": {"checkout": str(self.checkout), "base_branch": "main",
                                      "host_id": "local", "project_id": "project"}},
        }
        self.api = QueueAPI(self.checkout)
        self.native = NativeFixture()
        self.events = []

    def loop(self, **options):
        loop = CaptainLoop(
            self.root, self.config, self.api, CodexCLIAdapter(self.native),
            poll_seconds=0.01, heartbeat_seconds=0.02, batch_size=1,
            emit=self.events.append, **options,
        )
        self.addCleanup(loop.close)
        return loop

    def spin(self, loop, condition, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            loop.step()
            if condition():
                return
            time.sleep(0.005)
        self.fail(f"Loop did not reach expected condition; events={self.events}")

    def write_result(self, slot, text="# Report"):
        runner = AttendedRunner(self.root, self.config, self.api, slot=slot)
        packet = runner._handoff(runner._snapshot()).packet
        handoff = runner._handoff(runner._snapshot())
        handoff.result_path.write_text(json.dumps({
            **packet.result_identity(), "outcome": "succeeded", "details": {"evidence": text},
        }), encoding="utf-8")
        return packet

    def test_slow_git_and_native_wait_do_not_block_other_slot_or_heartbeats(self):
        git_gate, native_gate = Event(), Event()
        original = WorkspaceManager.prepare
        entered = Event()
        def prepare(manager, item, **kwargs):
            if item == "first":
                entered.set()
                git_gate.wait(5)
            return original(manager, item, **kwargs)
        self.native.gate = native_gate
        self.native.gated_slot = 3
        loop = self.loop()
        with patch.object(WorkspaceManager, "prepare", prepare):
            try:
                self.spin(loop, lambda: entered.is_set() and len(self.native.launches) == 1)
                self.spin(loop, lambda: {"first", "second"} <= set(self.api.renewed))
                git_gate.set()
                self.spin(loop, lambda: len(self.native.launches) == 2)
                second_slot = next(slot for slot, row in loop._records().items()
                                   if row["claim"]["item_id"] == "second")
                self.native.gated_slot = 3 - second_slot
                packet = self.write_result(second_slot)
                receipt = self.native.threads[packet.marker]
                original_native = self.native.__call__
                def stopped(operation, arguments):
                    reply = original_native(operation, arguments)
                    if operation == "wait_threads":
                        for row in reply["threads"]:
                            if row["thread_id"] == receipt["thread_id"]:
                                row.update(state="stopped", writers_stopped=True)
                    return reply
                loop.adapter = CodexCLIAdapter(stopped)
                self.spin(loop, lambda: bool(self.api.submissions))
                self.assertFalse(native_gate.is_set())
                self.assertLessEqual(max(len(wait["references"]) for wait in self.native.waits), 1)
            finally:
                git_gate.set()
                native_gate.set()

    def test_ambiguous_launch_survives_restart_without_relay_and_recovers_receipt(self):
        self.api.waiting = ["first"]
        self.native.fail_launch = True
        loop = self.loop()
        self.spin(loop, lambda: any(event["status"] == "slot_blocked" for event in self.events))
        loop.close()
        self.assertEqual(len(self.native.launches), 1)
        self.native.fail_launch = False
        restarted = self.loop()
        self.spin(restarted, lambda: bool(restarted._records()[1].get("receipt")))
        for _ in range(5):
            restarted.step()
            time.sleep(0.01)
        self.assertEqual(len(self.native.launches), 1)
        self.assertEqual(restarted._records()[1]["relay"]["status"], "started")

    def test_pending_claim_replays_original_filters_and_secret(self):
        self.api.waiting = ["first"]
        self.api.drop_claim = True
        runner = AttendedRunner(self.root, self.config, self.api)
        with self.assertRaisesRegex(RunnerError, "lost claim"):
            runner.prepare()
        original = self.api.requests[0]
        loop = self.loop()
        self.spin(loop, lambda: bool(self.native.launches))
        replay = next(request for request in self.api.requests[1:] if request[1] == original[1])
        self.assertEqual(replay, original)
        self.assertEqual(len(self.native.launches), 1)

    def test_changed_claim_after_project_resolution_never_relays(self):
        self.api.waiting = ["first"]
        original = self.native.__call__
        resolutions = []
        def native(operation, arguments):
            if operation == "resolve_project":
                resolutions.append(arguments)
                if len(resolutions) == 2:
                    for claim in self.api.claims.values():
                        claim["execution"]["state"]["stopping"] = True
            return original(operation, arguments)
        loop = self.loop()
        loop.adapter = CodexCLIAdapter(native)
        with self.assertRaisesRegex(RunnerError, "changed before launch"):
            loop._prepare(1)
        self.assertFalse(self.native.launches)
        self.assertIn("packet", loop._records()[1])

    def test_pause_capacity_reduction_and_unmatched_attempts_prevent_admission(self):
        self.api.paused = True
        loop = self.loop()
        self.spin(loop, lambda: any(event["status"] == "paused" for event in self.events))
        self.assertFalse(loop._records())
        self.api.paused = False
        self.spin(loop, lambda: len(self.native.launches) == 2)
        loop.close()
        self.config["capacity"] = 1
        self.api.waiting = ["third"]
        restarted = self.loop()
        self.spin(restarted, lambda: "second" in self.api.renewed)
        self.assertEqual(set(restarted._records()), {1, 2})
        self.assertEqual(len(self.native.launches), 2)
        restarted.close()
        self.api.expired = [{"attempt_id": "unmatched", "item_id": "missing"}]
        self.root = self.directory / "other-state"
        blocked = self.loop()
        before = len(self.api.requests)
        self.spin(blocked, lambda: bool(blocked.unmatched))
        self.assertEqual(len(self.api.requests), before)
        self.assertTrue(any(row["status"] == "unmatched_attempts"
                            for row in blocked.runner.status(all_slots=True)["loop_status"]))

    def test_cursor_retention_is_per_attempt_and_not_authority(self):
        loop = self.loop()
        self.spin(loop, lambda: len(self.native.waits) >= 4)
        self.assertTrue(any(wait["cursors"] for wait in self.native.waits))
        self.assertEqual(len(self.native.launches), 2)
        with loop.runner._journal() as db:
            self.assertLessEqual(db.execute("SELECT count(*) FROM captain WHERE key LIKE 'cursor-%'").fetchone()[0], 2)

    def test_saved_submission_replays_even_after_local_lease_expiry(self):
        self.api.waiting = ["first"]
        self.api.drop_result = True
        loop = self.loop()
        self.spin(loop, lambda: len(self.native.launches) == 1)
        packet = self.write_result(1)
        runner = AttendedRunner(self.root, self.config, self.api)
        row = runner._snapshot()
        runner.receipt(packet.marker, row["host_id"], thread_id=self.native.threads[packet.marker]["thread_id"])
        with self.assertRaisesRegex(RunnerError, "lost result"):
            runner.submit(packet.marker, row["host_id"], self.native.threads[packet.marker]["thread_id"])
        runner._transaction(lambda record, db: record.update(lease_expires_at=0))
        loop.expired = {row["claim"]["attempt_id"]: {}}
        loop._advance(1, runner._snapshot(), None)
        self.assertEqual(self.api.submissions[0], self.api.submissions[1])
        self.assertFalse(runner._snapshot())

    def test_fixture_subprocess_verifies_native_and_queued_receipts(self):
        fixture = self.directory / "fixture.json"
        project = {field: self.config["repositories"]["repo"][field]
                   for field in ("host_id", "project_id", "checkout")}
        fixture.write_text(json.dumps({"projects": [project], "queued": True}), encoding="utf-8")
        command = [sys.executable, str(Path(__file__).parent / "fixtures" / "captain_bridge.py"), str(fixture)]
        adapter = CodexCLIAdapter(CommandTransport(command))
        adapter.resolve_project(project)
        runner = AttendedRunner(self.root, self.config, self.api)
        launch = runner.prepare()
        receipt = adapter.launch(launch)
        self.assertIn("client_thread_id", receipt)
        runner.receipt(launch["marker"], "local", **receipt)
        packet = runner._handoff(runner._snapshot()).packet
        batch = adapter.wait_threads([{
            "slot": 1, "marker": packet.marker, "project_id": "project", "host_id": "local",
            "started_at": runner._snapshot()["started_at"], "assignment": packet.result_identity(),
            "receipt": receipt,
        }], cursors={}, timeout=1)
        self.assertEqual(str(batch.threads[0].client_thread_id), receipt["client_thread_id"])
        with self.assertRaisesRegex(RunnerError, "failed with exit code"):
            adapter.launch(launch)
        with self.assertRaisesRegex(RunnerError, "unavailable"):
            CommandTransport([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.01)("wait_threads", {})
        with patch.dict(os.environ, {"PHALANX_RUNNER_TOKEN": "private", "PHALANX_GITHUB_TOKEN": "private"}):
            response = CommandTransport([sys.executable, "-c", (
                "import json, os; print(json.dumps({'tokens': [key for key in os.environ "
                "if key in ('PHALANX_RUNNER_TOKEN', 'PHALANX_GITHUB_TOKEN')]}))"
            )])("resolve_project", {})
        self.assertEqual(response, {"tokens": []})

    def test_full_report_route_uses_native_workers_and_runner_only_storage(self):
        self.start_server(self.directory / "server.sqlite3")
        self.request("/v1/repositories", OWNER, {"id": "repo", "remote": str(self.checkout)})
        status, item = self.request("/v1/items", OWNER, {
            "repository_id": "repo", "goal": "Write a test report", "target": "report",
        })
        self.assertEqual(status, 201)
        self.api = RunnerAPI(self.url, RUNNER)
        original = self.native.__call__
        def native(operation, arguments):
            reply = original(operation, arguments)
            if operation == "wait_threads":
                for ref, row in zip(arguments["references"], reply["threads"]):
                    packet = ref["assignment"]
                    result = {**packet, "outcome": "succeeded", "details": {"evidence": (
                        "# Tests\nUse unittest." if packet["stage"] == "report_drafting" else "Verified command."
                    )}}
                    path = self.root / "results" / f"{ref['marker']}.json"
                    if not path.exists():
                        path.write_text(json.dumps(result), encoding="utf-8")
                    row.update(state="stopped", writers_stopped=True)
            return reply
        self.native = native
        loop = self.loop()
        self.spin(loop, lambda: self.get(f"/v1/items/{item['id']}", OWNER)[1]["execution"]["state"]["status"]
                  == "completed" and any(event["status"] == "report_storage_submitted" for event in self.events),
                  timeout=10)
        stages = [json.loads(launch["create_thread"]["prompt"].split("Work packet:\n")[1].split(
            "\n\nWrite one UTF-8")[0])["result_identity"]["stage"] for launch in original.__self__.launches]
        self.assertEqual(stages, ["report_drafting", "report_review"])
        self.assertTrue(any(event["status"] == "report_storage_submitted" for event in self.events))

    def test_slow_runner_only_draft_keeps_worker_slot_and_renewals_usable(self):
        self.api.stages["first"] = "pr_creation"
        gate, entered = Event(), Event()
        def draft(runner, snapshot, token):
            entered.set()
            gate.wait(5)
            runner._transaction(lambda record, db: record.clear())
            return {"status": "draft_submitted"}
        loop = self.loop()
        with patch.dict(os.environ, {"PHALANX_GITHUB_TOKEN": "fixture-only"}), patch.object(
            AttendedRunner, "_run_draft", draft,
        ):
            try:
                self.spin(loop, lambda: entered.is_set() and len(self.native.launches) == 1)
                self.spin(loop, lambda: {"first", "second"} <= set(self.api.renewed))
                self.assertEqual(len(self.native.launches), 1)
                self.assertTrue(all("pr_creation" in body["stages"] for body, _ in self.api.requests))
            finally:
                gate.set()
                loop.close()

    def test_identity_and_stopped_attestation_are_rejected(self):
        adapter = CodexCLIAdapter(lambda operation, arguments: {
            "host_id": "wrong", "project_id": "project", "checkout": str(self.checkout),
        })
        with self.assertRaisesRegex(RunnerError, "exact host"):
            adapter.resolve_project(self.config["repositories"]["repo"])
        adapter = CodexCLIAdapter(lambda operation, arguments: {
            "observed_at": datetime.now(timezone.utc).isoformat(), "discovery_complete": True,
            "threads": [{"host_id": "local", "project_id": "project", "marker": "marker",
                         "thread_id": str(uuid4()), "started_at": datetime.now(timezone.utc).isoformat(),
                         "assignment": {}, "state": "stopped"}],
        })
        with self.assertRaisesRegex(RunnerError, "invalid or stale"):
            adapter.wait_threads([{"marker": "marker"}], cursors={}, timeout=1)
        loop = self.loop()
        loop.close()
        self.config["runner_id"] = "other"
        with self.assertRaisesRegex(RunnerError, "another runner"):
            self.loop()

    def test_cli_forwards_adapter_flags_without_consuming_them(self):
        from phalanx.__main__ import main
        with patch.object(sys, "argv", [
            "phalanx", "captain", "--config", "runner.json", "--state", str(self.root),
            "--adapter-command", "bridge", "--host", "fixture-host",
        ]), patch("phalanx.__main__.run_captain") as run:
            main()
        self.assertEqual(run.call_args.args[0].adapter_command, ["bridge", "--host", "fixture-host"])

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
                    eligible = [item for item in self.waiting
                                if "repo" in body["repository_ids"]
                                and self.stages.get(item, "report_drafting") in body["stages"]]
                    if not eligible:
                        return {"status": "no_eligible_work"}
                    item = eligible[0]
                    self.waiting.remove(item)
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
        self.queued = False
        self.stopped = set()
        self.omit_queued = False

    def __call__(self, operation, arguments):
        if operation == "resolve_project":
            return arguments.copy()
        if operation == "create_thread":
            self.launches.append(arguments)
            receipt = {"marker": arguments["marker"], "host_id": arguments["host_id"],
                       "thread_id": str(uuid4()), "started_at": datetime.now(timezone.utc).isoformat()}
            self.threads[arguments["marker"]] = receipt
            if self.queued:
                receipt["client_thread_id"] = str(uuid4())
            if self.fail_launch:
                raise RunnerError("ambiguous desktop launch")
            if self.queued:
                return {field: receipt[field] for field in ("host_id", "marker", "client_thread_id")}
            return receipt
        if operation == "wait_threads":
            self.waits.append(arguments)
            if self.gate and any(ref["slot"] == self.gated_slot for ref in arguments["references"]):
                self.gate.wait(5)
            rows = []
            for ref in arguments["references"]:
                receipt = self.threads.get(ref["marker"])
                if receipt:
                    row = {
                        **receipt, "project_id": ref["project_id"], "assignment": ref["assignment"],
                        "state": "unknown" if ref["slot"] in self.unknown else "active",
                    }
                    if ref["marker"] in self.stopped:
                        row.update(state="stopped", writers_stopped=True)
                    if self.omit_queued:
                        row.pop("client_thread_id", None)
                    rows.append(row)
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
        self.config["capacity"] = 1
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

    def test_queued_receipt_promotes_on_active_then_submits_on_native_only_stop(self):
        self.config["capacity"] = 1
        self.api.waiting = ["first"]
        self.native.queued = True
        loop = self.loop()
        self.spin(loop, lambda: bool(loop._records().get(1, {}).get("receipt", {}).get("thread_id")))
        snapshot = loop._records()[1]
        receipt = snapshot["receipt"].copy()
        self.assertEqual(set(receipt), {"thread_id", "client_thread_id"})
        packet = self.write_result(1)
        self.native.omit_queued = True
        self.native.stopped.add(packet.marker)
        self.spin(loop, lambda: bool(self.api.submissions) and not loop._records())
        self.assertFalse(any("Receipt conflicts" in event["reason"] for event in self.events))
        with loop.runner._journal() as db:
            archive = json.loads(db.execute("SELECT record FROM completed").fetchone()[0])
        self.assertEqual(archive["receipt"], receipt)

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
        loop.close()
        self.assertFalse(loop._records())
        self.assertFalse(self.api.claims)
        self.api.paused = False
        loop = self.loop()
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
        self.spin(loop, lambda: len(self.native.waits) >= 4 and len(self.native.launches) == 2)
        self.assertTrue(any(wait["cursors"] for wait in self.native.waits))
        self.assertEqual(len(self.native.launches), 2)
        with loop.runner._journal() as db:
            self.assertLessEqual(db.execute("SELECT count(*) FROM captain WHERE key LIKE 'cursor-%'").fetchone()[0], 2)

    def test_saved_submission_replays_even_after_local_lease_expiry(self):
        self.config["capacity"] = 1
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
                self.assertTrue(any(body["stages"] == ["pr_creation", "report_storage"]
                                    for body, _ in self.api.requests))
            finally:
                gate.set()
                loop.close()

    def test_missing_desktop_mapping_does_not_filter_runner_only_repository(self):
        self.config["repositories"]["healthy"] = {**self.config["repositories"]["repo"],
                                                   "project_id": "healthy-project"}
        self.api.waiting = ["first"]
        self.api.stages["first"] = "report_storage"
        original_native = self.native.__call__
        def native(operation, arguments):
            if operation == "resolve_project" and arguments["project_id"] == "project":
                raise RunnerError("Desktop project unavailable")
            return original_native(operation, arguments)
        self.native = native
        original_call = self.api.call
        def call(route, body=None, **kwargs):
            if route == "/repositories":
                return [{"id": repo, "remote": str(self.checkout)} for repo in self.config["repositories"]]
            return original_call(route, body, **kwargs)
        self.api.call = call
        loop = self.loop()
        loop._metadata("admission-1", "worker")
        def store(runner, snapshot):
            self.assertEqual(snapshot["claim"]["request"]["repository_id"], "repo")
            runner._transaction(lambda record, db: record.clear())
            return {"status": "report_storage_submitted"}
        with patch.object(AttendedRunner, "_store_report", store):
            runner, result = loop._claim_new(1)
        self.assertEqual(result["status"], "report_storage_submitted")
        worker_request = next(body for body, _ in self.api.requests if "report_drafting" in body["stages"])
        storage_request = next(body for body, _ in self.api.requests if body["stages"] == ["report_storage"])
        self.assertEqual(worker_request["repository_ids"], ["healthy"])
        self.assertEqual(storage_request["repository_ids"], ["healthy", "repo"])
        self.assertFalse(original_native.__self__.launches)

    def test_cancel_before_packet_finalizes_and_lost_stop_response_replays(self):
        self.start_server(self.directory / "cancel.sqlite3")
        self.request("/v1/repositories", OWNER, {"id": "repo", "remote": str(self.checkout)})
        _, item = self.request("/v1/items", OWNER, {
            "repository_id": "repo", "goal": "Write a report", "target": "report",
        })
        self.api = RunnerAPI(self.url, RUNNER)
        execution_route = f"/v1/items/{item['id']}/executions/{item['execution']['id']}"
        original_prepare, original_call = WorkspaceManager.prepare, self.api.call
        stop_posts = []
        def prepare(manager, item_id, **kwargs):
            workspace = original_prepare(manager, item_id, **kwargs)
            current = self.get(f"/v1/items/{item_id}", OWNER)[1]["execution"]
            status, stopping = self.request(execution_route + "/cancel", OWNER, {
                "expected_version": current["version"], "reason": "Cancelled before writer launch",
            })
            self.assertEqual(status, 200, stopping)
            return workspace
        def call(route, body=None, **kwargs):
            response = original_call(route, body, **kwargs)
            if route.endswith("/stop-confirmed"):
                stop_posts.append((body.copy(), kwargs.copy()))
                if len(stop_posts) == 1:
                    raise RunnerError("lost stop acknowledgment")
            return response
        self.api.call = call
        loop = self.loop()
        with patch.object(WorkspaceManager, "prepare", prepare):
            self.spin(loop, lambda: self.get(f"/v1/items/{item['id']}", OWNER)[1]["execution"]["state"]["status"]
                      == "cancelled" and not loop._records() and len(stop_posts) == 2, timeout=10)
        for _ in range(10):
            loop.step()
            time.sleep(0.01)
        self.assertEqual(stop_posts[0], stop_posts[1])
        self.assertFalse(self.native.launches)
        timeline = self.get(execution_route + "/timeline", OWNER)[1]
        self.assertEqual(sum(event["event"] == "stop_confirmed" for event in timeline), 1)
        self.assertEqual(self.get(execution_route + "/report", OWNER)[0], 200)

    def test_no_writer_cancellation_replays_exact_stop_through_repeated_ticks(self):
        self.api.waiting = ["first"]
        original_prepare, original_call = WorkspaceManager.prepare, self.api.call
        current, stop_posts = {}, []
        response = None
        def prepare(manager, item_id, **kwargs):
            workspace = original_prepare(manager, item_id, **kwargs)
            claim = next(iter(self.api.claims.values()))
            current.update(
                id=claim["execution"]["id"], version=claim["execution"]["version"] + 1,
                state={"stage": "report_drafting", "status": "waiting_for_external", "stopping": "cancelled"},
            )
            return workspace
        def call(route, body=None, **kwargs):
            nonlocal response
            if route == "/items/first" and current:
                return {"execution": current}
            if route.endswith("/stop-confirmed"):
                stop_posts.append((body.copy(), kwargs.copy()))
                self.assertEqual(body["expected_version"], current["version"])
                if response is None:
                    response = {"id": current["id"], "version": current["version"] + 2,
                                "state": {"status": "cancelled", "final_output": {
                                    "reference": "/v1" + route.removesuffix("/stop-confirmed") + "/report",
                                }}}
                    raise RunnerError("lost stop acknowledgment")
                return response
            return original_call(route, body, **kwargs)
        self.api.call = call
        loop = self.loop()
        with patch.object(WorkspaceManager, "prepare", prepare):
            with self.assertRaisesRegex(RunnerError, "changed before launch"):
                loop._prepare(1)
            self.spin(loop, lambda: len(stop_posts) == 2 and not loop._records())
        for _ in range(10):
            loop.step()
            time.sleep(0.01)
        self.assertEqual(stop_posts[0], stop_posts[1])
        self.assertFalse(self.native.launches)
        with loop.runner._journal() as db:
            archive = json.loads(db.execute("SELECT record FROM completed").fetchone()[0])
        self.assertEqual(archive["stop_acknowledgment"], response)
        self.assertNotIn("lease", archive)

    def test_close_does_not_join_transport_descendant_stdio(self):
        from test_captain_adapter import CommandTransportTests
        self.api.waiting = []
        self.config["stages"] = ["report_storage"]
        probe = CommandTransportTests()
        identities, command = probe.probe(self.directory, parent_wait=10)
        loop = self.loop()
        future = loop.native_pool.submit(CommandTransport(command, timeout=2), "wait_threads", {})
        try:
            self.spin(loop, identities.exists)
            started = time.monotonic()
            loop.close()
            self.assertLess(time.monotonic() - started, 2.6)
            with self.assertRaisesRegex(RunnerError, "unavailable"):
                future.result()
        finally:
            probe.cleanup_probe(identities)

    def test_unlaunched_stop_refuses_dirty_or_ambiguous_reservations(self):
        self.api.waiting = ["first"]
        runner = AttendedRunner(self.root, self.config, self.api)
        self.api.drop_claim = True
        with self.assertRaises(RunnerError):
            runner.prepare()
        loop = self.loop()
        original = WorkspaceManager.prepare
        def prepare(manager, item, **kwargs):
            workspace = original(manager, item, **kwargs)
            for claim in self.api.claims.values():
                claim["execution"]["state"]["stopping"] = "cancelled"
            (workspace.path / "unrecorded.txt").write_text("Preserve this change")
            return workspace
        with patch.object(WorkspaceManager, "prepare", prepare), self.assertRaisesRegex(
            RunnerError, "changed before launch",
        ):
            loop._prepare(1)
        current = self.api.call("/items/first")["execution"]
        with self.assertRaisesRegex(RunnerError, "unaccounted changes"):
            loop._confirm_unlaunched_stop(runner, current)
        self.assertNotIn("cancel_stop", runner._snapshot())
        manager = runner._manager(self.config["repositories"]["repo"], str(self.checkout))
        workspace = manager.inspect("first")
        (workspace.path / "unrecorded.txt").unlink()
        proof = loop._unlaunched_proof(runner, runner._snapshot())
        runner._transaction(lambda record, db: record.update(cancel_stop={
            "proof": proof, "key": "saved-stop", "disposition": "cancelled",
            "body": {"attempt_id": record["claim"]["attempt_id"], "stage": "report_drafting",
                     "expected_version": current["version"]},
        }))
        (workspace.path / "unrecorded.txt").write_text("New effects after the first stop request")
        with self.assertRaisesRegex(RunnerError, "unaccounted changes"):
            loop._confirm_unlaunched_stop(runner)
        runner._transaction(lambda record, db: record.update(relay={"status": "started"}))
        with self.assertRaisesRegex(RunnerError, "ambiguous launch"):
            loop._confirm_unlaunched_stop(runner, current)
        self.assertTrue(runner._snapshot())

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

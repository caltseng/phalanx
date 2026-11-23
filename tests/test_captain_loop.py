from datetime import datetime, timezone
from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import subprocess
import sqlite3
import sys
from tempfile import TemporaryDirectory
from threading import Event, Lock, Thread
import time
import unittest
from unittest.mock import ANY, patch
from uuid import uuid4

from phalanx.attended import AttendedRunner, RunnerAPI, RunnerError
from phalanx.attended_recovery_service import unlaunched_proof
from phalanx.captain_adapter import BridgeAdapter, CommandTransport, LaunchNotDelivered
from phalanx.captain_loop import CaptainLoop, command_notifier
from phalanx.captain_relay import RelayTransport, respond, take_next
from phalanx.transitions import RUNNER_ONLY_SLOTS
from phalanx.workspaces import WorkspaceManager
from test_bootstrap import OWNER, RUNNER, ServerFixture
from timing import WAIT_SECONDS


class QueueAPI:
    def __init__(self, checkout):
        self.checkout = checkout
        self.requests, self.renewed, self.submissions, self.recovered = [], [], [], []
        self.blockers = []
        self.recovery_waits = []
        self.claims = {}
        self.waiting = ["first", "second"]
        self.stages = {}
        self.expired = []
        self.paused = False
        self.drop_claim = False
        self.drop_result = False
        self.lock = Lock()

    def call(self, route, body=None, *, lease=None, key=None, **offer):
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
                self.blockers.append(body.get("blocker"))
                return {"attempt_id": row["attempt_id"], "lease_expires_at": time.time() + 90}
            if route.endswith("/result"):
                self.submissions.append((body, lease, key))
                if self.drop_result:
                    self.drop_result = False
                    raise RunnerError("lost result response")
                return {"version": 4}
            if route.endswith("/recovery-wait"):
                self.recovery_waits.append((route.split("/")[-2], body["blocker"]))
                return {"attempt_id": route.split("/")[-2], "blocker": body["blocker"]}
            if route.endswith("/recover-claim"):
                attempt = route.split("/")[-2]
                old = next(key for key, row in self.claims.items() if row["attempt_id"] == attempt)
                row = self.claims.pop(old)
                self.claims[lease] = row
                self.recovered.append((attempt, body, key))
                return {"attempt_id": attempt, "execution_id": row["execution"]["id"],
                        "version": row["execution"]["version"], "lease_expires_at": int(time.time()) + 90}
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
        self.resumes = []
        self.resume_errors = []

    def __call__(self, operation, arguments):
        if operation == "resume":
            self.resumes.append(arguments)
            if self.resume_errors:
                raise self.resume_errors.pop(0)
            receipt = self.threads[arguments["marker"]]
            return {"host_id": receipt["host_id"], "thread_id": receipt["thread_id"],
                    "marker": arguments["marker"]}
        if operation == "resolve_project":
            return arguments.copy()
        if operation == "launch":
            self.launches.append(arguments)
            receipt = {"marker": arguments["marker"], "host_id": arguments["host_id"],
                       "thread_id": str(uuid4()), "started_at": datetime.now(timezone.utc).isoformat()}
            self.threads[arguments["marker"]] = receipt
            if self.queued:
                receipt["client_thread_id"] = str(uuid4())
            if self.fail_launch:
                raise RunnerError("ambiguous launch")
            if self.queued:
                return {field: receipt[field] for field in ("host_id", "marker", "client_thread_id")}
            return receipt
        if operation == "observe":
            self.waits.append(arguments)
            if self.gate and any(ref["slot"] == self.gated_slot for ref in arguments["references"]):
                self.gate.wait(WAIT_SECONDS)
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
            self.root, self.config, self.api, BridgeAdapter(self.native),
            poll_seconds=0.01, heartbeat_seconds=0.02, batch_size=1,
            emit=self.events.append, **options,
        )
        self.addCleanup(loop.close)
        return loop

    def spin(self, loop, condition, timeout=WAIT_SECONDS):
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
                git_gate.wait(WAIT_SECONDS)
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
                    if operation == "observe":
                        for row in reply["threads"]:
                            if row["thread_id"] == receipt["thread_id"]:
                                row.update(state="stopped", writers_stopped=True)
                    return reply
                loop.adapter = BridgeAdapter(stopped)
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

    def test_undelivered_launch_retracts_relay_intent_and_relaunches_once(self):
        self.config["capacity"] = 1
        self.api.waiting = ["first"]
        original = self.native.__call__
        undelivered = []
        def native(operation, arguments):
            if operation == "launch" and not undelivered:
                undelivered.append(arguments["marker"])
                raise LaunchNotDelivered("Captain relay never took launch")
            return original(operation, arguments)
        loop = CaptainLoop(self.root, self.config, self.api, BridgeAdapter(native),
                           poll_seconds=0.01, heartbeat_seconds=0.02, batch_size=1, emit=self.events.append)
        self.addCleanup(loop.close)
        self.spin(loop, lambda: bool(loop._records().get(1, {}).get("receipt")))
        for _ in range(5):
            loop.step()
            time.sleep(0.01)
        self.assertEqual(len(undelivered), 1)
        self.assertEqual([launch["marker"] for launch in self.native.launches], undelivered)
        self.assertEqual(loop._records()[1]["relay"], {"marker": undelivered[0], "status": "started"})

    def test_relay_stall_is_visible_and_a_scheduled_wake_drains_without_restart(self):
        self.api.waiting = ["first"]
        relay = self.directory / "relay"
        taken, notes = [], []
        def serve(stop):
            while not stop.is_set():
                request = take_next(relay, wait=0.05, poll=0.005)
                if "id" not in request:
                    continue
                taken.append(request["id"])
                try:
                    respond(relay, request["id"], response=self.native(request["operation"], request["arguments"]))
                except RunnerError as error:
                    respond(relay, request["id"], error=str(error))
        def start(count):
            stop = Event()
            threads = [Thread(target=serve, args=(stop,)) for _ in range(count)]
            for thread in threads:
                thread.start()
            def halt():
                stop.set()
                for thread in threads:
                    thread.join(30)
            self.addCleanup(halt)
            return halt
        # A loaded host can delay a live server past a short threshold, so arm it only once the relay is down.
        loop = CaptainLoop(self.root, self.config, self.api, BridgeAdapter(RelayTransport(relay, timeout=30, poll=0.01)),
                           poll_seconds=0.01, heartbeat_seconds=0.02, batch_size=1, relay_stall_seconds=60,
                           emit=self.events.append, notify=notes.append)
        self.addCleanup(loop.close)
        def first_slot():
            # Either slot may claim "first"; slots claim concurrently.
            return next((slot for slot, row in loop._records().items()
                         if row.get("claim", {}).get("item_id") == "first" and row.get("receipt")), None)
        def noted(count):
            return [note["event"] for note in notes[:count]]
        halt = start(1)
        self.spin(loop, lambda: first_slot() is not None, timeout=120)
        halt()
        loop.relay_stall_seconds = 0.3
        packet = self.write_result(first_slot())
        self.native.stopped.add(packet.marker)
        self.api.waiting.append("second")
        stalled = "Captain relay has taken no request"
        self.spin(loop, lambda: any(stalled in (blocker or "") for blocker in self.api.blockers)
                  and noted(1) == ["relay_stalled"], timeout=120)
        self.assertEqual(noted(2), ["relay_stalled"])
        self.assertTrue(any(event["status"] == "relay_stalled" for event in self.events))
        self.assertIn("first", notes[0]["items"])
        self.assertEqual((self.api.submissions, len(self.native.launches)), ([], 1))

        start(2)  # A scheduled wake overlapping a server that is already running.
        # A slow host may stall the new servers again later; only the first recovery is asserted.
        self.spin(loop, lambda: bool(self.api.submissions) and len(self.native.launches) == 2
                  and noted(2) == ["relay_stalled", "relay_serving"], timeout=120)
        self.assertTrue(any(event["status"] == "relay_serving" for event in self.events))
        self.assertEqual(len(taken), len(set(taken)))
        self.assertEqual(len({launch["marker"] for launch in self.native.launches}), 2)
        self.spin(loop, lambda: self.api.blockers[-1] is None, timeout=120)

    def test_notify_command_gets_the_event_without_runner_secrets(self):
        output = self.directory / "event.json"
        script = ("import json, os, sys; event = json.load(sys.stdin); "
                  "event['token'] = os.environ.get('PHALANX_RUNNER_TOKEN'); "
                  f"open({str(output)!r}, 'w').write(json.dumps(event))")
        with patch.dict(os.environ, {"PHALANX_RUNNER_TOKEN": "secret"}):
            command_notifier([sys.executable, "-c", script])({"event": "relay_stalled"})
        self.assertEqual(json.loads(output.read_text()), {"event": "relay_stalled", "token": None})
        self.assertIsNone(command_notifier(None))
        with self.assertRaises(subprocess.CalledProcessError):
            command_notifier([sys.executable, "-c", "raise SystemExit(3)"])({})

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

    def test_finished_slot_retracts_its_queued_observations(self):
        self.config["capacity"] = 1
        self.api.waiting = ["first"]
        retractions = []
        self.native.retract_observations = retractions.append
        loop = self.loop()
        self.spin(loop, lambda: bool(loop._records().get(1, {}).get("receipt")))
        self.assertEqual(retractions, [])
        packet = self.write_result(1)
        self.native.stopped.add(packet.marker)
        self.spin(loop, lambda: bool(self.api.submissions) and not loop._records())
        loop.step()
        self.assertEqual(retractions, [set()])

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
        AttendedRunner(self.root, self.config, self.api).prepare()
        original = self.native.__call__
        resolutions = []
        def native(operation, arguments):
            if operation == "resolve_project":
                resolutions.append(arguments)
                for claim in self.api.claims.values():
                    claim["execution"]["state"]["stopping"] = True
            return original(operation, arguments)
        loop = self.loop()
        loop.adapter = BridgeAdapter(native)
        with self.assertRaisesRegex(RunnerError, "changed before launch"):
            loop._prepare(1)
        self.assertFalse(self.native.launches)
        self.assertIn("packet", loop._records()[1])
        self.assertEqual(len(resolutions), 1)

    def test_cancel_during_post_packet_discovery_confirms_stop_without_launch(self):
        self.config["capacity"] = 1
        self.start_server(self.directory / "packet-cancel.sqlite3")
        self.request("/v1/repositories", OWNER, {"id": "repo", "remote": str(self.checkout)})
        _, item = self.request("/v1/items", OWNER, {
            "repository_id": "repo", "goal": "Write a report", "target": "report",
        })
        self.api = RunnerAPI(self.url, RUNNER)
        AttendedRunner(self.root, self.config, self.api).prepare()
        execution_route = f"/v1/items/{item['id']}/executions/{item['execution']['id']}"
        native, call = self.native.__call__, self.api.call
        stop_posts, prepared = [], []
        loop = self.loop()
        def discover(operation, arguments):
            if operation == "resolve_project" and "packet" in loop._records().get(1, {}) and not prepared:
                prepared.append(loop._records()[1])
                current = self.get(f"/v1/items/{item['id']}", OWNER)[1]["execution"]
                status, stopping = self.request(execution_route + "/cancel", OWNER, {
                    "expected_version": current["version"], "reason": "Cancelled during project discovery",
                })
                self.assertEqual(status, 200, stopping)
            return native(operation, arguments)
        def stop(route, body=None, **kwargs):
            if route.endswith("/stop-confirmed"):
                saved = loop._records()[1]["cancel_stop"]
                self.assertEqual(saved["body"], body)
                self.assertEqual(saved["key"], kwargs["key"])
                stop_posts.append((body.copy(), kwargs.copy()))
            return call(route, body, **kwargs)
        loop.adapter = BridgeAdapter(discover)
        self.api.call = stop
        self.spin(loop, lambda: bool(stop_posts) and not loop._records())
        self.assertEqual(len(stop_posts), 1)
        self.assertFalse(self.native.launches)
        self.assertNotIn("relay", prepared[0])
        self.assertNotIn("receipt", prepared[0])
        with loop.runner._journal() as db:
            archive = json.loads(db.execute("SELECT record FROM completed").fetchone()[0])
        self.assertEqual(archive["packet"], prepared[0]["packet"])
        self.assertEqual(self.get(f"/v1/items/{item['id']}", OWNER)[1]["execution"]["state"]["status"],
                         "cancelled")
        timeline = self.get(execution_route + "/timeline", OWNER)[1]
        self.assertEqual(sum(event["event"] == "stop_confirmed" for event in timeline), 1)

    def test_transient_post_packet_discovery_resumes_original_launch_once(self):
        self.config["capacity"] = 1
        self.api.waiting = ["first"]
        native = self.native.__call__
        failed = False
        loop = self.loop()
        loop.runner.prepare()
        def discover(operation, arguments):
            nonlocal failed
            if operation == "resolve_project" and "packet" in loop._records().get(1, {}) and not failed:
                failed = True
                raise OSError("Transient post-packet discovery failure")
            return native(operation, arguments)
        loop.adapter = BridgeAdapter(discover)
        with self.assertRaisesRegex(RunnerError, "Transient post-packet"):
            loop._prepare(1)
        original = loop._records()[1]
        # Only the runner-only lane keeps polling; the original worker claim is never repeated.
        def worker_claims():
            return sum("report_drafting" in body["stages"] for body, _ in self.api.requests)
        claim_requests = worker_claims()
        self.assertNotIn("relay", original)
        self.assertFalse(self.native.launches)
        self.spin(loop, lambda: bool(loop._records()[1].get("receipt")))
        for _ in range(10):
            loop.step()
            time.sleep(0.01)
        resumed = loop._records()[1]
        self.assertEqual(len(self.native.launches), 1)
        self.assertEqual(worker_claims(), claim_requests)
        self.assertEqual(resumed["packet"], original["packet"])
        self.assertEqual(resumed["lease"], original["lease"])
        self.assertEqual(resumed["started_at"], original["started_at"])
        self.assertEqual(resumed["relay"]["marker"], self.native.launches[0]["marker"])

    def test_one_project_verification_is_shared_across_slots_per_poll(self):
        self.config["repositories"]["alias"] = self.config["repositories"]["repo"].copy()
        calls = []
        native = self.native.__call__
        def discover(operation, arguments):
            if operation == "resolve_project":
                calls.append(arguments)
            return native(operation, arguments)
        loop = self.loop()
        loop.poll_seconds = 30
        loop.adapter = BridgeAdapter(discover)
        self.spin(loop, lambda: len(self.native.launches) == 2)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(self.api.claims), 2)
        loop.discovery_due = 0
        for _ in range(5):
            loop.step()
            time.sleep(0.01)
        self.assertEqual(len(calls), 1, "launched slots at capacity need no project check")
        self.config["capacity"] = 3
        loop.runner.capacity = 3
        self.spin(loop, lambda: len(calls) == 2)

    def test_untrusted_repositories_are_reported_together_and_others_launch(self):
        for name in ("untrusted-a", "untrusted-b"):
            self.config["repositories"][name] = {**self.config["repositories"]["repo"], "project_id": name}
        native = self.native.__call__
        def untrusted(operation, arguments):
            if operation == "resolve_project" and arguments["project_id"].startswith("untrusted"):
                raise RunnerError(f"{arguments['project_id']} is untrusted; trust its checkout once")
            return native(operation, arguments)
        original_call = self.api.call
        def call(route, body=None, **kwargs):
            if route == "/repositories":
                return [{"id": repo, "remote": str(self.checkout)} for repo in self.config["repositories"]]
            return original_call(route, body, **kwargs)
        self.api.call = call
        loop = self.loop()
        loop.adapter = BridgeAdapter(untrusted)
        self.spin(loop, lambda: len(self.native.launches) == 2)
        for _ in range(5):
            loop.step()
            time.sleep(0.01)
        self.assertEqual({launch["session"]["project_id"] for launch in self.native.launches}, {"project"})
        reported = [event for event in self.events if event["status"] == "mapping_blocked"]
        self.assertEqual(len(reported), 1, "one status per change, not one per slot or poll")
        self.assertIn(f"untrusted-a (project untrusted-a, checkout {self.checkout}): untrusted-a is untrusted; "
                      f"trust its checkout once; untrusted-b (project untrusted-b, checkout {self.checkout}): ",
                      reported[0]["reason"])
        saved = AttendedRunner(self.root, self.config, self.api).status(all_slots=True)["loop_status"]
        self.assertIn(reported[0]["reason"], [event["reason"] for event in saved])

    def test_slot_blocker_stays_visible_and_is_published_until_the_condition_changes(self):
        self.config["capacity"] = 1
        self.api.waiting = ["first"]
        loop = self.loop()
        self.spin(loop, lambda: bool(loop._records().get(1, {}).get("receipt")))
        with patch.object(loop, "_native", side_effect=RunnerError("No exact writer")):
            self.spin(loop, lambda: any("No exact writer" in (blocker or "") for blocker in self.api.blockers))
            for _ in range(20):
                loop.step()
                time.sleep(0.01)
            blocked = next(index for index, event in enumerate(self.events) if event["status"] == "slot_blocked")
            self.assertEqual([event for event in self.events[blocked:]
                              if event["slot"] == 1 and event["status"] != "slot_blocked"], [])
        self.spin(loop, lambda: any(event["reason"].startswith("Exact native writer is active")
                                    for event in self.events[blocked:]))
        self.spin(loop, lambda: self.api.blockers[-1] is None)

    def test_long_active_worker_is_published_once_as_overdue_and_keeps_renewing(self):
        self.config["capacity"] = 1
        self.api.waiting = ["first"]
        loop = self.loop(overdue_seconds=0)
        self.spin(loop, lambda: any("with no result" in (blocker or "") for blocker in self.api.blockers))
        renewals = len(self.api.renewed)
        self.spin(loop, lambda: len(self.api.renewed) > renewals + 3)
        self.assertEqual([event["status"] for event in self.events if event["slot"] == 1][-1], "worker_overdue")
        self.assertEqual(sum(event["status"] == "worker_overdue" for event in self.events), 1)
        packet = self.write_result(1)
        self.native.stopped.add(packet.marker)
        self.spin(loop, lambda: bool(self.api.submissions) and not loop._records())

    def test_stopped_writer_without_result_stops_renewing_for_recovery(self):
        self.config["capacity"] = 1
        self.api.waiting = ["first"]
        loop = self.loop()
        self.spin(loop, lambda: bool(loop._records().get(1, {}).get("receipt")))
        marker = AttendedRunner(self.root, self.config, self.api, slot=1)._handoff(loop._records()[1]).packet.marker
        self.native.stopped.add(marker)
        self.spin(loop, lambda: any(event["status"] == "stopped_without_result" for event in self.events))
        self.spin(loop, lambda: 1 not in loop.renewals)
        renewals = len(self.api.renewed)
        for _ in range(20):
            loop.step()
            time.sleep(0.01)
        self.assertEqual(len(self.api.renewed), renewals)
        self.assertEqual(self.api.submissions, [])
        self.assertIn(1, loop._records())

    def test_dirty_stopped_writer_is_resumed_in_place_then_parks_visibly_and_readopts(self):
        self.config["capacity"] = 1
        self.api.waiting = ["first"]
        notified = []
        loop = self.loop(notify=notified.append)
        self.spin(loop, lambda: bool(loop._records().get(1, {}).get("receipt")))
        record = loop._records()[1]
        claim, packet = record["claim"], self._runner_packet(record)
        work = packet.worktree / "work.txt"
        work.write_text("unfinished", encoding="utf-8")
        self.native.stopped.add(packet.marker)
        self.spin(loop, lambda: 1 in loop.lapsing)
        self.api.expired = [{
            "attempt_id": claim["attempt_id"], "item_id": claim["item_id"],
            "execution_id": claim["execution"]["id"], "stage": "report_drafting",
            "version": claim["execution"]["version"], "lease_expires_at": 0,
        }]
        self.native.resume_errors = [LaunchNotDelivered("bridge never took it")]
        with patch("phalanx.attended_recovery.RESUME_GRACE_SECONDS", 0):
            self.spin(loop, lambda: any(event["status"] == "owner_wait" for event in self.events))
        thread = self.native.threads[packet.marker]["thread_id"]
        # The undelivered try was refunded, so the limit allows three delivered resumes.
        self.assertEqual(len(self.native.resumes), 4)
        for resume in self.native.resumes:
            self.assertEqual((resume["thread_id"], resume["marker"], resume["host_id"]),
                             (thread, packet.marker, "local"))
            self.assertIn(str(packet.worktree), resume["prompt"])
        self.assertTrue(any(event["status"] == "resume_failed" for event in self.events))
        self.assertTrue(any(event["status"] == "writer_resumed" for event in self.events))
        self.spin(loop, lambda: bool(notified))
        # A pass that lacked an observation reports another status; the same wait is not notified again.
        loop._status(1, "observing", "Awaiting native observation before recovery")
        for _ in range(10):
            loop.step()
            time.sleep(0.01)
        self.assertEqual(len(notified), 1)
        self.assertEqual(notified[0]["event"], "owner_wait")
        self.assertEqual(notified[0]["items"], ["first"])
        self.assertIn(str(packet.worktree), notified[0]["reason"])
        self.assertIn("Uncommitted changes", notified[0]["reason"])
        waits = [blocker for attempt, blocker in self.api.recovery_waits if attempt == claim["attempt_id"]]
        self.assertTrue(any(blocker and "Resuming the same writer" in blocker for blocker in waits))
        self.assertIn("Recovery is waiting on the owner", waits[-1])
        self.assertEqual(len(self.native.resumes), 4)
        # The writer comes back on its own: the parked wait is re-evaluated and the attempt adopted.
        self.native.stopped.discard(packet.marker)
        self.spin(loop, lambda: bool(self.api.recovered))
        self.assertEqual(self.api.recovered[0][0], claim["attempt_id"])
        # The adopted lease renews again, and each renewal replaces the board blocker.
        renewed = len(self.api.renewed)
        self.spin(loop, lambda: len(self.api.renewed) > renewed)
        self.assertIsNone(self.api.blockers[-1])
        self.assertEqual(work.read_text(encoding="utf-8"), "unfinished")
        self.assertEqual(len(self.native.launches), 1)
        self.assertEqual(self.api.submissions, [])

    def _runner_packet(self, record):
        return AttendedRunner(self.root, self.config, self.api, slot=1)._handoff(record).packet

    def test_renewal_interval_near_the_lease_is_reported(self):
        self.config["capacity"] = 1
        self.api.waiting = ["first"]
        warned = lambda: any(event["status"] == "renewal_interval_too_long" for event in self.events)
        fast = self.loop()
        self.spin(fast, lambda: len(self.api.renewed) >= 3)
        self.assertFalse(warned())
        fast.close()
        slow = CaptainLoop(self.root, self.config, self.api, BridgeAdapter(self.native), poll_seconds=0.01,
                           heartbeat_seconds=60, batch_size=1, emit=self.events.append)
        self.addCleanup(slow.close)
        self.spin(slow, warned)

    def test_malformed_resolution_blocks_only_its_mapping(self):
        self.config["repositories"]["healthy"] = {**self.config["repositories"]["repo"],
                                                   "project_id": "healthy-project"}
        native = self.native.__call__
        def malformed(operation, arguments):
            if operation == "resolve_project" and arguments["project_id"] == "project":
                return ["not", "an", "object"]
            return native(operation, arguments)
        loop = self.loop()
        loop.adapter = BridgeAdapter(malformed)
        resolved = loop._discover({})
        self.assertIsNone(resolved[loop._mapping_key(self.config["repositories"]["healthy"])])
        self.assertRegex(resolved[loop._mapping_key(self.config["repositories"]["repo"])], "AttributeError")

    def test_status_changes_have_one_durable_store_and_no_repeated_writes(self):
        runner = AttendedRunner(self.root, self.config, self.api)
        runner.prepare()
        loop = self.loop()
        statements = []
        journal = loop.runner._journal
        @contextmanager
        def traced():
            with journal() as db:
                db.set_trace_callback(statements.append)
                yield db
        with patch.object(loop.runner, "_journal", traced):
            for _ in range(5):
                loop._status(1, "observing", "Exact native writer is active")
        self.assertEqual(sum("INSERT OR REPLACE INTO captain" in row for row in statements), 1)
        self.assertNotIn("loop", runner._snapshot())
        saved = runner.status()["loop"]
        self.assertEqual(runner.status(all_slots=True)["reservations"][0]["loop"], saved)
        with runner._journal() as db:
            db.execute("INSERT INTO captain VALUES ('cursor-1', '{}')")
            db.execute("INSERT INTO captain VALUES ('admission-1', '\"worker\"')")
        runner._transaction(lambda record, db: record.update(loop=saved))
        restarted = self.loop()
        restarted._status(1, "observing", "Exact native writer is active")
        self.assertEqual(len(self.events), 1)
        self.assertEqual(runner.status()["loop"], saved)
        self.assertNotIn("loop", runner._snapshot())
        with runner._journal() as db:
            self.assertEqual(db.execute(
                "SELECT count(*) FROM captain WHERE key LIKE 'cursor-%' OR key LIKE 'admission-%'"
            ).fetchone()[0], 0)
        restarted._status(1, "slot_blocked", "Restore runtime access")
        self.assertEqual(runner.status()["loop"]["reason"], "Restore runtime access")

    def test_runner_only_no_intent_cancel_replays_and_archives_once_over_http(self):
        self.config["capacity"] = 1
        self.start_server(self.directory / "runner-only-cancel.sqlite3")
        self.request("/v1/repositories", OWNER, {"id": "repo", "remote": str(self.checkout)})
        self.api = RunnerAPI(self.url, RUNNER)
        loop = self.loop()
        runner = loop.runner
        for stage, target, worker_stages, method in (
            ("report_storage", "report", ("report_drafting", "report_review"), "_store_report"),
            ("pr_creation", "ready_for_check_in", ("development",), "_run_draft"),
        ):
            with self.subTest(stage=stage):
                _, item = self.request("/v1/items", OWNER, {
                    "repository_id": "repo", "goal": "Cancel before runner publication", "target": target,
                })
                for worker_stage in worker_stages:
                    runner.prepare()
                    packet = runner._handoff(runner._snapshot()).packet
                    self.assertEqual(packet.stage, worker_stage)
                    evidence = "# Reviewed report" if worker_stage == "report_drafting" else "Reviewed"
                    if worker_stage == "development":
                        subprocess.run([
                            "git", "-c", "user.name=Test", "-c", "user.email=test@example.test",
                            "commit", "--allow-empty", "-m", "Candidate",
                        ], cwd=packet.worktree, check=True, capture_output=True)
                        evidence = subprocess.check_output(
                            ["git", "rev-parse", "HEAD"], cwd=packet.worktree, text=True,
                        ).strip()
                    self.write_result(1, evidence)
                    native_id = str(uuid4())
                    runner.receipt(packet.marker, "local", thread_id=native_id)
                    runner.submit(packet.marker, "local", native_id)
                with patch.object(AttendedRunner, method, side_effect=RunnerError("Interrupted before intent")):
                    with self.assertRaisesRegex(RunnerError, "Interrupted before intent"):
                        runner.prepare(stages=[stage])
                snapshot = runner._snapshot()
                route = f"/v1/items/{item['id']}/executions/{item['execution']['id']}"
                current = self.get(f"/v1/items/{item['id']}", OWNER)[1]["execution"]
                self.assertEqual(self.request(route + "/cancel", OWNER, {
                    "expected_version": current["version"], "reason": "Stop runner-only stage before intent",
                })[0], 200)
                call, stops = self.api.call, []
                def lost_stop(path, body=None, **kwargs):
                    response = call(path, body, **kwargs)
                    if path.endswith("/stop-confirmed"):
                        stops.append((body.copy(), kwargs.copy()))
                        if len(stops) == 1:
                            raise RunnerError("Lost stop acknowledgment")
                    return response
                self.api.call = lost_stop
                try:
                    with self.assertRaisesRegex(RunnerError, "Lost stop acknowledgment"):
                        loop._advance(1, snapshot, None)
                    restarted = self.loop()
                    self.assertEqual(restarted._advance(1, runner._snapshot(), None)["status"], "stop_confirmed")
                finally:
                    self.api.call = call
                self.assertEqual(stops[0], stops[1])
                self.assertFalse(runner._snapshot())
                with runner._journal() as db:
                    archives = db.execute("SELECT record FROM completed WHERE attempt_id=?",
                                          (snapshot["claim"]["attempt_id"],)).fetchall()
                self.assertEqual(len(archives), 1)
                self.assertNotIn("lease", json.loads(archives[0][0]))
                self.assertEqual(self.get(route + "/report", OWNER)[0], 200)
                self.assertEqual(self.get(f"/v1/items/{item['id']}", OWNER)[1]["execution"]["state"]["status"],
                                 "cancelled")
        self.assertFalse(self.native.launches)

    def test_runner_only_cancel_preserves_publication_intents_and_storage_submission(self):
        self.api.waiting = ["first"]
        self.api.stages["first"] = "pr_creation"
        runner = AttendedRunner(self.root, self.config, self.api)
        with patch.object(AttendedRunner, "_run_draft", side_effect=RunnerError("Before intent")):
            with self.assertRaises(RunnerError):
                runner.prepare(stages=["pr_creation"])
        loop = self.loop()
        snapshot = runner._snapshot()
        current = {**snapshot["claim"]["execution"], "version": 4,
                   "state": {"stage": "pr_creation", "stopping": "cancelled"}}
        publication = self.root / "publication"
        publication.mkdir()
        for filename, table in (("draft-publication.sqlite3", "draft_publication"),
                                ("draft-observations.sqlite3", "draft_observations")):
            with self.subTest(table=table):
                with closing(sqlite3.connect(publication / filename)) as db, db:
                    db.execute(f"CREATE TABLE {table} (item_id TEXT PRIMARY KEY)")
                    db.execute(f"INSERT INTO {table} VALUES ('first')")
                with self.assertRaisesRegex(RunnerError, "effect reconciliation"):
                    loop._confirm_stop(runner, current)
                self.assertEqual(runner._snapshot(), snapshot)
                with closing(sqlite3.connect(publication / filename)) as db, db:
                    db.execute(f"DELETE FROM {table}")
        runner._transaction(lambda record, db: record.update(submission={"body": {}}))
        with self.assertRaisesRegex(RunnerError, "effect reconciliation"):
            loop._confirm_stop(runner, current)
        self.assertIn("submission", runner._snapshot())
        self.assertFalse(self.native.launches)

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

    def test_runner_only_lane_claims_beside_full_agent_slots_without_touching_workers(self):
        for capacity in (1, 5, 10):
            with self.subTest(capacity=capacity):
                self.setUp()
                self.config["capacity"] = capacity
                storage = [f"storage-{n}" for n in range(RUNNER_ONLY_SLOTS + 1)]
                workers = [f"worker-{n}" for n in range(capacity + 1)]
                # Runner-only work ranks first, but general slots still fill with agent workers.
                self.api.waiting = storage + workers
                self.api.stages.update(dict.fromkeys(storage, "report_storage"))
                gate, lock, stored, running = Event(), Lock(), [], []
                peak = [0]
                def store(runner, snapshot):
                    with lock:
                        stored.append((runner.slot, snapshot["claim"]["item_id"]))
                        running.append(runner.slot)
                        peak[0] = max(peak[0], len(running))
                    gate.wait(WAIT_SECONDS)
                    with lock:
                        running.remove(runner.slot)
                    runner._transaction(lambda record, db: record.clear())
                    return {"status": "report_storage_submitted"}
                with patch.object(AttendedRunner, "_store_report", store):
                    loop = self.loop()
                    try:
                        self.spin(loop, lambda: len(self.native.launches) == capacity
                                  and len(stored) == RUNNER_ONLY_SLOTS)
                        launched = {slot: record["claim"]["attempt_id"]
                                    for slot, record in loop._records().items() if slot <= capacity}
                        for _ in range(20):
                            loop.step()
                            time.sleep(0.01)
                        self.assertEqual(set(launched), set(range(1, capacity + 1)))
                        self.assertEqual({slot for slot, _ in stored},
                                         set(range(capacity + 1, capacity + RUNNER_ONLY_SLOTS + 1)))
                        self.assertEqual((peak[0], len(stored)), (RUNNER_ONLY_SLOTS, RUNNER_ONLY_SLOTS))
                        self.assertEqual(self.api.waiting, [storage[-1], workers[-1]])
                        gate.set()
                        # Agent slots stay full; the lane admits the next runner-only stage.
                        self.spin(loop, lambda: len(stored) == len(storage))
                        self.assertEqual(stored[-1], (stored[-1][0], storage[-1]))
                        self.assertGreater(stored[-1][0], capacity)
                        self.assertEqual(len(self.native.launches), capacity)
                        self.assertEqual({slot: record["claim"]["attempt_id"] for slot, record
                                          in loop._records().items() if slot <= capacity}, launched)
                        self.assertEqual(self.api.waiting, [workers[-1]])
                    finally:
                        gate.set()
                        loop.close()

    def test_unmatched_attempt_reserves_capacity_without_blocking_other_slot(self):
        self.api.expired = [{"attempt_id": "unmatched", "item_id": "missing"}]
        loop = self.loop()
        self.spin(loop, lambda: len(self.native.launches) == 1)
        for _ in range(20):
            loop.step()
            time.sleep(0.01)
        self.assertEqual(len(self.native.launches), 1)
        self.assertEqual(len(loop._records()) + len(loop.unmatched), self.config["capacity"])
        self.assertTrue(any(event["status"] == "unmatched_attempts" for event in self.events))

    def test_unmatched_attempts_status_clears_when_none_remain_and_after_restart(self):
        def unmatched_status(loop):
            return [row for row in loop.runner.status(all_slots=True)["loop_status"]
                    if row["status"] == "unmatched_attempts"]

        self.api.expired = [{"attempt_id": "unmatched", "item_id": "missing"}]
        loop = self.loop()
        self.spin(loop, lambda: bool(unmatched_status(loop)))
        self.api.expired = []
        self.spin(loop, lambda: not unmatched_status(loop))
        self.assertTrue(any(event["status"] == "cleared" for event in self.events))
        self.api.expired = [{"attempt_id": "unmatched", "item_id": "missing"}]
        self.spin(loop, lambda: bool(unmatched_status(loop)))
        loop.close()
        # A status persisted by an earlier run is cleared by the first fresh recovery listing.
        self.api.expired = []
        restarted = self.loop()
        self.spin(restarted, lambda: not unmatched_status(restarted))

    def test_expired_active_writer_is_adopted_after_observation_and_keeps_renewing(self):
        self.config["capacity"] = 1
        self.api.waiting = ["first"]
        loop = self.loop()
        loop._prepare(1)
        snapshot = loop._records()[1]
        claim = snapshot["claim"]
        self.api.expired = [{
            "attempt_id": claim["attempt_id"], "item_id": claim["item_id"],
            "execution_id": claim["execution"]["id"], "stage": "report_drafting",
            "version": claim["execution"]["version"], "lease_expires_at": 0,
        }]
        loop.expired = {claim["attempt_id"]: self.api.expired[0]}
        for _ in range(6):
            result = loop._advance(1, loop._records()[1], None)
            self.assertEqual(result["status"], "observing")
        self.assertNotIn("recovery", loop._records()[1])
        old_lease = loop._records()[1]["lease"]
        batch = loop._wait((1,), loop._records())
        self.assertEqual(loop._advance(1, loop._records()[1], batch)["status"], "adopted")
        record = loop._records()[1]
        self.assertNotEqual(record["lease"], old_lease)
        self.assertNotIn("recover_claim", record)
        self.assertEqual(len(self.api.recovered), 1)
        self.api.expired, loop.expired = [], {}
        renewed = len(self.api.renewed)
        AttendedRunner(self.root, self.config, self.api, slot=1).renew()
        self.assertEqual(len(self.api.renewed), renewed + 1)
        self.assertEqual(loop._records()[1]["lease"], record["lease"])
        self.assertEqual(len(self.native.launches), 1)

    def test_malformed_row_blocks_only_its_marker_in_shared_batch(self):
        native = self.native.__call__
        loop = self.loop()
        self.spin(loop, lambda: len(self.native.launches) == 2)
        loop.close()
        records = loop._records()
        bad_slot, good_slot = sorted(records)
        bad_marker = AttendedRunner(self.root, self.config, self.api, slot=bad_slot)._handoff(
            records[bad_slot],
        ).packet.marker
        def malformed(operation, arguments):
            reply = native(operation, arguments)
            if operation == "observe":
                for row in reply["threads"]:
                    if row["marker"] == bad_marker:
                        row["thread_id"] = "malformed UUID"
            return reply
        loop.adapter = BridgeAdapter(malformed)
        packet = self.write_result(good_slot)
        self.native.stopped.add(packet.marker)
        batch = loop._wait(tuple(records), records)
        with self.assertRaisesRegex(RunnerError, "Malformed native observation"):
            loop._advance(bad_slot, records[bad_slot], batch)
        self.assertEqual(loop._advance(good_slot, records[good_slot], batch)["status"], "submitted")
        self.assertIn(bad_slot, loop._records())
        self.assertNotIn(good_slot, loop._records())
        self.assertEqual(len(self.api.submissions), 1)

    def test_cursor_retention_is_per_attempt_and_not_authority(self):
        loop = self.loop()
        self.spin(loop, lambda: len(self.native.waits) >= 4 and len(self.native.launches) == 2)
        self.assertTrue(any(wait["cursors"] for wait in self.native.waits))
        self.assertEqual(len(self.native.launches), 2)
        self.assertLessEqual(len(loop.cursors), 2)
        with loop.runner._journal() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM captain WHERE key LIKE 'cursor-%'").fetchone()[0], 0)
        restarted = self.loop()
        self.assertFalse(restarted.cursors)
        self.assertEqual(len(self.native.launches), 2)

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
        # The server ended the attempt on the lost accepted reply, so it is not listed as expired.
        loop.expired = {}
        loop._advance(1, runner._snapshot(), None)
        self.assertEqual(self.api.submissions[0], self.api.submissions[1])
        self.assertFalse(runner._snapshot())

    def test_fixture_subprocess_verifies_native_and_queued_receipts(self):
        fixture = self.directory / "fixture.json"
        project = {field: self.config["repositories"]["repo"][field]
                   for field in ("host_id", "project_id", "checkout")}
        fixture.write_text(json.dumps({"projects": [project], "queued": True}), encoding="utf-8")
        command = [sys.executable, str(Path(__file__).parent / "fixtures" / "captain_bridge.py"), str(fixture)]
        adapter = BridgeAdapter(CommandTransport(command))
        adapter.resolve_project(project)
        runner = AttendedRunner(self.root, self.config, self.api)
        launch = runner.prepare()
        receipt = adapter.launch(launch)
        self.assertIn("client_thread_id", receipt)
        runner.receipt(launch["marker"], "local", **receipt)
        packet = runner._handoff(runner._snapshot()).packet
        batch = adapter.observe([{
            "slot": 1, "marker": packet.marker, "project_id": "project", "host_id": "local",
            "started_at": runner._snapshot()["started_at"], "assignment": packet.result_identity(),
            "receipt": receipt,
        }], cursors={}, timeout=1)
        self.assertEqual(str(batch.threads[0].client_thread_id), receipt["client_thread_id"])
        with self.assertRaisesRegex(RunnerError, "failed with exit code"):
            adapter.launch(launch)
        with self.assertRaisesRegex(RunnerError, "unavailable"):
            CommandTransport([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.01)("observe", {})
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
            if operation == "observe":
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
                  == "completed" and any(event["status"] == "report_storage_submitted" for event in self.events))
        stages = [json.loads(launch["session"]["prompt"].split("Work packet:\n")[1].split(
            "\n\nWrite one UTF-8")[0])["result_identity"]["stage"] for launch in original.__self__.launches]
        self.assertEqual(stages, ["report_drafting", "report_review"])
        self.assertTrue(any(event["status"] == "report_storage_submitted" for event in self.events))

    def test_validation_stages_need_policy_and_github_token(self):
        loop = self.loop()
        self.assertNotIn("pre_pr_validation", loop._capabilities())
        self.config["validation_policy"] = str(self.directory / "policy.json")
        with patch.dict(os.environ, {"PHALANX_GITHUB_TOKEN": ""}):
            available = loop._capabilities()
        self.assertIn("pre_pr_validation", available)
        self.assertFalse({"pr_promotion", "pr_validation"} & set(available))
        with patch.dict(os.environ, {"PHALANX_GITHUB_TOKEN": "fixture-only"}):
            self.assertTrue(self.loop().rechecks)
            self.assertLessEqual({"pre_pr_validation", "pr_promotion", "pr_validation"},
                                 set(loop._capabilities()))

    def test_slow_runner_only_draft_keeps_worker_slot_and_renewals_usable(self):
        self.api.stages["first"] = "pr_creation"
        gate, entered = Event(), Event()
        def draft(runner, snapshot, token):
            entered.set()
            gate.wait(WAIT_SECONDS)
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

    def test_missing_project_mapping_does_not_filter_runner_only_repository(self):
        self.config["repositories"]["healthy"] = {**self.config["repositories"]["repo"],
                                                   "project_id": "healthy-project"}
        self.api.waiting = ["first"]
        self.api.stages["first"] = "report_storage"
        original_native = self.native.__call__
        def native(operation, arguments):
            if operation == "resolve_project" and arguments["project_id"] == "project":
                raise RunnerError("Runtime project unavailable")
            return original_native(operation, arguments)
        self.native = native
        original_call = self.api.call
        def call(route, body=None, **kwargs):
            if route == "/repositories":
                return [{"id": repo, "remote": str(self.checkout)} for repo in self.config["repositories"]]
            return original_call(route, body, **kwargs)
        self.api.call = call
        loop = self.loop()
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
                      == "cancelled" and not loop._records() and len(stop_posts) == 2)
        for _ in range(10):
            loop.step()
            time.sleep(0.01)
        self.assertEqual(stop_posts[0], stop_posts[1])
        self.assertFalse(self.native.launches)
        timeline = self.get(execution_route + "/timeline", OWNER)[1]
        self.assertEqual(sum(event["event"] == "stop_confirmed" for event in timeline), 1)
        self.assertEqual(self.get(execution_route + "/report", OWNER)[0], 200)

    def test_cancel_after_launch_confirms_stop_without_submitting_worker_result(self):
        self.config["capacity"] = 1
        self.start_server(self.directory / "launched-cancel.sqlite3")
        self.request("/v1/repositories", OWNER, {"id": "repo", "remote": str(self.checkout)})
        _, item = self.request("/v1/items", OWNER, {
            "repository_id": "repo", "goal": "Write a report", "target": "report",
        })
        self.api = RunnerAPI(self.url, RUNNER)
        loop = self.loop()
        self.spin(loop, lambda: bool(loop._records().get(1, {}).get("receipt")))
        packet = self.write_result(1)
        execution_route = f"/v1/items/{item['id']}/executions/{item['execution']['id']}"
        current = self.get(f"/v1/items/{item['id']}", OWNER)[1]["execution"]
        self.assertEqual(self.request(execution_route + "/cancel", OWNER, {
            "expected_version": current["version"], "reason": "Cancel the launched report writer",
        })[0], 200)
        self.native.stopped.add(packet.marker)
        call = self.api.call
        stop_posts, submissions = [], []
        def stop(route, body=None, **kwargs):
            if route.endswith("/result"):
                submissions.append(body)
            response = call(route, body, **kwargs)
            if route.endswith("/stop-confirmed"):
                snapshot = loop._records()[1]
                self.assertNotIn("submission", snapshot)
                self.assertIn("thread_id", snapshot["cancel_stop"])
                stop_posts.append((body.copy(), kwargs.copy()))
                if len(stop_posts) == 1:
                    raise RunnerError("Lost launched stop acknowledgment")
            return response
        self.api.call = stop
        self.spin(loop, lambda: not loop._records() and len(stop_posts) == 2)
        self.assertFalse(submissions)
        self.assertEqual(stop_posts[0], stop_posts[1])
        self.assertEqual(len(self.native.launches), 1)
        self.assertEqual(self.get(f"/v1/items/{item['id']}", OWNER)[1]["execution"]["state"]["status"],
                         "cancelled")
        self.assertEqual(self.get(execution_route + "/report", OWNER)[0], 200)
        with loop.runner._journal() as db:
            archive = json.loads(db.execute("SELECT record FROM completed").fetchone()[0])
        checkpoint = json.loads(archive["cancel_stop"]["proof"].split(". ", 1)[1])
        self.assertEqual(checkpoint["path"], str(packet.worktree))

    def rejected_submission(self):
        self.config["capacity"] = 1
        self.start_server(self.directory / "rejected-result.sqlite3")
        self.request("/v1/repositories", OWNER, {"id": "repo", "remote": str(self.checkout)})
        _, item = self.request("/v1/items", OWNER, {
            "repository_id": "repo", "goal": "Write a report", "target": "report",
        })
        self.api = RunnerAPI(self.url, RUNNER)
        loop = self.loop()
        loop._prepare(1)
        packet = self.write_result(1)
        route = f"/v1/items/{item['id']}/executions/{item['execution']['id']}"
        current = self.get(f"/v1/items/{item['id']}", OWNER)[1]["execution"]
        self.assertEqual(self.request(route + "/cancel", OWNER, {
            "expected_version": current["version"], "reason": "Cancel races with result publication",
        })[0], 200)
        runner = AttendedRunner(self.root, self.config, self.api)
        with self.assertRaisesRegex(RunnerError, "HTTP 409"):
            runner.submit(packet.marker, "local", runner._snapshot()["receipt"]["thread_id"])
        self.assertIn("submission", runner._snapshot())
        self.native.stopped.add(packet.marker)
        return loop, runner, item

    def test_rejected_submission_stop_archives_exactly_once(self):
        loop, runner, item = self.rejected_submission()
        snapshot = runner._snapshot()
        batch = loop._wait((1,), {1: snapshot})
        self.assertEqual(loop._advance(1, snapshot, batch)["status"], "stop_confirmed")
        self.assertFalse(runner._snapshot())
        with runner._journal() as db:
            rows = db.execute("SELECT record FROM completed").fetchall()
        self.assertEqual(len(rows), 1)
        archive = json.loads(rows[0][0])
        self.assertEqual(archive["submission"], snapshot["submission"])
        self.assertEqual(archive["stop_acknowledgment"]["state"]["status"], "cancelled")
        self.assertNotIn("lease", archive)

    def test_restart_observes_rejected_submission_and_finalizes_stop(self):
        loop, runner, item = self.rejected_submission()
        loop.close()
        self.assertFalse(self.native.waits)
        restarted = self.loop()
        self.spin(restarted, lambda: not runner._snapshot())
        self.assertTrue(self.native.waits)
        self.assertEqual(len(self.native.launches), 1)
        self.assertEqual(self.get(f"/v1/items/{item['id']}", OWNER)[1]["execution"]["state"]["status"],
                         "cancelled")

    def test_recovery_owner_wait_is_published_once_and_cleared_when_recovery_moves_on(self):
        loop = self.loop()
        owner_wait = {"status": "owner_wait", "reason": "Could not verify one exact native thread for the launch",
                      "next_action": "Inspect the original worker launch and workspace, then explicitly recheck"}
        loop._publish_recovery_wait(2, "attempt-a", owner_wait)
        loop._publish_recovery_wait(2, "attempt-a", owner_wait)
        self.assertEqual(len(self.api.recovery_waits), 1)
        attempt, blocker = self.api.recovery_waits[0]
        self.assertEqual(attempt, "attempt-a")
        self.assertTrue(blocker.startswith("Recovery is waiting on the owner: Could not verify"))
        self.assertIn(f"attended-recover --config <runner config> --state {self.root} --slot 2 --recheck", blocker)
        loop._publish_recovery_wait(2, "attempt-a", {"status": "retry", "reason": "Rechecking"})
        loop._publish_recovery_wait(2, "attempt-a", {"status": "retry", "reason": "Rechecking"})
        loop._publish_recovery_wait(2, "attempt-b", {"status": "retry", "reason": "Rechecking"})
        self.assertEqual(self.api.recovery_waits[1:], [("attempt-a", None)])

    def test_expired_prepared_packet_without_relay_recovers_without_observation(self):
        from contextlib import closing
        import sqlite3
        self.config["capacity"] = 1
        database = self.directory / "expired-packet.sqlite3"
        self.start_server(database)
        self.request("/v1/repositories", OWNER, {"id": "repo", "remote": str(self.checkout)})
        _, item = self.request("/v1/items", OWNER, {
            "repository_id": "repo", "goal": "Write a report", "target": "report",
        })
        self.api = RunnerAPI(self.url, RUNNER)
        runner = AttendedRunner(self.root, self.config, self.api)
        runner.prepare()
        snapshot = runner._snapshot()
        self.assertIn("packet", snapshot)
        self.assertNotIn("relay", snapshot)
        with closing(sqlite3.connect(database)) as db, db:
            db.execute("UPDATE attempts SET lease_expires_at=0 WHERE id=?",
                       (snapshot["claim"]["attempt_id"],))
        runner._transaction(lambda record, db: record.update(lease_expires_at=0))
        loop = self.loop()
        expired = self.api.call("/dispatch/runners/desk/recovery")
        loop.expired = {row["attempt_id"]: row for row in expired}
        self.assertEqual(loop._advance(1, runner._snapshot(), None)["status"], "recovered")
        self.assertFalse(runner._snapshot())
        self.assertFalse(self.native.launches)
        self.assertFalse(self.native.waits)
        self.assertEqual(self.get(f"/v1/items/{item['id']}", OWNER)[1]["execution"]["state"]["status"],
                         "queued")

    def test_restart_adopts_expired_attempt_before_and_after_receipt_without_redraft(self):
        for before_receipt in (True, False):
            with self.subTest(before_receipt=before_receipt):
                self.adopt_after_restart(before_receipt)

    def adopt_after_restart(self, before_receipt):
        self.config["capacity"] = 1
        root = self.root = self.directory / f"runner-{before_receipt}"
        self.native = NativeFixture()
        self.events.clear()
        database = self.directory / f"adopt-{before_receipt}.sqlite3"
        server = self.start_server(database)
        self.request("/v1/repositories", OWNER, {"id": "repo", "remote": str(self.checkout)})
        _, item = self.request("/v1/items", OWNER, {
            "repository_id": "repo", "goal": "Write a report", "target": "report",
        })
        self.api = RunnerAPI(self.url, RUNNER)
        self.native.fail_launch = before_receipt
        loop = self.loop()
        self.spin(loop, lambda: len(self.native.launches) == 1 and (
            before_receipt or bool(loop._records()[1].get("receipt"))))
        loop.close()
        runner = AttendedRunner(root, self.config, self.api)
        snapshot = runner._snapshot()
        self.assertEqual(bool(snapshot.get("receipt")), not before_receipt)
        old = snapshot["lease"]
        with closing(sqlite3.connect(database)) as db, db:
            db.execute("UPDATE attempts SET lease_expires_at=0 WHERE id=?",
                       (snapshot["claim"]["attempt_id"],))
        self.native.fail_launch = False
        restarted = self.loop()
        self.spin(restarted, lambda: any(event["status"] == "adopted" for event in self.events))
        self.assertNotIn(runner._snapshot()["lease"], (None, old))
        self.assertEqual(len(self.native.launches), 1)
        self.assertEqual(runner.renew()["attempt_id"], snapshot["claim"]["attempt_id"])
        packet = self.write_result(1)
        self.native.stopped.add(packet.marker)
        self.spin(restarted, lambda: ((runner._snapshot().get("claim") or {}).get("execution") or {})
                  .get("state", {}).get("stage") == "report_review")
        restarted.close()
        execution = self.get(f"/v1/items/{item['id']}", OWNER)[1]["execution"]
        self.assertEqual(execution["state"]["stage"], "report_review")
        with closing(sqlite3.connect(database)) as db:
            self.assertEqual(db.execute(
                "SELECT count(*) FROM attempts WHERE stage='report_drafting'").fetchone()[0], 1)
        self.stop_server(server)

    def test_recheck_cli_releases_unlaunched_slot_only_after_no_launch_proof(self):
        from argparse import Namespace
        from io import StringIO
        from phalanx.attended_recovery_service import run_recovery_command
        self.config["capacity"] = 1
        database = self.directory / "recheck.sqlite3"
        self.start_server(database)
        self.request("/v1/repositories", OWNER, {"id": "repo", "remote": str(self.checkout)})
        _, item = self.request("/v1/items", OWNER, {
            "repository_id": "repo", "goal": "Write a report", "target": "report",
        })
        self.api = RunnerAPI(self.url, RUNNER)
        runner = AttendedRunner(self.root, self.config, self.api)
        runner.prepare()
        snapshot = runner._snapshot()
        with closing(sqlite3.connect(database)) as db, db:
            db.execute("UPDATE attempts SET lease_expires_at=0 WHERE id=?",
                       (snapshot["claim"]["attempt_id"],))
        config = self.directory / "runner.json"
        config.write_text(json.dumps(self.config | {"base_url": self.url}), encoding="utf-8")
        args = Namespace(config=config, state=self.root, slot=1, observation=None, recheck=True)

        def recover():
            with patch.dict(os.environ, {"PHALANX_RUNNER_TOKEN": RUNNER}), \
                    patch("sys.stdout", new_callable=StringIO) as output:
                run_recovery_command(args)
            return json.loads(output.getvalue())

        workspace = Path(snapshot["packet"]["worktree"])
        (workspace / "unrecorded.txt").write_text("Preserve this change", encoding="utf-8")
        with self.assertRaisesRegex(RunnerError, "unaccounted changes"):
            recover()
        (workspace / "unrecorded.txt").unlink()
        runner._transaction(lambda record, db: record.update(relay={"status": "started"}))
        self.assertEqual(recover()["status"], "retry")
        self.assertTrue(runner._snapshot())
        runner._transaction(lambda record, db: record.pop("relay") and record.pop("recovery"))
        self.assertEqual(recover()["status"], "recovered")
        self.assertFalse(runner._snapshot())
        self.assertEqual(self.get(f"/v1/items/{item['id']}", OWNER)[1]["execution"]["state"]["status"],
                         "queued")

    def test_unlaunched_cancel_accepts_clean_ancestor_after_origin_advances(self):
        self.config["capacity"] = 1
        self.api.waiting = ["first"]
        prepare, call = WorkspaceManager.prepare, self.api.call
        current, stop_posts = {}, []
        def cancelled_prepare(manager, item_id, **kwargs):
            workspace = prepare(manager, item_id, **kwargs)
            subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.test",
                            "commit", "--allow-empty", "-m", "Advance origin base"], cwd=self.checkout,
                           check=True, capture_output=True)
            subprocess.run(["git", "update-ref", "refs/remotes/origin/main", "HEAD"],
                           cwd=self.checkout, check=True, capture_output=True)
            claim = next(iter(self.api.claims.values()))
            current.update(id=claim["execution"]["id"], version=claim["execution"]["version"] + 1,
                           state={"stage": "report_drafting", "status": "waiting_for_external",
                                  "stopping": "cancelled"})
            return workspace
        def stop(route, body=None, **kwargs):
            if route == "/items/first" and current:
                return {"execution": current}
            if route.endswith("/stop-confirmed"):
                stop_posts.append(body)
                return {"id": current["id"], "version": current["version"] + 2,
                        "state": {"status": "cancelled", "final_output": {
                            "reference": "/v1" + route.removesuffix("/stop-confirmed") + "/report",
                        }}}
            return call(route, body, **kwargs)
        self.api.call = stop
        loop = self.loop()
        with patch.object(WorkspaceManager, "prepare", cancelled_prepare):
            with self.assertRaisesRegex(RunnerError, "changed before launch"):
                loop._prepare(1)
            self.spin(loop, lambda: bool(stop_posts) and not loop._records())
        self.assertEqual(len(stop_posts), 1)
        self.assertFalse(self.native.launches)

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
        future = loop.native_pool.submit(CommandTransport(command, timeout=2), "observe", {})
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
            loop._confirm_stop(runner, current)
        self.assertNotIn("cancel_stop", runner._snapshot())
        manager = runner._manager(self.config["repositories"]["repo"], str(self.checkout))
        workspace = manager.inspect("first")
        (workspace.path / "unrecorded.txt").unlink()
        proof = unlaunched_proof(runner, runner._snapshot())
        runner._transaction(lambda record, db: record.update(cancel_stop={
            "proof": proof, "key": "saved-stop", "disposition": "cancelled",
            "body": {"attempt_id": record["claim"]["attempt_id"], "stage": "report_drafting",
                     "expected_version": current["version"]},
        }))
        (workspace.path / "unrecorded.txt").write_text("New effects after the first stop request")
        with self.assertRaisesRegex(RunnerError, "unaccounted changes"):
            loop._confirm_stop(runner)
        runner._transaction(lambda record, db: record.update(relay={"status": "started"}))
        with self.assertRaisesRegex(RunnerError, "ambiguous launch"):
            loop._confirm_stop(runner, current)
        self.assertTrue(runner._snapshot())

    def test_identity_and_stopped_attestation_are_rejected(self):
        started_at = datetime.now(timezone.utc).isoformat()
        adapter = BridgeAdapter(lambda operation, arguments: {
            "host_id": "wrong", "project_id": "project", "checkout": str(self.checkout),
        })
        with self.assertRaisesRegex(RunnerError, "exact host"):
            adapter.resolve_project(self.config["repositories"]["repo"])
        adapter = BridgeAdapter(lambda operation, arguments: {
            "observed_at": datetime.now(timezone.utc).isoformat(), "discovery_complete": True,
            "threads": [{"host_id": "local", "project_id": "project", "marker": "marker",
                         "thread_id": str(uuid4()), "started_at": started_at,
                         "assignment": {}, "state": "stopped"}],
        })
        batch = adapter.observe([{"marker": "marker"}], cursors={}, timeout=1)
        self.assertFalse(batch.threads)
        self.assertIn("marker", batch.errors)
        self.assertIn("attestation", batch.errors["marker"])
        thread = str(uuid4())
        for url, expected in (("https://example.test/chat/1", {"worker_url": "https://example.test/chat/1"}),
                              ("javascript:alert(1)", {"warning": ANY})):
            adapter = BridgeAdapter(lambda operation, arguments: {
                "host_id": "local", "marker": "marker", "thread_id": thread, "worker_url": url,
            })
            launched = adapter.launch({"host_id": "local", "marker": "marker", "session": {}})
            self.assertEqual(launched, {"thread_id": thread, **expected})
        adapter = BridgeAdapter(lambda operation, arguments: {
            "host_id": "local", "marker": "marker", "thread_id": str(uuid4()),
        })
        with self.assertRaisesRegex(RunnerError, "exact host, writer"):
            adapter.resume({"host_id": "local", "project_id": "project", "thread_id": thread,
                            "marker": "marker", "prompt": "Continue"})
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

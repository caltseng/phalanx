"""One bridge contract, run against the fixture subprocess and the Captain chat relay."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event, Thread, get_ident
from time import sleep
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

from phalanx.captain_adapter import BridgeAdapter, CommandTransport, LaunchNotDelivered, RequestRetracted
from phalanx.captain_relay import RelayTransport, _queue, respond, take_next
from phalanx.observe import build_reply
from phalanx.runner_errors import RunnerError
from timing import Clock, WAIT_SECONDS


ROOT = Path(__file__).resolve().parents[1]
BRIDGE = Path(__file__).parent / "fixtures" / "captain_bridge.py"


class BridgeContract:
    """Scenarios every production or fixture bridge must satisfy."""

    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.checkout = self.directory / "checkout"
        self.checkout.mkdir()
        self.project = {"host_id": "local", "project_id": "project", "checkout": str(self.checkout)}
        self.fixture = self.directory / "fixture.json"
        self.configure()

    def configure(self, **options):
        self.fixture.write_text(json.dumps({"projects": [self.project], **options}), encoding="utf-8")

    def launch(self, adapter):
        marker = f"phalanx-attempt-{uuid4()}"
        return marker, adapter.launch({"host_id": "local", "marker": marker,
                                       "session": {"prompt": "Run the packet"}})

    def test_resolve_project_verifies_exact_mapping(self):
        adapter = self.adapter()
        adapter.resolve_project(self.project)
        with self.assertRaises(RunnerError):
            adapter.resolve_project({**self.project, "project_id": "other"})

    def test_launch_returns_native_or_queued_receipt_once(self):
        adapter = self.adapter()
        marker, receipt = self.launch(adapter)
        UUID(receipt["thread_id"])
        with self.assertRaises(RunnerError):
            adapter.launch({"host_id": "local", "marker": marker, "session": {}})
        self.configure(queued=True)
        _, queued = self.launch(adapter)
        self.assertEqual(set(queued), {"client_thread_id"})

    def test_observe_discovers_launched_marker(self):
        adapter = self.adapter()
        marker, receipt = self.launch(adapter)
        assignment = {"item_id": "item", "attempt_id": "attempt"}
        batch = adapter.observe([{
            "slot": 1, "marker": marker, "project_id": "project", "host_id": "local",
            "started_at": datetime.now(timezone.utc).isoformat(), "assignment": assignment, "receipt": receipt,
        }], cursors={}, timeout=1)
        self.assertTrue(batch.discovery_complete)
        self.assertEqual([(str(row.thread_id), row.marker, row.assignment, row.state) for row in batch.threads],
                         [(receipt["thread_id"], marker, assignment, "active")])
        self.assertEqual(batch.cursors, {marker: "fixture-next"})


class FixtureBridgeContractTests(BridgeContract, unittest.TestCase):
    def adapter(self):
        return BridgeAdapter(CommandTransport([sys.executable, str(BRIDGE), str(self.fixture)]))


def relay(*args, stdin=None):
    return subprocess.run([sys.executable, "-m", "phalanx", "relay", *args], cwd=ROOT, input=stdin,
                          capture_output=True, text=True, timeout=2 * WAIT_SECONDS)


class RelayBridgeContractTests(BridgeContract, unittest.TestCase):
    """A simulated Captain chat drives the relay CLI and runs the fixture as its app tools."""

    def adapter(self):
        state, stop = self.directory / "runner", Event()

        def captain():
            while not stop.is_set():
                taken = relay("--state", str(state), "next", "--wait", "0.2")
                request = json.loads(taken.stdout)
                if "id" not in request:
                    continue
                request_id = request.pop("id")
                tool = subprocess.run([sys.executable, str(BRIDGE), str(self.fixture)], input=json.dumps(request),
                                      capture_output=True, text=True, timeout=WAIT_SECONDS)
                if tool.returncode:
                    relay("--state", str(state), "respond", request_id, "--error", "app tool failed")
                else:
                    reply = self.directory / f"{request_id}.json"
                    reply.write_text(tool.stdout, encoding="utf-8")
                    relay("--state", str(state), "respond", request_id, "--file", str(reply))

        thread = Thread(target=captain, daemon=True)
        thread.start()
        self.addCleanup(thread.join, WAIT_SECONDS)
        self.addCleanup(stop.set)
        return BridgeAdapter(RelayTransport(state, timeout=WAIT_SECONDS, poll=0.02))


class RelayQueueTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def clock(self):
        """Age relay requests on demand, so expiry never depends on how fast a loaded host runs."""
        clock = Clock()
        patcher = patch("phalanx.captain_relay.time", clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        return clock

    def call(self, transport, results, *args):
        try:
            results.append(transport(*args))
        except RunnerError as error:
            results.append(error)

    def test_untaken_request_expires_as_undelivered_and_is_never_handed_out(self):
        with self.assertRaisesRegex(LaunchNotDelivered, "never took launch"):
            RelayTransport(self.root, timeout=0.1, poll=0.01)("launch", {"marker": "m"})
        self.assertEqual(take_next(self.root, wait=0), {"status": "idle"})

    def test_taken_request_is_never_resent_after_captain_restart(self):
        clock, results = self.clock(), []
        waiter = Thread(target=self.call, args=(RelayTransport(self.root, timeout=60, poll=0.01), results,
                                                "launch", {"marker": "m"}))
        waiter.start()
        taken = take_next(self.root, wait=WAIT_SECONDS, poll=0.01)
        self.assertEqual(taken["operation"], "launch")
        clock.advance(120)
        waiter.join(WAIT_SECONDS)
        self.assertIsInstance(results[0], RunnerError)
        self.assertNotIsInstance(results[0], LaunchNotDelivered)
        self.assertEqual(take_next(self.root, wait=0), {"status": "idle"})
        with self.assertRaisesRegex(RunnerError, "unknown or settled"):
            respond(self.root, taken["id"], response={})

    def test_unanswered_observation_is_settled_and_retry_reaches_a_fresh_captain(self):
        clock = self.clock()
        transport = RelayTransport(self.root, timeout=60, poll=0.01)
        arguments = {"references": [{"marker": "m"}]}
        results = []
        waiter = Thread(target=self.call, args=(transport, results, "observe", arguments))
        waiter.start()
        take_next(self.root, wait=WAIT_SECONDS, poll=0.01)
        clock.advance(120)
        waiter.join(WAIT_SECONDS)
        self.assertRegex(str(results[0]), "never answered")
        retry = Thread(target=self.call, args=(transport, results, "observe", arguments))
        retry.start()
        fresh = take_next(self.root, wait=WAIT_SECONDS, poll=0.01)
        self.assertEqual(fresh["arguments"], arguments)
        respond(self.root, fresh["id"], response={"threads": []})
        retry.join(WAIT_SECONDS)
        self.assertEqual(results[1], {"threads": []})

    def test_relay_output_is_ascii_json_that_round_trips(self):
        arguments = {"prompt": "Wait\u2026 then reply \u2014 caf\u00e9"}
        results = []
        waiter = Thread(target=self.call, args=(RelayTransport(self.root, timeout=WAIT_SECONDS, poll=0.01), results,
                                                "launch", arguments))
        waiter.start()
        taken = relay("--state", str(self.root), "next", "--wait", str(WAIT_SECONDS))
        self.assertEqual(taken.returncode, 0, taken.stderr)
        self.assertTrue(taken.stdout.isascii())
        request = json.loads(taken.stdout)
        self.assertEqual(request["arguments"], arguments)
        respond(self.root, request["id"], response={})
        waiter.join(WAIT_SECONDS)

    def test_restarted_loop_expires_orphaned_pending_requests(self):
        with _queue(self.root) as db:
            db.execute("INSERT INTO request VALUES ('old', 'k', 'launch', '{}', 'pending', NULL, NULL, 0, 0)")
        RelayTransport(self.root, timeout=1)
        self.assertEqual(take_next(self.root, wait=0), {"status": "idle"})

    def test_identical_waiters_share_one_request_and_resolution_is_cached(self):
        transport = RelayTransport(self.root, timeout=WAIT_SECONDS, poll=0.01)
        waiting, results = set(), []
        def sleep(seconds):
            waiting.add(get_ident())
        threads = [Thread(target=self.call, args=(transport, results, "resolve_project", {"project_id": "p"}))
                   for _ in range(2)]
        with patch("phalanx.captain_relay.sleep", sleep):
            for thread in threads:
                thread.start()
            while len(waiting) < 2:
                pass
            taken = take_next(self.root, wait=0)
            self.assertEqual(take_next(self.root, wait=0), {"status": "idle"})
            respond(self.root, taken["id"], response={"project_id": "p"})
            for thread in threads:
                thread.join(WAIT_SECONDS)
        self.assertEqual(results, [{"project_id": "p"}] * 2)
        self.assertEqual(transport("resolve_project", {"project_id": "p"}), {"project_id": "p"})
        self.assertEqual(take_next(self.root, wait=0), {"status": "idle"})

    def test_failed_resolution_is_cached_briefly_without_a_new_request(self):
        transport = RelayTransport(self.root, timeout=WAIT_SECONDS, poll=0.01)
        results = []
        waiter = Thread(target=self.call, args=(transport, results, "resolve_project", {"project_id": "p"}))
        waiter.start()
        taken = take_next(self.root, wait=WAIT_SECONDS, poll=0.01)
        respond(self.root, taken["id"], error="checkout is untrusted; trust it once")
        waiter.join(WAIT_SECONDS)
        self.assertRegex(str(results[0]), "untrusted")
        with self.assertRaisesRegex(RunnerError, "untrusted"):
            transport("resolve_project", {"project_id": "p"})
        self.assertEqual(take_next(self.root, wait=0), {"status": "idle"})
        with patch("phalanx.captain_relay.RESOLVE_FAILURE_CACHE_SECONDS", 0):
            retry = Thread(target=self.call, args=(transport, results, "resolve_project", {"project_id": "p"}))
            retry.start()
            fresh = take_next(self.root, wait=WAIT_SECONDS, poll=0.01)
            respond(self.root, fresh["id"], response={"project_id": "p"})
            retry.join(WAIT_SECONDS)
        self.assertEqual(results[1], {"project_id": "p"})

    def test_cli_rejects_invalid_reply_and_reports_tool_error(self):
        results = []
        waiter = Thread(target=self.call, args=(RelayTransport(self.root, timeout=WAIT_SECONDS, poll=0.01), results,
                                                "observe", {"references": []}))
        waiter.start()
        taken = json.loads(relay("--state", str(self.root), "next", "--wait", str(WAIT_SECONDS)).stdout)
        self.assertEqual(taken["contract_version"], 2)
        invalid = relay("--state", str(self.root), "respond", taken["id"], stdin="[")
        self.assertEqual(invalid.returncode, 1, invalid.stderr)
        self.assertIn("not valid JSON", invalid.stderr)
        self.assertEqual(relay("--state", str(self.root), "respond", taken["id"], "--error", "no app").returncode, 0)
        waiter.join(WAIT_SECONDS)
        self.assertRegex(str(results[0]), "observe failed: no app")

    def test_pending_observation_is_retracted_once_its_marker_stops_observing(self):
        transport = RelayTransport(self.root, timeout=WAIT_SECONDS, poll=0.01)
        results = []
        waiters = [Thread(target=self.call, args=(transport, results, "observe",
                                                  {"references": [{"marker": marker}]}))
                   for marker in ("kept", "gone")]
        for waiter in waiters:
            waiter.start()
        def pending():
            with _queue(self.root) as db:
                return db.execute("SELECT COUNT(*) FROM request WHERE status='pending'").fetchone()[0]
        while pending() < 2:
            sleep(0.01)
        transport.retract_observations({"kept"})
        waiters[1].join(WAIT_SECONDS)
        self.assertIsInstance(results[0], RequestRetracted)
        taken = take_next(self.root, wait=0)
        self.assertEqual(taken["arguments"], {"references": [{"marker": "kept"}]})
        respond(self.root, taken["id"], response={"threads": []})
        waiters[0].join(WAIT_SECONDS)
        self.assertEqual(results[1], {"threads": []})

    def test_stall_survives_requeue_and_clears_once_a_server_polls(self):
        transport = RelayTransport(self.root, timeout=0.1, poll=0.01)
        self.assertIsNone(transport.stall(0))
        with self.assertRaises(LaunchNotDelivered):
            transport("observe", {"references": []})
        with self.assertRaises(LaunchNotDelivered):
            transport("observe", {"references": [{"marker": "m"}]})
        stall = transport.stall(0.15)
        self.assertRegex(stall, "taken no request since .*Z; start or wake the relay server")
        self.assertIsNone(transport.stall(WAIT_SECONDS))
        take_next(self.root, wait=0)
        self.assertIsNone(transport.stall(0))

    def test_overlapping_servers_take_each_request_exactly_once(self):
        # Polls are coarse enough that 22 threads don't starve each other of the queue lock.
        transport = RelayTransport(self.root, timeout=WAIT_SECONDS, poll=0.05)
        results, taken, stop = [], [], Event()
        def server():
            while not stop.is_set():
                request = take_next(self.root, wait=0.5, poll=0.01)
                if "id" in request:
                    taken.append(request["id"])
                    respond(self.root, request["id"], response=request["arguments"])
        servers = [Thread(target=server) for _ in range(2)]
        waiters = [Thread(target=self.call, args=(transport, results, "observe", {"references": [{"marker": str(n)}]}))
                   for n in range(20)]
        for thread in servers + waiters:
            thread.start()
        for waiter in waiters:
            waiter.join(WAIT_SECONDS)
        stop.set()
        for thread in servers:
            thread.join(WAIT_SECONDS)
        self.assertEqual(len(taken), 20)
        self.assertEqual(len(set(taken)), 20)
        self.assertEqual(sorted(result["references"][0]["marker"] for result in results),
                         sorted(str(n) for n in range(20)))


def worker_prompt(marker, identity):
    packet = json.dumps({"result_identity": identity, "goal": "Fix it"}, indent=2)
    return f"Phalanx attempt marker: {marker}  \n\nExecute the stage.\n\nWork packet:\n{packet}\n\nWrite JSON.\n"


class ObserveHelperTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.identity = {"item_id": "item", "attempt_id": "attempt", "expected_revision": 3}
        self.reference = {"slot": 1, "host_id": "local", "project_id": "project", "marker": "phalanx-attempt-a",
                          "started_at": "2025-11-21T10:00:00+00:00", "assignment": self.identity, "receipt": {}}

    def request(self, references):
        return {"id": "r", "operation": "observe", "arguments": {"references": references}}

    def test_reply_matches_marker_and_assignment_and_passes_bridge_validation(self):
        thread, other = str(uuid4()), str(uuid4())
        sessions = [
            {"thread_id": thread, "project_id": "project", "state": "stopped",
             "prompt": worker_prompt("phalanx-attempt-a", self.identity), "started_at": "2025-11-21T10:00:01+00:00"},
            {"thread_id": other, "project_id": "project", "state": "active",
             "prompt": worker_prompt("phalanx-attempt-a", {**self.identity, "expected_revision": "3"}),
             "started_at": "2025-11-21T10:00:02+00:00"},
        ]
        reply = build_reply(self.request([self.reference]), sessions, host_id="local", complete=True)
        batch = BridgeAdapter(lambda operation, arguments: reply).observe([self.reference], cursors={}, timeout=1)
        self.assertTrue(batch.discovery_complete)
        self.assertEqual([(str(row.thread_id), row.state, row.assignment) for row in batch.threads],
                         [(thread, "stopped", self.identity)])
        self.assertEqual(build_reply(self.request([self.reference]), sessions, host_id="other",
                                     complete=False)["threads"], [])

    def test_relay_observe_reads_copilot_transcripts_and_writes_a_reply(self):
        thread = str(uuid4())
        transcript = self.root / "home" / ".copilot" / "session-state" / thread / "events.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text("\n".join(json.dumps(event) for event in [
            {"type": "session.start", "timestamp": "2025-11-21T10:00:01.000Z",
             "data": {"sessionId": thread, "startTime": "2025-11-21T10:00:01.000Z"}},
            {"type": "user.message", "timestamp": "2025-11-21T10:00:02.000Z",
             "data": {"content": worker_prompt("phalanx-attempt-a", self.identity)}},
        ]) + "\n", encoding="utf-8")
        state, results = self.root / "runner", []
        adapter = BridgeAdapter(RelayTransport(state, timeout=WAIT_SECONDS, poll=0.01))

        def observe():
            results.append(adapter.observe([self.reference], cursors={}, timeout=1))
        waiter = Thread(target=observe)
        waiter.start()
        taken = take_next(state, wait=WAIT_SECONDS, poll=0.01)
        out = self.root / "reply.json"
        env = {**os.environ, "HOME": str(self.root / "home"), "USERPROFILE": str(self.root / "home")}
        written = subprocess.run(
            [sys.executable, "-m", "phalanx", "relay", "--state", str(state), "observe", taken["id"],
             "--runtime", "copilot", "--host", "local", "--session", thread, "active", "project",
             "--complete", "--out", str(out)],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=WAIT_SECONDS)
        self.assertEqual(written.returncode, 0, written.stderr)
        self.assertEqual(relay("--state", str(state), "respond", taken["id"], "--file", str(out)).returncode, 0)
        waiter.join(WAIT_SECONDS)
        self.assertEqual([(str(row.thread_id), row.state) for row in results[0].threads], [(thread, "active")])
        unknown = relay("--state", str(state), "observe", taken["id"], "--runtime", "other", "--host", "local")
        self.assertNotEqual(unknown.returncode, 0)


if __name__ == "__main__":
    unittest.main()

"""One desktop bridge contract, run against the fixture subprocess and the Captain chat relay."""

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event, Thread, get_ident
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

from phalanx.captain_adapter import CodexCLIAdapter, CommandTransport, LaunchNotDelivered
from phalanx.captain_relay import RelayTransport, respond, take_next
from phalanx.runner_errors import RunnerError


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
                                       "create_thread": {"prompt": "Run the packet"}})

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
            adapter.launch({"host_id": "local", "marker": marker, "create_thread": {}})
        self.configure(queued=True)
        _, queued = self.launch(adapter)
        self.assertEqual(set(queued), {"client_thread_id"})

    def test_wait_threads_discovers_launched_marker(self):
        adapter = self.adapter()
        marker, receipt = self.launch(adapter)
        assignment = {"item_id": "item", "attempt_id": "attempt"}
        batch = adapter.wait_threads([{
            "slot": 1, "marker": marker, "project_id": "project", "host_id": "local",
            "started_at": datetime.now(timezone.utc).isoformat(), "assignment": assignment, "receipt": receipt,
        }], cursors={}, timeout=1)
        self.assertTrue(batch.discovery_complete)
        self.assertEqual([(str(row.thread_id), row.marker, row.assignment, row.state) for row in batch.threads],
                         [(receipt["thread_id"], marker, assignment, "active")])
        self.assertEqual(batch.cursors, {marker: "fixture-next"})


class FixtureBridgeContractTests(BridgeContract, unittest.TestCase):
    def adapter(self):
        return CodexCLIAdapter(CommandTransport([sys.executable, str(BRIDGE), str(self.fixture)]))


def relay(*args, stdin=None):
    return subprocess.run([sys.executable, "-m", "phalanx", "relay", *args], cwd=ROOT, input=stdin,
                          capture_output=True, text=True, timeout=60)


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
                                      capture_output=True, text=True, timeout=60)
                if tool.returncode:
                    relay("--state", str(state), "respond", request_id, "--error", "app tool failed")
                else:
                    reply = self.directory / f"{request_id}.json"
                    reply.write_text(tool.stdout, encoding="utf-8")
                    relay("--state", str(state), "respond", request_id, "--file", str(reply))

        thread = Thread(target=captain, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 60)
        self.addCleanup(stop.set)
        return CodexCLIAdapter(RelayTransport(state, timeout=60, poll=0.02))


class RelayQueueTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def call(self, transport, results, *args):
        try:
            results.append(transport(*args))
        except RunnerError as error:
            results.append(error)

    def test_untaken_request_expires_as_undelivered_and_is_never_handed_out(self):
        with self.assertRaisesRegex(LaunchNotDelivered, "never took create_thread"):
            RelayTransport(self.root, timeout=0.1, poll=0.01)("create_thread", {"marker": "m"})
        self.assertEqual(take_next(self.root, wait=0), {"status": "idle"})

    def test_taken_request_is_never_resent_after_captain_restart(self):
        results = []
        waiter = Thread(target=self.call, args=(RelayTransport(self.root, timeout=0.3, poll=0.01), results,
                                                "create_thread", {"marker": "m"}))
        waiter.start()
        taken = take_next(self.root, wait=10, poll=0.01)
        self.assertEqual(taken["operation"], "create_thread")
        waiter.join(10)
        self.assertIsInstance(results[0], RunnerError)
        self.assertNotIsInstance(results[0], LaunchNotDelivered)
        self.assertEqual(take_next(self.root, wait=0), {"status": "idle"})
        self.assertEqual(respond(self.root, taken["id"], response={}), {"status": "recorded", "id": taken["id"]})
        with self.assertRaisesRegex(RunnerError, "unknown or settled"):
            respond(self.root, taken["id"], error="again")

    def test_identical_waiters_share_one_request_and_resolution_is_cached(self):
        transport = RelayTransport(self.root, timeout=30, poll=0.01)
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
                thread.join(10)
        self.assertEqual(results, [{"project_id": "p"}] * 2)
        self.assertEqual(transport("resolve_project", {"project_id": "p"}), {"project_id": "p"})
        self.assertEqual(take_next(self.root, wait=0), {"status": "idle"})

    def test_cli_rejects_invalid_reply_and_reports_tool_error(self):
        results = []
        waiter = Thread(target=self.call, args=(RelayTransport(self.root, timeout=30, poll=0.01), results,
                                                "wait_threads", {"references": []}))
        waiter.start()
        taken = json.loads(relay("--state", str(self.root), "next", "--wait", "10").stdout)
        self.assertEqual(taken["contract_version"], 1)
        invalid = relay("--state", str(self.root), "respond", taken["id"], stdin="[")
        self.assertEqual(invalid.returncode, 1, invalid.stderr)
        self.assertIn("not valid JSON", invalid.stderr)
        self.assertEqual(relay("--state", str(self.root), "respond", taken["id"], "--error", "no app").returncode, 0)
        waiter.join(10)
        self.assertRegex(str(results[0]), "wait_threads failed: no app")


if __name__ == "__main__":
    unittest.main()

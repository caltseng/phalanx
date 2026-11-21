import json
import sqlite3
import unittest
from contextlib import closing
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from phalanx.attended import AttendedRunner, RunnerError
from phalanx.attended_recovery import NativeObservation
from phalanx.attended_recovery_service import RecoveryJournal, run_recovery
from phalanx.handoff import WorkPacket
from phalanx.workspaces import Workspace


class RecoveryAPI:
    def __init__(self, test):
        self.test = test
        self.expired = [test.expired]
        self.requests = []
        self.response = {"id": "execution", "version": 4, "state": {"status": "queued"}}
        self.claims = []
        self.claim_response = {"attempt_id": test.packet.attempt_id.hex, "execution_id": "execution",
                               "version": 3, "lease_expires_at": 500}
        self.error = None

    def call(self, path, body=None, *, lease=None, key=None):
        prefix = "/dispatch/runners/desk"
        if path == prefix + "/recovery":
            return self.expired
        record, _ = self.test.journal.snapshot()
        if path.endswith("/recover-claim"):
            self.test.assertEqual(path, f"{prefix}/attempts/{self.test.packet.attempt_id.hex}/recover-claim")
            self.test.assertEqual(record["recover_claim"], {
                "attempt_id": self.test.packet.attempt_id.hex, "body": body, "key": key, "lease": lease,
            })
            self.claims.append((body, key, lease))
            if self.error is not None:
                raise RunnerError(self.error)
            return self.claim_response
        self.test.assertEqual(path, f"{prefix}/attempts/{self.test.packet.attempt_id.hex}/recover-stopped")
        self.test.assertEqual(record["recover_stop"], {
            "attempt_id": self.test.packet.attempt_id.hex, "body": body, "key": key,
        })
        self.requests.append((body, key))
        if self.error is not None:
            raise RunnerError(self.error)
        return self.response


class RecoveryServiceTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        (self.root / "results").mkdir()
        self.journal = RecoveryJournal(self.root, 1)
        now = datetime.now(UTC)
        self.packet = WorkPacket("item", uuid4(), 2, "report_drafting", "repo",
                                 self.root / "tree", "phalanx/item", "base", "goal", "report", "body")
        self.native = NativeObservation("local", uuid4(), "project", now, self.packet.marker,
                                        self.packet.result_identity(), "stopped")
        self.record = {
            "runner_id": "desk", "lease": "local-secret", "host_id": "local",
            "project_id": "project", "started_at": now.isoformat(), "checkout": str(self.root),
            "repositories": {"repo": "remote"},
            "claim": {"attempt_id": self.packet.attempt_id.hex, "item_id": "item",
                      "execution": {"id": "execution", "version": 2,
                                    "state": {"stage": self.packet.stage}}},
            "packet": {**asdict(self.packet), "attempt_id": str(self.packet.attempt_id),
                       "worktree": str(self.packet.worktree)},
        }
        self.expired = {"attempt_id": self.packet.attempt_id.hex, "execution_id": "execution",
                        "item_id": "item", "stage": self.packet.stage, "version": 3}
        self.api = RecoveryAPI(self)
        self.journal.snapshot()
        self.seed()
        self.workspace = Workspace(self.packet.worktree, self.packet.branch, "base", False)

    def seed(self, slot=1, record=None):
        with closing(sqlite3.connect(self.journal.path)) as db, db:
            db.execute("INSERT OR REPLACE INTO slot VALUES (?, ?)",
                       (slot, json.dumps(self.record if record is None else record)))

    def step(self, *, now=0, observations=None, **kwargs):
        with patch("phalanx.attended_recovery_service.WorkspaceManager.inspect",
                   return_value=self.workspace):
            return run_recovery(
                self.root, "desk", self.api, RecoveryJournal(self.root, 1),
                observations=[self.native] if observations is None else observations,
                discovery_complete=True, clock=lambda: now, **kwargs,
            )

    def test_lost_stop_reply_replays_exact_key_and_archives_after_valid_ack(self):
        self.api.error = "Runner API response unavailable"
        self.assertEqual(self.step()["status"], "retry")
        self.assertEqual(self.step(now=1)["status"], "retry")
        self.assertEqual(len(self.api.requests), 1)
        self.api.error = None
        self.api.response = {"id": "unrelated", "version": 4, "state": {"status": "queued"}}
        self.assertEqual(self.step(now=2, observations=[])["status"], "retry")
        self.assertTrue(self.journal.snapshot()[0])
        self.api.response["id"] = "execution"
        self.assertEqual(self.step(now=6, observations=[])["status"], "recovered")
        self.assertEqual(self.api.requests, [self.api.requests[0]] * 3)
        self.assertEqual(self.journal.snapshot()[0], {})
        with closing(sqlite3.connect(self.journal.path)) as db, db:
            archived = json.loads(db.execute("SELECT record FROM completed").fetchone()[0])
        self.assertNotIn("lease", archived)
        self.assertEqual(archived["recovery_acknowledgment"], self.api.response)

    def test_resumed_writer_holds_saved_stop_until_checkpoint_is_reverified(self):
        self.api.error = "Runner API response unavailable"
        self.assertEqual(self.step()["status"], "retry")
        self.api.error = None
        active = [replace(self.native, state="active")]
        self.assertEqual(self.step(now=1, observations=active)["status"], "owner_wait")
        self.assertEqual(self.step(now=2, observations=[], recheck=True)["status"], "owner_wait")
        self.workspace = replace(self.workspace, dirty=True)
        self.assertEqual(self.step(now=3, recheck=True)["status"], "owner_wait")
        self.assertEqual(len(self.api.requests), 1)
        self.workspace = replace(self.workspace, dirty=False)
        self.assertEqual(self.step(now=4, recheck=True)["status"], "recovered")
        self.assertEqual(self.api.requests, [self.api.requests[0]] * 2)

    def test_conflict_requires_explicit_recheck_and_reissues_for_changed_version(self):
        self.api.error = "Runner API returned HTTP 409 for /dispatch"
        self.assertEqual(self.step()["status"], "owner_wait")
        self.api.expired = [self.expired | {"version": 4}]
        self.api.error = None
        self.api.response["version"] = 5
        self.assertEqual(self.step(now=2)["status"], "owner_wait")
        self.assertEqual(self.step(now=2, recheck=True)["status"], "recovered")
        self.assertNotEqual(self.api.requests[0][1], self.api.requests[1][1])
        self.assertEqual(self.api.requests[1][0]["expected_version"], 4)

    def test_active_writer_is_adopted_and_other_slot_is_accounted_for(self):
        other = self.expired | {"attempt_id": uuid4().hex}
        orphan = self.expired | {"attempt_id": uuid4().hex}
        self.seed(slot=2, record=self.record | {"claim": {"attempt_id": other["attempt_id"]}})
        self.api.expired.extend((other, orphan))
        decision = self.step(observations=[replace(self.native, state="active")])
        self.assertEqual(decision["status"], "adopted")
        self.assertEqual([row["attempt_id"] for row in decision["unmatched"]], [orphan["attempt_id"]])
        self.assertEqual(self.api.requests, [])
        [(body, key, lease)] = self.api.claims
        self.assertEqual(body["expected_version"], 3)
        record = self.journal.snapshot()[0]
        self.assertEqual((record["lease"], record["lease_expires_at"]), (lease, 500))
        self.assertNotIn("recover_claim", record)
        self.assertNotIn(lease, json.dumps(decision))

    def test_verified_result_is_adopted_without_releasing_writer_slot(self):
        result = self.packet.result_identity() | {"outcome": "succeeded", "details": {"evidence": "# Report"}}
        path = self.root / "results" / f"{self.packet.marker}.json"
        path.write_text(json.dumps(result), encoding="utf-8")
        self.api.error = "Runner API response unavailable"
        self.assertEqual(self.step()["status"], "retry")
        saved = self.journal.snapshot()[0]
        self.assertEqual(saved["lease"], "local-secret")
        checkpoint = saved["recovery_checkpoint"]
        self.assertEqual(checkpoint["thread_id"], str(self.native.thread_id))
        self.assertEqual(checkpoint["workspace"]["result_path"], str(path))
        # A mismatched reply never swaps the lease, and the replay reuses key and secret.
        self.api.error = None
        self.api.claim_response = self.api.claim_response | {"execution_id": "other"}
        self.assertEqual(self.step(now=2, observations=[])["status"], "retry")
        self.assertEqual(self.journal.snapshot()[0]["lease"], "local-secret")
        self.api.claim_response["execution_id"] = "execution"
        self.assertEqual(self.step(now=6, observations=[])["status"], "adopted")
        self.assertEqual(self.api.claims, [self.api.claims[0]] * 3)
        self.assertEqual(self.journal.snapshot()[0]["lease"], self.api.claims[0][2])
        self.assertEqual(self.api.requests, [])

    def test_incomplete_result_exhausts_bounded_rechecks(self):
        (self.root / "results" / f"{self.packet.marker}.json.tmp").touch()
        for now in (0, 2, 6, 14):
            self.assertEqual(self.step(now=now)["status"], "retry")
        self.assertEqual(self.step(now=30)["status"], "owner_wait")
        self.assertEqual(self.api.requests, [])

    def test_concurrent_prepare_cannot_publish_launch_intent_after_stop_intent(self):
        before_launch = {key: value for key, value in self.record.items() if key != "packet"}
        self.seed(record=before_launch)
        self.api.error = "Runner API response unavailable"

        def prepare(snapshot):
            self.assertEqual(self.step(observations=[], no_launch_proof="No worktree")["status"], "retry")
            snapshot.update(self.record)
            snapshot["lease_expires_at"] = 100
            return {}

        runner = AttendedRunner(self.root, {"runner_id": "desk"}, self.api)
        with patch.object(runner, "_prepare_launch", side_effect=prepare):
            with self.assertRaisesRegex(RunnerError, "Recovery owns this reservation"):
                runner.prepare()
        self.assertNotIn("packet", self.journal.snapshot()[0])

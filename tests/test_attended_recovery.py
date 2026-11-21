import json
import unittest
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from phalanx.attended_recovery import (
    NativeObservation, StoppedEvidence, acknowledge_stop, reconcile, unmatched_leases,
)
from phalanx.handoff import WorkPacket


class AttendedRecoveryTests(unittest.TestCase):
    def setUp(self):
        attempt = uuid4()
        self.start = datetime.now(timezone.utc)
        self.packet = WorkPacket("item", attempt, 2, "development", "repo",
                                 Path.cwd() / "tree", "phalanx/item", "revision", "goal",
                                 "pull_request", "commit")
        self.record = {
            "runner_id": "runner", "lease": "secret", "host_id": "local",
            "project_id": "project", "started_at": self.start.isoformat(),
            "claim": {"attempt_id": attempt.hex, "item_id": "item",
                      "execution": {"id": "execution", "version": 2,
                                    "state": {"stage": "development"}}},
            "packet": {**asdict(self.packet), "attempt_id": str(attempt),
                       "worktree": str(self.packet.worktree)},
        }
        self.observation = NativeObservation(
            "local", uuid4(), "project", self.start, self.packet.marker,
            self.packet.result_identity(), "stopped",
        )
        self.expired = [{"attempt_id": attempt.hex, "execution_id": "execution",
                         "item_id": "item", "stage": "development", "version": 3}]
        self.checkpoint = StoppedEvidence(attempt, self.observation.thread_id,
                                         str(self.packet.worktree), "retry_safe",
                                         "Git checkpoint verified and effects accounted for.")

    def decision(self, **overrides):
        arguments = dict(runner_id="runner", packet=self.packet, expired=self.expired, now=0)
        return reconcile(self.record, **(arguments | overrides))

    def test_lost_launch_stays_reserved_and_becomes_actionable(self):
        for now, expected in [(0, 2), (1, 2), (2, 6), (6, 14), (14, 30)]:
            decision = self.decision(now=now, discovery_complete=True)
            self.assertEqual((decision["status"], decision["retry_at"]), ("retry", expected))
            self.record = json.loads(json.dumps(self.record))
        decision = self.decision(now=30, discovery_complete=True)
        self.assertEqual(decision["status"], "owner_wait")
        self.assertIn("next_action", decision)
        self.assertNotIn("retry_at", decision)
        self.assertEqual(self.decision(now=100)["checked_at"], 30)
        self.assertNotIn("recover_stop", self.record)
        decision = self.decision(now=101, recheck=True, discovery_complete=True,
                                 observations=[replace(self.observation, state="active")])
        self.assertEqual(decision["status"], "reattach")

    def test_exact_native_identity_and_unique_complete_discovery(self):
        for observation in (
            replace(self.observation, host_id="other"),
            replace(self.observation, project_id="other"),
            replace(self.observation, marker="other"),
            replace(self.observation, started_at=self.start - timedelta(seconds=1)),
            replace(self.observation, assignment=self.observation.assignment | {"state_version": 2.0}),
        ):
            with self.subTest(observation=observation):
                decision = self.decision(recheck=True, discovery_complete=True,
                                         observations=[observation], stopped=self.checkpoint)
                self.assertEqual(decision["status"], "retry")
                self.assertNotIn("receipt", self.record)
        for observations, complete in (
            ([self.observation, replace(self.observation, thread_id=uuid4())], True),
            ([self.observation], False),
        ):
            decision = self.decision(recheck=True, discovery_complete=complete,
                                     observations=observations, stopped=self.checkpoint)
            self.assertEqual(decision["status"], "retry")
            self.assertNotIn("receipt", self.record)

    def test_queued_receipt_requires_native_correlation(self):
        client_id = uuid4()
        self.record["receipt"] = {"client_thread_id": str(client_id)}
        decision = self.decision(discovery_complete=True, observations=[self.observation])
        self.assertEqual(decision["status"], "retry")
        bound = replace(self.observation, client_thread_id=client_id)
        decision = self.decision(now=2, discovery_complete=True, observations=[bound])
        self.assertEqual(decision["status"], "verify_outputs")
        self.assertEqual(self.record["receipt"], {"client_thread_id": str(client_id),
                                                  "thread_id": str(bound.thread_id)})
        decision = self.decision(now=3, observations=[replace(bound, thread_id=uuid4())])
        self.assertEqual(decision["status"], "retry")

    def test_stop_request_replays_after_restart_until_exact_ack(self):
        pins = [{"upstream_id": "upstream", "execution_id": "upstream-execution",
                 "milestone": "report", "reference": "/report", "revision": "pinned-report",
                 "reviewed_revision": None}]
        self.packet = replace(self.packet, dependencies=tuple(pins))
        self.record["packet"]["dependencies"] = pins
        self.record["claim"]["dependencies"] = pins
        decision = self.decision(discovery_complete=True, observations=[self.observation],
                                 stopped=self.checkpoint)
        self.assertEqual(decision["status"], "send_stop")
        request = self.record["recover_stop"]
        self.assertEqual(request["body"]["expected_version"], 3)
        self.record = json.loads(json.dumps(self.record))
        replay = self.decision(expired=None)
        self.assertEqual(replay["status"], "send_stop")
        self.assertEqual(replay["body"], request["body"])
        self.assertEqual(replay["key"], request["key"])
        self.assertEqual(self.decision(now=1, stop_failure="unavailable")["status"], "retry")
        self.assertEqual(self.decision(now=2)["status"], "retry")
        self.assertEqual(self.decision(now=3)["status"], "send_stop")
        self.assertEqual(self.decision(now=4, stop_failure="conflict")["status"], "owner_wait")
        self.assertEqual(self.decision(now=5)["status"], "owner_wait")
        self.assertEqual(self.decision(now=6, recheck=True)["key"], request["key"])
        response = {"id": "execution", "version": 4, "state": {"status": "queued"}}
        self.assertFalse(acknowledge_stop(self.record, request, response | {"version": 5}))
        self.assertFalse(acknowledge_stop(self.record, request, response | {"id": "other"}))
        self.assertTrue(acknowledge_stop(self.record, request, response))
        self.assertIn("lease", self.record)

    def test_finished_result_and_mismatched_checkpoint_cannot_enqueue_retry(self):
        for evidence, status in (
            (replace(self.checkpoint, worktree="wrong"), "verify_outputs"),
            (replace(self.checkpoint, thread_id=uuid4()), "verify_outputs"),
            (replace(self.checkpoint, disposition="verified_result"), "owner_wait"),
        ):
            decision = self.decision(discovery_complete=True, observations=[self.observation],
                                     stopped=evidence)
            self.assertEqual(decision["status"], status)
            self.assertNotIn("recover_stop", self.record)

    def test_no_launch_requires_exact_expired_server_attempt(self):
        del self.record["packet"]
        self.assertEqual(self.decision(packet=None, runner_id="other")["status"], "owner_wait")
        self.assertEqual(self.decision(packet=None, expired=[])["status"], "reconcile_server")
        wrong = [self.expired[0] | {"execution_id": "other"}]
        self.assertEqual(self.decision(packet=None, expired=wrong)["status"], "owner_wait")
        self.assertEqual(self.decision(packet=None)["status"], "send_stop")

    def test_draft_claim_requires_publication_reconciliation_without_a_worker(self):
        del self.record["packet"]
        self.record["claim"]["execution"]["state"]["stage"] = "pr_creation"
        expired = [self.expired[0] | {"stage": "pr_creation"}]
        result = self.decision(packet=None, expired=expired)
        self.assertEqual(result["status"], "owner_wait")
        self.assertIn("publication journal", result["next_action"])
        self.assertNotIn("recover_stop", self.record)

    def test_saved_submission_and_unmatched_lease(self):
        self.record["submission"] = {"body": {}}
        self.assertEqual(self.decision(expired=[])["status"], "replay_submission")
        self.assertEqual(self.decision()["status"], "owner_wait")
        self.assertNotIn("recover_stop", self.record)
        self.assertEqual(unmatched_leases(self.expired, {self.packet.attempt_id.hex}), [])
        self.assertEqual(unmatched_leases(self.expired, set())[0]["status"], "owner_wait")

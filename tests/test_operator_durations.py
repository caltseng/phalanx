from datetime import UTC, datetime, timedelta
import json
import sqlite3
import unittest

from phalanx import operator_durations as durations
from phalanx.operator_api import summary

START = datetime(2026, 1, 1, tzinfo=UTC)


def at(seconds):
    return (START + timedelta(seconds=seconds)).isoformat(timespec="microseconds")


def row(version, seconds, stage, status, event="result", attempt=None, outcome=None, reason=None,
        retried=False, execution="e1"):
    return {"execution_id": execution, "version": version, "event": event, "attempt_id": attempt,
            "created_at": at(seconds), "stage": stage, "status": status, "outcome": outcome,
            "reason": reason, "retried": retried}


class LifecycleTest(unittest.TestCase):
    def test_returns_retries_and_waits_accumulate_per_stage_and_open_visit_runs_to_now(self):
        rows = [
            row(1, 0, "development", "queued", "intake"),
            row(2, 10, "development", "running", "claim", "a1"),
            row(3, 100, "review", "running", "claim", "a2", outcome="succeeded"),
            row(4, 160, "development", "queued", outcome="changes_required", reason="Fix tests"),
            row(5, 170, "development", "queued", "retry", retried=True),
            row(6, 200, "development", "running", "claim", "a3"),
            row(7, 230, "development", "waiting_for_input", reason="Choose scope"),
            row(8, 290, "development", "queued", "reply"),
        ]
        result = durations.lifecycle(rows, at(0), START + timedelta(seconds=300))
        self.assertEqual(result["coverage"], "complete")
        first, review, second = result["visits"]
        self.assertEqual((first["entered_at"], first["exited_at"], first["seconds"]), (at(0), at(100), 100))
        self.assertEqual((first["queued_seconds"], first["executing_seconds"]), (10, 90))
        self.assertEqual((first["outcome"], first["attempt_ids"]), ("succeeded", ["a1"]))
        self.assertEqual((review["seconds"], review["attempt_ids"], review["returned"]), (60, ["a2"], False))
        self.assertEqual((review["outcome"], review["reason"]), ("changes_required", "Fix tests"))
        self.assertTrue(second["returned"] and second["open"])
        self.assertEqual((second["entered_at"], second["exited_at"], second["seconds"]), (at(160), None, 140))
        self.assertEqual((second["queued_seconds"], second["waiting_seconds"], second["executing_seconds"]),
                         (50, 60, 30))
        self.assertEqual((second["retries"], second["attempt_ids"]), (1, ["a3"]))
        development = result["stages"][0]
        self.assertEqual((development["stage"], development["visits"], development["seconds"]),
                         ("development", 2, 240))
        # Phase totals partition the item's recorded time with no overlap.
        self.assertEqual(result["seconds"], 300)
        self.assertEqual(result["queued_seconds"] + result["waiting_seconds"] + result["executing_seconds"], 300)
        self.assertEqual(result["age_seconds"], 300)

    def test_terminal_items_freeze_and_reruns_start_new_visits(self):
        rows = [
            row(1, 0, "report_drafting", "queued", "intake"),
            row(2, 5, "report_drafting", "running", "claim", "a1"),
            row(3, 50, "report_drafting", "failed", reason="Worker crashed"),
            row(1, 100, "report_drafting", "queued", "rerun", execution="e2"),
            row(2, 120, "report_drafting", "running", "claim", "a2", execution="e2"),
            row(3, 180, "report_drafting", "completed", execution="e2"),
        ]
        result = durations.lifecycle(rows, at(0), START + timedelta(days=30))
        self.assertEqual((result["closed_at"], result["age_seconds"]), (at(180), 180))
        failed, rerun = result["visits"]
        self.assertEqual((failed["seconds"], failed["outcome"], failed["exited_at"]), (50, "failed", at(50)))
        self.assertEqual((rerun["execution_id"], rerun["seconds"], rerun["returned"]), ("e2", 80, True))
        self.assertFalse(rerun["open"])
        # The gap between the failure and the rerun is not attributed to any stage.
        self.assertEqual(result["seconds"], 130)

    def test_missing_history_is_partial_and_never_estimated(self):
        rows = [
            row(3, 0, "development", "queued"),
            row(4, 10, "development", "running", "claim", "a1"),
            row(7, 50, "review", "queued"),
        ]
        result = durations.lifecycle(rows, at(-500), START + timedelta(seconds=60))
        self.assertEqual(result["coverage"], "partial")
        self.assertIn("before version 3", result["coverage_note"])
        self.assertIn("versions 5 to 6", result["coverage_note"])
        development, review = result["visits"]
        self.assertTrue(development["partial"])
        self.assertEqual((development["queued_seconds"], development["executing_seconds"]), (10, 0))
        self.assertEqual((review["seconds"], review["open"]), (10, True))
        self.assertTrue(result["stages"][0]["partial"])


class SlotTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            """CREATE TABLE attempts (id TEXT, execution_id TEXT, started_at TEXT, ended_at TEXT);
               CREATE TABLE transition_history (execution_id TEXT, version INTEGER, attempt_id TEXT,
                                                event TEXT, details_json TEXT);"""
        )
        self.addCleanup(self.db.close)

    def claim(self, attempt, execution, version, started, ended=None, continued_from=None):
        self.db.execute("INSERT INTO attempts VALUES (?, ?, ?, ?)", (attempt, execution, at(started),
                                                                    ended and at(ended)))
        details = {"continued_from": continued_from} if continued_from else {}
        self.db.execute("INSERT INTO transition_history VALUES (?, ?, ?, 'claim', ?)",
                        (execution, version, attempt, json.dumps(details)))

    def test_retained_slot_occupancy_spans_handoffs_and_release_resets_it(self):
        self.claim("a1", "e1", 2, 0, ended=100)
        self.claim("a2", "e1", 4, 100, ended=200, continued_from="a1")
        self.claim("a3", "e1", 6, 200, continued_from="a2")
        # e2 released its slot and was later admitted again without continuation.
        self.claim("b1", "e2", 2, 0, ended=50)
        self.claim("b2", "e2", 4, 250)
        occupied = durations.occupancy(self.db, ["a3", "b2", None])
        self.assertEqual(occupied, {"a3": (at(0), 2), "b2": (at(250), 0)})
        now = START + timedelta(seconds=300)
        retained = durations.slot({"id": "a3", "started_at": at(200)}, occupied, "running", None, 1e12, now)
        self.assertEqual((retained["occupied_since"], retained["occupied_seconds"]), (at(0), 300))
        self.assertEqual((retained["attempt_age_seconds"], retained["handoffs"]), (100, 2))
        self.assertEqual(retained["activity"], "executing")
        fresh = durations.slot({"id": "b2", "started_at": at(250)}, occupied, "running", None, 1e12, now)
        self.assertEqual((fresh["occupied_seconds"], fresh["attempt_age_seconds"]), (50, 50))

    def test_activity_separates_execution_from_waits(self):
        now = START + timedelta(seconds=10)
        self.assertEqual(durations.slot_activity("running", "No exact writer", 1e12, now), "resource_wait")
        self.assertEqual(durations.slot_activity("waiting_for_external", None, 1e12, now), "external_wait")
        self.assertEqual(durations.slot_activity("running", None, now.timestamp(), now), "lease_expired")
        self.assertNotIn("secret", durations.runner_label("secret-runner"))


class SummaryTest(unittest.TestCase):
    def test_wait_start_follows_reason_source_and_missing_start_is_unavailable(self):
        state = {"final_output": None, "wait": None}
        base = {
            "id": "e", "item_id": "i", "version": 3, "title": "t", "repository_id": "r", "stage": "development",
            "status": "running", "state_json": json.dumps(state), "last_advanced_at": at(0),
            "updated_at": at(50), "wait_since": at(5), "blocked_count": 0, "retry_at": None,
            "recheck_at_ms": None, "worker_reference": None, "worker_url": None, "stage_owner": "a1",
            "slot_retained": 0, "capacity_release": None, "runner_blocker": "Disk full",
            "blocker_since": None, "lease_expires_at": 1e12, "attempt_started_at": at(40),
            "item_created_at": at(0), "dependency_wait_since": None, "pending_since": at(30),
        }
        now = START + timedelta(seconds=100)
        item = summary(base, now)
        # A blocker reported before blocker_since existed has no recorded start.
        self.assertEqual((item["wait_reason"], item["wait_since"], item["wait_age_seconds"]),
                         ("Disk full", None, None))
        self.assertIsNone(item["pending_since"])
        self.assertEqual((item["age_seconds"], item["slot"]["attempt_age_seconds"]), (100, 60))
        queued = summary(dict(base, status="queued", stage_owner=None, runner_blocker=None, blocked_count=2,
                              dependency_wait_since=at(70)), now)
        self.assertEqual((queued["wait_since"], queued["wait_age_seconds"]), (at(70), 30))
        self.assertEqual((queued["pending_since"], queued["pending_seconds"], queued["slot"]), (at(30), 70, None))

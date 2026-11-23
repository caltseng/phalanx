from dataclasses import asdict, replace
from datetime import datetime
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from phalanx.intake import record, steer
from phalanx.intake_models import Steering
from phalanx.operator_api import closed_page, open_page
from phalanx.store import connect, migrate, write_transaction
from phalanx.transitions import Execution, PullRequest, Revision, Stage, Status, StoredReport, Target, Wait

from test_bootstrap import OWNER, RUNNER, ServerFixture


BOARD = "/v1/operator/executions"
EARLY = "2025-01-01T00:00:00.000000+00:00"
LATER = "2025-01-02T00:00:00.000000+00:00"
LATEST = "2025-01-03T00:00:00.000000+00:00"
QUEUED = Execution(Target.REPORT, Stage.REPORT_DRAFTING)
WAITING = replace(QUEUED, status=Status.WAITING_FOR_INPUT, wait=Wait("Choose scope", "reply"))


def identifier(value):
    return f"{value:032x}"


def insert(db, number, state=QUEUED):
    item, execution = identifier(number + 100000), identifier(number)
    data = json.dumps(asdict(state))
    db.execute("INSERT INTO work_items VALUES (?, 'repo', ?, ?)",
               (item, json.dumps({"goal": f"Work {number}"}), EARLY))
    db.execute(
        """INSERT INTO executions (id, item_id, version, state_json, created_at, updated_at)
           VALUES (?, ?, 1, ?, ?, ?)""", (execution, item, data, EARLY, EARLY))
    record(db, execution, 1, "intake", {}, data, EARLY)


def transition(db, number, version, state, at=LATER, attempt=None):
    execution = identifier(number)
    data = json.dumps(asdict(state))
    db.execute("UPDATE executions SET version=?, state_json=?, updated_at=? WHERE id=?",
               (version, data, at, execution))
    record(db, execution, version, "result", {}, data, at, attempt)


class OperatorTest(ServerFixture, unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "queue.sqlite3"
        migrate(self.path)
        with write_transaction(self.path) as db:
            db.execute("INSERT INTO repositories VALUES ('repo', 'https://github.com/a/b', ?)", (EARLY,))
            for number in range(1, 5001):
                insert(db, number)
            transition(db, 5000, 2, WAITING)
            transition(db, 5000, 3, WAITING, LATEST)
            for number in range(6000, 8000):
                insert(db, number, self.closed(number))
            for version in range(2, 5001):
                transition(db, 10, version, QUEUED)
            # Intake marks queued work ready; the raw seed rows need it to be ranked.
            db.execute("UPDATE executions SET ready_since = ? WHERE status = 'queued'",
                       (int(datetime.fromisoformat(EARLY).timestamp() * 1000),))
        self.start_server(self.path)

    @staticmethod
    def closed(number):
        return replace(QUEUED, status=Status.CANCELLED,
                       final_output=StoredReport(f"/v1/items/{identifier(number + 100000)}"
                                                 f"/executions/{identifier(number)}/report"))

    def page(self, url):
        status, body = self.get(url, OWNER)
        self.assertEqual(status, 200, body)
        return body

    def test_open_and_closed_pages_during_advancement_and_closure(self):
        first = self.page(BOARD + "?limit=3")
        waiting, queued, _ = first["items"]
        self.assertEqual([row["id"] for row in first["items"]], list(map(identifier, [5000, 1, 2])))
        self.assertEqual((waiting["stage"], waiting["status"]), ("report_drafting", "waiting_for_input"))
        self.assertEqual(waiting["repository_id"], "repo")
        self.assertEqual(waiting["wait_reason"], "Choose scope")
        self.assertEqual(waiting["resume_trigger"], "reply")
        self.assertEqual(waiting["wait_since"], LATER)
        self.assertEqual(waiting["last_advanced_at"], EARLY)
        self.assertEqual(waiting["last_progress_at"], LATEST)
        self.assertGreater(waiting["wait_age_seconds"], 0)
        self.assertEqual(queued["wait_reason"],
                         "Waiting for runner capacity: rank 1 of 4999 eligible, 0 higher-ranked ahead")
        self.assertGreater(queued["wait_age_seconds"], 0)

        closed_first = self.page(BOARD + "?kind=closed&limit=2")
        self.assertEqual([row["id"] for row in closed_first["items"]], list(map(identifier, [7999, 7998])))
        with write_transaction(self.path) as db:
            transition(db, 2, 2, replace(QUEUED, stage=Stage.REPORT_REVIEW))
            db.execute("UPDATE executions SET retry_at=? WHERE id=?", (1700000000000, identifier(2)))
            transition(db, 3, 2, self.closed(3))
            transition(db, 4999, 2, WAITING)
            db.execute("INSERT INTO reports VALUES (?, ?)", (identifier(3), "Cancellation report"))
        later = self.page(f"{BOARD}?limit=3&cursor={first['next_cursor']}")
        self.assertEqual([row["id"] for row in later["items"]], list(map(identifier, [4, 5, 6])))
        refreshed = self.page(BOARD + "?limit=2")
        self.assertEqual([row["id"] for row in refreshed["items"]], list(map(identifier, [4999, 5000])))
        detail = self.page(f"{BOARD}/{identifier(2)}/timeline")
        self.assertEqual(detail["item"]["last_advanced_at"], LATER)
        self.assertEqual(detail["item"]["retry_at_ms"], 1700000000000)
        self.assertEqual(detail["events"][0]["from_stage"], "report_drafting")
        closed_next = self.page(f"{BOARD}?kind=closed&limit=2&cursor={closed_first['next_cursor']}")
        self.assertEqual([row["id"] for row in closed_next["items"]], list(map(identifier, [7997, 7996])))
        newest = self.page(BOARD + "?kind=closed&limit=1")["items"][0]
        self.assertEqual((newest["id"], newest["closed_at"], newest["outcome"], newest["output"]["kind"]),
                         (identifier(3), LATER, "cancelled", "report"))
        self.assertEqual(self.get(newest["output"]["url"], OWNER), (200, "Cancellation report"))

        pr = PullRequest("https://github.com/example/project/pull/123", Revision("head", "base"))
        ready = Execution(Target.READY, Stage.PR_RECONCILIATION, Status.COMPLETED, final_output=pr)
        merged = Execution(Target.MERGED, Stage.CHECK_IN, Status.COMPLETED,
                           final_output=replace(pr, merge_commit="merge"))
        with write_transaction(self.path) as db:
            transition(db, 7, 2, ready, LATEST)
            transition(db, 8, 2, merged, LATEST)
        results = self.page(BOARD + "?kind=closed&limit=2")["items"]
        self.assertEqual([row["outcome"] for row in results], ["checked_in", "ready_for_check_in"])
        self.assertEqual(results[0]["output"], {"kind": "pr", "url": pr.url})

    def test_timeline_pages_and_authentication(self):
        route = f"{BOARD}/{identifier(10)}/timeline"
        self.assertEqual(self.get(BOARD)[0], 401)
        self.assertEqual(self.get(route)[0], 401)
        self.assertEqual(self.get(BOARD, RUNNER)[0], 200)
        self.assertEqual(self.get(route, RUNNER)[0], 200)
        self.assertEqual(self.get(f"{BOARD}/{identifier(0)}/timeline", OWNER)[0], 404)
        for query, status in (("cursor=", 400), ("cursor=bogus", 400), ("limit=101", 422), ("limit=0", 422)):
            self.assertEqual(self.get(f"{BOARD}?{query}", OWNER)[0], status)
            self.assertEqual(self.get(f"{route}?{query}", OWNER)[0], status)
        first = self.page(route + "?limit=2")
        self.assertEqual([row["version"] for row in first["events"]], [5000, 4999])
        with write_transaction(self.path) as db:
            db.execute("INSERT INTO attempts VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL)",
                       ("attempt", identifier(10), QUEUED.stage, "runner-secret", "lease-digest", 9999, LATEST))
            transition(db, 10, 5001, replace(QUEUED, status=Status.RUNNING), LATEST, "attempt")
            db.execute("INSERT INTO external_rechecks VALUES (?, 5001, 1700000000000)", (identifier(10),))
        later = self.page(f"{route}?limit=2&cursor={first['next_cursor']}")
        self.assertEqual(later["attempt"], "attempt")
        self.assertEqual(later["item"]["recheck_at_ms"], 1700000000000)
        self.assertEqual([row["version"] for row in later["events"]], [4998, 4997])
        self.assertNotIn("lease-digest", json.dumps(later))
        self.assertNotIn("runner-secret", json.dumps(later))
        self.assertEqual(self.get(f"{BOARD}/{identifier(11)}/timeline?cursor={first['next_cursor']}", OWNER)[0], 400)
        self.assertEqual(self.get(f"{BOARD}?cursor={first['next_cursor']}", OWNER)[0], 400)
        self.assertIsNone(self.page(f"{BOARD}/{identifier(11)}/timeline")["next_cursor"])

    def test_deep_queue_pages_seek_to_cursor(self):
        with connect(self.path, mode="ro") as db:
            # Interrupt after 2,000 VM instructions. A scan through thousands
            # of rows before either boundary would exceed this budget.
            db.set_progress_handler(lambda: 1, 2000)
            opened = open_page(db, [1, EARLY, identifier(4900)], 3)
            closed = closed_page(db, [EARLY, identifier(6100)], 3)
            self.assertEqual([row["id"] for row in opened], list(map(identifier, [4901, 4902, 4903])))
            self.assertEqual([row["id"] for row in closed], list(map(identifier, [6099, 6098, 6097])))

    def test_timeline_exposes_prerequisite_blockers_and_clears_after_steering(self):
        upstream = identifier(100001)
        status, item = self.request("/v1/items", OWNER, {
            "repository_id": "repo", "goal": "Follow the upstream report", "target": "report",
            "dependencies": [{"upstream_id": upstream, "milestone": "report"}],
        })
        self.assertEqual(status, 201, item)
        execution = item["execution"]
        route = f"{BOARD}/{execution['id']}/timeline"
        blocked = self.page(route)
        self.assertEqual(blocked["item"]["status"], "queued")
        self.assertEqual(blocked["item"]["blocked_prerequisite_count"], 1)
        self.assertEqual(blocked["item"]["wait_reason"], "Waiting for 1 prerequisite")
        self.assertEqual(blocked["prerequisites"], [{
            "item_id": upstream, "title": "Work 1", "milestone": "report",
            "reason": "Waiting for prerequisite milestone report",
        }])
        with write_transaction(self.path) as db:
            steer(db, item["id"], execution["id"],
                  Steering(expected_version=execution["version"], dependencies=[]))
        ready = self.page(route)
        self.assertEqual(ready["item"]["blocked_prerequisite_count"], 0)
        self.assertTrue(ready["item"]["wait_reason"].startswith("Waiting for runner capacity: rank "))
        self.assertEqual(ready["prerequisites"], [])

    def test_queued_work_explains_its_capacity_wait(self):
        _, first, second = self.page(BOARD + "?limit=3")["items"]
        self.assertEqual(first["resume_trigger"], "A runner with a free slot claims eligible work in rank order")
        self.assertEqual(second["wait_reason"],
                         "Waiting for runner capacity: rank 2 of 4999 eligible, 1 higher-ranked ahead")
        with write_transaction(self.path) as db:
            db.execute("UPDATE executions SET ready_since=? WHERE id=?", (32503680000000, identifier(1)))
        _, retrying, second = self.page(BOARD + "?limit=3")["items"]
        self.assertEqual(retrying["wait_reason"], "Retry scheduled at 3000-01-01T00:00:00.000000+00:00")
        self.assertEqual(second["wait_reason"],
                         "Waiting for runner capacity: rank 1 of 4998 eligible, 0 higher-ranked ahead")
        with write_transaction(self.path) as db:
            db.execute("UPDATE dispatch_settings SET paused=1")
        self.assertEqual(self.page(f"{BOARD}/{identifier(2)}/timeline")["item"]["wait_reason"],
                         "Dispatch is paused")

    def test_result_events_explain_backward_transitions(self):
        development = Execution(Target.MERGED, Stage.DEVELOPMENT)
        candidate, head = "a" * 40, "b" * 40
        pr = "https://github.com/example/project/pull/119"
        with write_transaction(self.path) as db:
            insert(db, 9000, development)
            db.execute("INSERT INTO attempts VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                       ("attempt-9", identifier(9000), "pre_pr_validation", "runner", "digest", 1, EARLY, LATER))
            db.execute("INSERT INTO attempt_workers VALUES ('attempt-9', 'worker-9', NULL)")
            steps = [
                (Stage.PRE_PR_VALIDATION, {}, None),
                (Stage.DEVELOPMENT, {
                    "stage": "pre_pr_validation", "outcome": "changes_required", "candidate": candidate,
                    "review_notes": "PRIVATE TASK BODY",
                    "local_checks": [{"name": "unit", "outcome": "failed",
                                      "reason": "Timed out after 1800 seconds"},
                                     {"name": "lint", "outcome": "passed", "reason": None}],
                }, "attempt-9"),
                (Stage.PR_PROMOTION, {}, None),
                (Stage.DEVELOPMENT, {
                    "stage": "pr_promotion", "outcome": "changes_required", "issue": "merge_conflict",
                    "pull": {"head_sha": head}, "pr_url": pr, "findings": ["PRIVATE FINDING"],
                }, None),
                (Stage.DEVELOPMENT, {
                    "stage": "development", "outcome": "failed",
                    "failure_reason": "Push rejected with token ghp_" + "x" * 36 + " " + "y" * 900,
                }, None),
            ]
            for version, (stage, details, attempt) in enumerate(steps, start=2):
                state = json.dumps(asdict(replace(development, stage=stage)))
                db.execute("UPDATE executions SET version=?, state_json=? WHERE id=?",
                           (version, state, identifier(9000)))
                record(db, identifier(9000), version, "result", details, state, LATER, attempt)
        events = self.page(f"{BOARD}/{identifier(9000)}/timeline")["events"]
        failure, conflict, _, checks = events[:4]
        self.assertEqual((checks["from_stage"], checks["stage"]), ("pre_pr_validation", "development"))
        self.assertEqual(checks["reason"], "changes_required; local check unit failed: "
                                           f"Timed out after 1800 seconds; at {candidate[:12]}")
        self.assertEqual((checks["candidate"], checks["attempt_id"], checks["worker_reference"]),
                         (candidate, "attempt-9", "worker-9"))
        self.assertEqual(conflict["from_stage"], "pr_promotion")
        self.assertEqual(conflict["reason"], f"changes_required; merge_conflict; at {head[:12]}")
        self.assertEqual((conflict["candidate"], conflict["pr_url"]), (head, pr))
        self.assertTrue(failure["reason"].startswith("failed; Push rejected with token [redacted] yyy"))
        self.assertLessEqual(len(failure["reason"]), 400)
        text = json.dumps(events)
        for secret in ("ghp_", "PRIVATE", "digest"):
            self.assertNotIn(secret, text)

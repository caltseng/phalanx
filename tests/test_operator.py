from dataclasses import asdict, replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from phalanx.intake import record, steer
from phalanx.intake_models import Steering
from phalanx.intake_schema import INTAKE_SCHEMA
from phalanx.operator_api import closed_page, open_page
from phalanx.store import APPLICATION_ID, MIGRATIONS, connect, write_transaction
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
        # Exercise the live migration from populated PH-05 storage.
        with write_transaction(self.path, create=True) as db:
            for statement in INTAKE_SCHEMA:
                db.execute(statement)
            db.execute(f"PRAGMA application_id = {APPLICATION_ID}")
            db.execute("PRAGMA user_version = 2")
            db.execute("INSERT INTO repositories VALUES ('repo', 'https://github.com/a/b', ?)", (EARLY,))
            for number in range(1, 5001):
                insert(db, number)
            transition(db, 5000, 2, WAITING)
            transition(db, 5000, 3, WAITING, LATEST)
            for number in range(6000, 8000):
                insert(db, number, self.closed(number))
            for version in range(2, 5001):
                transition(db, 10, version, QUEUED)

        # Bring the pre-operator store to the deployed PH-09 version first.
        with write_transaction(self.path) as db:
            for version, statements in enumerate(MIGRATIONS[2:4], start=3):
                for statement in statements:
                    db.execute(statement)
                db.execute(f"PRAGMA user_version = {version}")
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
        self.assertIsNone(queued["wait_age_seconds"])

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
        self.assertIsNone(ready["item"]["wait_reason"])
        self.assertEqual(ready["prerequisites"], [])

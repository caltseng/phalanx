from dataclasses import asdict, replace
from datetime import datetime, timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from urllib.request import Request, urlopen

from phalanx import intake
from phalanx.intake import record, steer
from phalanx.intake_models import Intake, Steering
from phalanx.intake_schema import INTAKE_SCHEMA
from phalanx.operator_api import closed_page, conditional_read, filtered_open_page, open_page, short_title
from phalanx.reconciliation import Observation, record as record_observation
from phalanx.store import APPLICATION_ID, MIGRATIONS, connect, migrate, write_transaction
from phalanx.transitions import Execution, PullRequest, Revision, Stage, Status, StoredReport, Target, Wait, item_branch

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
        self.assertEqual(results[0]["delivery"], {"merged": True, "merge_commit": "merge", "merge_method": None,
                                                  "merged_at": None})
        self.assertIsNone(results[1]["delivery"])

    def test_rows_carry_short_title_full_goal_target_and_created_at(self):
        goal = ("\n  Redesign the operator board for density. Then update DASHBOARD.md and the fixture.\n"
                "Second line with more detail.")
        with write_transaction(self.path) as db:
            db.execute("UPDATE work_items SET request_json=? WHERE id=?",
                       (json.dumps({"goal": goal, "target": "merged"}), identifier(105000)))
        row = self.page(BOARD + "?limit=1")["items"][0]
        self.assertEqual(row["title"], "Redesign the operator board for density.")
        self.assertEqual((row["goal"], row["target"], row["created_at"]), (goal, "merged", EARLY))
        self.assertEqual(self.page(f"{BOARD}/{identifier(5000)}/timeline")["item"]["title"], row["title"])
        self.assertIsNone(self.page(f"{BOARD}?limit=2")["items"][1]["target"])
        self.assertEqual(short_title("word " * 40), ("word " * 15).strip() + "…")
        self.assertEqual(short_title("x" * 100), "x" * 79 + "…")
        self.assertEqual(short_title("  \n "), "Untitled")

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
        with write_transaction(self.path) as db:
            for number in range(1000):
                db.execute("INSERT INTO github_observations VALUES (?, '', '', ?, 1, ?, ?, ?)",
                           (identifier(4901), str(number), EARLY, EARLY, '{"pr": null}'))
        with connect(self.path, mode="ro") as db:
            # Interrupt after 2,000 VM instructions. A scan through thousands
            # of rows before either boundary would exceed this budget.
            db.set_progress_handler(lambda: 1, 2000)
            opened = open_page(db, [1, EARLY, identifier(4900)], 3)
            closed = closed_page(db, [EARLY, identifier(6100)], 3)
            self.assertEqual([row["id"] for row in opened], list(map(identifier, [4901, 4902, 4903])))
            self.assertEqual([row["id"] for row in closed], list(map(identifier, [6099, 6098, 6097])))
        with connect(self.path, mode="ro") as db:
            db.set_progress_handler(lambda: 1, 200000)
            rows, cursor = filtered_open_page(
                db, [1, EARLY, identifier(100)], 3, " AND execution.id = ?", [identifier(4999)],
            )
            self.assertEqual(rows, [])
            self.assertEqual(cursor, [1, EARLY, identifier(2100)])
            rows, cursor = filtered_open_page(
                db, [1, EARLY, identifier(4900)], 3, " AND execution.id = ?", [identifier(4999)],
            )
            self.assertEqual([row["id"] for row in rows], [identifier(4999)])
            self.assertIsNone(cursor)

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
        self.assertEqual(ready["item"]["next_action"], "Next to run: a Captain dispatches a writer")
        self.assertEqual(ready["prerequisites"], [])


def observe(db, execution, version, stage, pull, **details):
    state = replace(Execution(Target.READY, Stage.DEVELOPMENT), stage=stage)
    data = json.dumps(asdict(state))
    db.execute("UPDATE executions SET version=?, state_json=? WHERE id=?", (version, data, execution))
    details = {"pr_url": "https://github.com/a/b/pull/5", "pull": pull, **details}
    record(db, execution, version, "pr_observation", details, data, LATER)
    item = db.execute("SELECT item_id FROM executions WHERE id=?", (execution,)).fetchone()[0]
    record_observation(db, item, execution, Observation(
        observed_at=datetime.fromisoformat(LATER) + timedelta(seconds=version),
        target_base="main",
        pr={**pull, "head_ref": item_branch(item), "base_ref": "main", "base_sha": "f" * 40},
        checks=details.get("gate_result", {}).get("state"),
        duplicate_count=len(details["duplicate_numbers"]) if "duplicate_numbers" in details else None,
    ))


class DashboardTest(ServerFixture, unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "queue.sqlite3"
        # Stop one migration short so the server backfills dashboard reads from history.
        with write_transaction(self.path, create=True) as db:
            for version, statements in enumerate(MIGRATIONS[:-1], start=1):
                for statement in statements:
                    db.execute(statement)
                db.execute(f"PRAGMA user_version = {version}")
            intake.register(db, "repo", "https://github.com/a/b.git")
            item = intake.create(db, Intake(repository_id="repo", goal="Ship it", target="ready_for_check_in", label="wave-1"))
            self.execution = item["execution"]["id"]
            pull = {"number": 5, "state": "open", "draft": True, "merged": False, "head_sha": "a" * 40}
            observe(db, self.execution, 2, Stage.PR_REVISION, pull,
                    gate_result={"state": "satisfied", "reasons": []}, duplicate_numbers=[6])
            intake.create(db, Intake(repository_id="repo", goal="Write notes", target="report"))
        self.start_server(self.path)

    def page(self, url, status=200):
        code, body = self.get(url, OWNER)
        self.assertEqual(code, status, body)
        return body

    def row(self, url):
        return next(row for row in self.page(url)["items"] if row["id"] == self.execution)

    def test_summary_pr_facts_and_revision_rounds(self):
        unknown = next(row for row in self.page(BOARD)["items"] if row["id"] != self.execution)
        self.assertIsNone(unknown["pr"])
        self.assertIsNone(unknown["duplicate_count"])
        row = self.row(BOARD)
        self.assertEqual((row["label"], row["target"], row["revision_round"], row["duplicate_count"]),
                         ("wave-1", "ready_for_check_in", 2, 1))
        self.assertEqual(row["pr"], {
            "url": "https://github.com/a/b/pull/5", "number": 5, "state": "open", "draft": True,
            "head_sha": "a" * 40, "head_ci": "satisfied", "additions": None, "deletions": None,
        })
        pull = {"number": 5, "state": "open", "draft": False, "merged": False, "head_sha": "b" * 40,
                "additions": 10, "deletions": 2}
        with write_transaction(self.path) as db:
            observe(db, self.execution, 3, Stage.DEVELOPMENT, pull)
        row = self.row(BOARD)
        self.assertEqual((row["revision_round"], row["duplicate_count"]), (3, None))
        self.assertEqual((row["pr"]["head_sha"], row["pr"]["head_ci"], row["pr"]["draft"],
                          row["pr"]["additions"], row["pr"]["deletions"]), ("b" * 40, None, False, 10, 2))
        rollup = self.page("/v1/operator/groups/wave-1")["rollup"]
        self.assertEqual((rollup["additions"], rollup["deletions"], rollup["size_unknown_count"]), (10, 2, 0))
        with write_transaction(self.path) as db:
            record_observation(db, row["item_id"], self.execution, Observation(
                observed_at=datetime.fromisoformat(LATER) + timedelta(seconds=1),
                target_base="main",
                pr={**pull, "head_sha": "a" * 40, "head_ref": item_branch(row["item_id"]),
                    "base_ref": "main", "base_sha": "f" * 40},
            ))
        self.assertEqual(self.row(BOARD)["pr"]["head_sha"], "b" * 40)
        with write_transaction(self.path) as db:
            item = row["item_id"]
            record_observation(db, item, self.execution, Observation(
                observed_at=datetime.fromisoformat(LATEST),
            ))
        # A writer's transition-history PR claim must never substitute for reconciler facts.
        unknown = self.row(BOARD)
        self.assertIsNone(unknown["pr"])
        self.assertIsNone(unknown["duplicate_count"])

    def test_conditional_reads_are_authenticated_scoped_and_invalidated_by_writes(self):
        routes = (BOARD, "/v1/operator/overview", "/v1/operator/groups/wave-1")
        for route in routes:
            with self.subTest(route=route):
                with urlopen(Request(self.url + route, headers={"Authorization": f"Bearer {OWNER}"})) as response:
                    token = response.headers["ETag"]
                    self.assertEqual(response.headers["Cache-Control"], "private, no-cache")
                self.assertEqual(self.request(route, OWNER, headers={"If-None-Match": token}), (304, ""))
                self.assertEqual(self.request(route, headers={"If-None-Match": token})[0], 401)
                self.assertEqual(self.request(BOARD + "?label=wave-1", OWNER,
                                              headers={"If-None-Match": token})[0], 200)
                with write_transaction(self.path) as db:
                    db.execute("UPDATE dispatch_settings SET paused = NOT paused")
                self.assertEqual(self.request(route, OWNER, headers={"If-None-Match": token})[0], 200)
        self.assertEqual(self.request(BOARD + "?cursor=bogus", OWNER,
                                      headers={"If-None-Match": "*"})[0], 400)
        self.assertEqual(self.request("/v1/operator/groups/missing", OWNER,
                                      headers={"If-None-Match": "*"})[0], 404)

    def test_retry_expiry_changes_token_and_queue_reason_without_a_write(self):
        from fastapi import Response
        from starlette.requests import Request as APIRequest
        before = datetime.fromisoformat(LATER)
        with write_transaction(self.path) as db:
            db.execute("UPDATE executions SET retry_at=? WHERE id=?",
                       (int((before + timedelta(seconds=1)).timestamp() * 1000), self.execution))
        request = APIRequest({"type": "http", "method": "GET", "path": BOARD,
                              "query_string": b"", "headers": []})
        with connect(self.path, mode="ro") as db:
            first = Response()
            self.assertIsNone(conditional_read(request, first, db, before))
            second = Response()
            self.assertIsNone(conditional_read(request, second, db, before + timedelta(seconds=2)))
        self.assertNotEqual(first.headers["ETag"], second.headers["ETag"])
        from phalanx.operator_api import dispatch_context, summary, SUMMARY
        with connect(self.path, mode="ro") as db:
            row = db.execute(f"{SUMMARY} WHERE execution.id=?", (self.execution,)).fetchone()
            context = dispatch_context(db, before)
        self.assertIn("Retry scheduled", summary(row, before, context)["wait_reason"])
        self.assertIsNone(summary(row, before + timedelta(seconds=2), context)["wait_reason"])

    def test_open_rows_always_say_why_without_capacity_inference(self):
        for row in self.page(BOARD)["items"]:
            self.assertTrue(row["wait_reason"] or row["next_action"], row)
            self.assertIsNone(row["wait_reason"])
            self.assertEqual(row["next_action"], "Next to run: a Captain dispatches a writer")
        overview = self.page("/v1/operator/overview")
        self.assertEqual(overview["sections"]["queued"], 2)
        self.assertEqual(overview["dispatch"], {"paused": False})
        self.assertNotIn("slots", overview)
        self.assertNotIn("runners", overview)
        self.assertIsNotNone(overview["oldest_queued_age_seconds"])

        claim = {"runner_id": "runner-a", "repository_ids": ["repo"], "slots": 1, "stages": ["report_drafting"]}
        status, claimed = self.request("/v1/dispatch/claim", RUNNER, claim, {"Phalanx-Lease": "x" * 32})
        self.assertEqual((status, claimed["status"]), (200, "claimed"), claimed)
        rows = self.page(BOARD)["items"]
        running = next(row for row in rows if row["status"] == "running")
        self.assertEqual(running["next_action"], "A worker is running the report_drafting stage")
        queued = next(row for row in rows if row["id"] == self.execution)
        self.assertIsNone(queued["wait_reason"])
        self.assertEqual(queued["next_action"], "Next to run: a Captain dispatches a writer")
        overview = self.page("/v1/operator/overview")
        self.assertEqual((overview["sections"]["running"], overview["sections"]["queued"]), (1, 1))
        self.page("/v1/operator/runners", 404)
        self.assertNotIn("runner-a", json.dumps(self.page(BOARD)))
        with write_transaction(self.path) as db:
            db.execute("UPDATE dispatch_settings SET paused=1")
        queued = self.row(BOARD)
        self.assertEqual(queued["wait_reason"], "Dispatch is paused")
        self.assertEqual(self.page("/v1/operator/overview")["dispatch"], {"paused": True})

    def test_filters_labels_and_groups(self):
        self.assertEqual([row["id"] for row in self.page(BOARD + "?label=wave-1")["items"]], [self.execution])
        self.assertEqual(len(self.page(BOARD + "?repository=repo&status=queued")["items"]), 2)
        self.assertEqual([row["title"] for row in self.page(BOARD + "?q=notes")["items"]], ["Write notes"])
        self.assertEqual(self.page(BOARD + "?stage=check_in")["items"], [])
        self.assertEqual([row["id"] for row in self.page(BOARD + "?stage=pr_revision")["items"]], [self.execution])
        self.assertEqual(self.page(BOARD + "?repository=missing")["items"], [])
        self.assertEqual(self.page(BOARD + "?status=running")["items"], [])
        for query in ("5", "%235", self.execution, "https%3A%2F%2Fgithub.com%2Fa%2Fb%2Fpull%2F5"):
            self.assertEqual([row["id"] for row in self.page(BOARD + "?q=" + query)["items"]], [self.execution])
        first = self.page(BOARD + "?repository=repo&limit=1")
        self.assertIsNotNone(first["next_cursor"])
        self.page(f"{BOARD}?limit=1&cursor={first['next_cursor']}", 400)
        self.assertEqual(len(self.page(f"{BOARD}?repository=repo&limit=1&cursor={first['next_cursor']}")["items"]), 1)
        for query in ("outcome=failed", "closed_after=2025-01-01T00:00:00Z", "status=cancelled",
                      "kind=closed&status=queued"):
            self.page(f"{BOARD}?{query}", 400)
        self.page(BOARD + "?label=bad%20label", 422)

        group = self.page("/v1/operator/groups/wave-1")
        self.assertEqual((group["rollup"]["items"], group["rollup"]["open"], group["rollup"]["size_unknown_count"]),
                         (1, 1, 1))
        self.assertEqual([row["id"] for row in group["items"]], [self.execution])
        self.page("/v1/operator/groups/wave-2", 404)
        item_id = group["items"][0]["item_id"]
        status, steered = self.request(f"/v1/items/{item_id}/executions/{self.execution}", OWNER,
                                       {"expected_version": group["items"][0]["version"], "label": "wave-2"},
                                       method="PATCH")
        self.assertEqual(status, 200, steered)
        moved = self.page("/v1/operator/groups/wave-2")
        self.assertEqual(moved["rollup"]["items"], 1)
        self.page("/v1/operator/groups/wave-1", 404)
        with write_transaction(self.path) as db:
            transition(db, int(self.execution, 16), moved["items"][0]["version"] + 1,
                       replace(QUEUED, status=Status.CANCELLED, final_output=StoredReport("/v1/report")))
        closed = self.page(BOARD + "?kind=closed&label=wave-2&outcome=cancelled&closed_after=2025-01-01T12:00:00Z")
        self.assertEqual([row["id"] for row in closed["items"]], [self.execution])
        self.assertEqual([row["id"] for row in self.page(BOARD + "?kind=closed&outcome=cancelled")["items"]],
                         [self.execution])
        self.assertEqual(self.page(BOARD + "?kind=closed&outcome=failed")["items"], [])
        self.assertEqual(self.page(BOARD + "?kind=closed&closed_after=2025-01-03T00:00:00Z")["items"], [])
        rollup = self.page("/v1/operator/groups/wave-2")["rollup"]
        self.assertEqual((rollup["open"], rollup["outcomes"]["cancelled"]), (0, 1))

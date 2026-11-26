from dataclasses import asdict, replace
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen

from phalanx import dispatch, intake, operator_api
from phalanx.intake import record, steer
from phalanx.intake_models import Intake, Steering
from phalanx.operator_api import closed_page, conditional_read, filtered_open_page, open_page, short_title
from phalanx.reconciliation import Observation, record as record_observation
from phalanx.store import APPLICATION_ID, connect, initialize, write_transaction
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


class SummaryWaitTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime.fromisoformat(LATER)
        self.row = {
            "id": "execution", "status": Status.QUEUED, "stage": Stage.DEVELOPMENT,
            "writer_id": "writer", "writer_ended_at": None, "writer_building": False,
            "building_requested_at": None, "writer_confirmed_at": None,
            "writer_nonce": "nonce", "writer_created_at": EARLY,
            "handoff_head": None, "pr_head_sha": None,
            "wait_since": LATER, "blocked_count": 0, "retry_at": None,
        }
        self.context = {"pickup": {"capacity": {"paused": False}, "holds": {}, "rework": None}}
        self.state = {"wait": None}

    def select(self):
        return operator_api.summary_wait(self.row, self.state, self.now, self.context)

    def test_writerless_queue_uses_only_pickup_even_with_competing_waits(self):
        self.state["wait"] = {"reason": "Persisted wait", "trigger": "reply"}
        self.row.update(blocked_count=2, building_requested_at=EARLY, retry_at=2**53)
        self.context["pickup"]["capacity"]["paused"] = True
        self.context["pickup"]["holds"]["execution"] = "Hold"
        for writer_id, ended_at in ((None, None), ("writer", EARLY)):
            self.row.update(writer_id=writer_id, writer_ended_at=ended_at)
            for eligible in (False, True):
                decision = {"eligible": eligible, "reason": "Pickup reason", "trigger": "Pickup trigger"}
                with self.subTest(writer_id=writer_id, eligible=eligible), patch.object(
                    operator_api.pickup, "decision", return_value=decision,
                ) as pickup, patch.object(operator_api.dispatch, "building_wait") as building:
                    wait, since, projected = self.select()
                    self.assertEqual(wait, {} if eligible else {
                        "reason": decision["reason"], "trigger": decision["trigger"],
                    })
                    self.assertEqual(since, LATER)
                    self.assertEqual(projected, decision)
                    pickup.assert_called_once_with(self.row, self.now, self.context["pickup"])
                    building.assert_not_called()

    def test_rework_waits_override_persisted_waits_but_capacity_respects_dependencies(self):
        self.row.update(status=Status.WAITING_FOR_EXTERNAL, stage=Stage.DELIVERY,
                        building_requested_at=EARLY, handoff_head="a" * 40, pr_head_sha="b" * 40)
        self.state["wait"] = {"reason": "Waiting for CI", "trigger": "Checks finish"}
        with patch.object(operator_api.dispatch, "building_wait", return_value="Building capacity exhausted"):
            self.assertEqual(self.select()[0]["reason"], "Building capacity exhausted")
        with patch.object(operator_api.dispatch, "building_wait", return_value=None):
            self.assertEqual(self.select()[0]["reason"], "Building capacity available; retry acquisition")
        self.row["writer_building"] = True
        self.assertEqual(self.select()[0]["reason"], "Writer is building or reviewing PR rework")
        self.row.update(writer_building=False, blocked_count=1)
        self.assertEqual(self.select()[0]["reason"], "Awaiting publication handoff " + "a" * 40)
        self.row["pr_head_sha"] = "a" * 40
        self.assertEqual(self.select()[0], self.state["wait"])
        self.state["wait"] = None
        self.assertEqual(self.select()[0]["reason"], "Waiting for 1 prerequisite")

    def test_retained_writer_queue_and_launch_fallbacks_preserve_precedence_and_age(self):
        self.state["wait"] = {"reason": "Persisted wait", "trigger": "reply"}
        self.row.update(blocked_count=2, retry_at=int(self.now.timestamp() * 1000) + 1000)
        self.context["pickup"]["capacity"]["paused"] = True
        self.context["pickup"]["holds"]["execution"] = "Hold"
        self.assertEqual(self.select()[0], self.state["wait"])
        self.state["wait"] = None
        self.assertEqual(self.select()[0]["reason"], "Waiting for 2 prerequisites")
        self.row["blocked_count"] = 0
        self.assertEqual(self.select()[0]["reason"], "Dispatch is paused")
        self.context["pickup"]["capacity"]["paused"] = False
        self.assertEqual(self.select()[0]["reason"], "Retry scheduled for 2025-01-02T00:00:01+00:00")
        self.row["retry_at"] = int(self.now.timestamp() * 1000)
        self.assertEqual(self.select()[0]["reason"], "Hold")
        self.context["pickup"]["holds"].clear()
        self.assertEqual(self.select(), ({}, LATER, None))
        self.row["status"] = Status.RUNNING
        wait, since, projected = self.select()
        self.assertIn("Native writer launch unconfirmed", wait["reason"])
        self.assertEqual((since, projected), (EARLY, None))
        for updates in ({"writer_confirmed_at": EARLY}, {"writer_ended_at": EARLY}):
            with self.subTest(updates=updates):
                original = self.row.copy()
                self.row.update(updates)
                self.assertEqual(self.select(), ({}, LATER, None))
                self.row = original
        self.state["stopping"] = {"reason": "Stop"}
        self.assertEqual(self.select(), ({}, LATER, None))


class OperatorTest(ServerFixture, unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "queue.sqlite3"
        initialize(self.path)
        with write_transaction(self.path) as db:
            db.execute("INSERT INTO repositories VALUES ('repo', 'https://github.com/a/b', ?)", (EARLY,))
            for number in [*range(1, 12), 4999, 5000]:
                insert(db, number)
            transition(db, 5000, 2, WAITING)
            transition(db, 5000, 3, WAITING, LATEST)
            for number in [6000, *range(7996, 8000)]:
                insert(db, number, self.closed(number))
            for version in range(2, 5):
                transition(db, 10, version, QUEUED)
            if self._testMethodName == "test_deep_queue_pages_seek_to_cursor":
                for number in range(12, 4999):
                    insert(db, number)
                for number in range(6001, 7996):
                    insert(db, number, self.closed(number))
                for number in range(1000):
                    db.execute("INSERT INTO github_observations VALUES (?, '', '', ?, 1, ?, ?, ?)",
                               (identifier(4901), str(number), EARLY, EARLY, '{"pr": null}'))
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

    def test_briefs_are_detail_only_and_titles_invalidate_polling_without_changing_execution(self):
        item_id, execution_id = identifier(100001), identifier(1)
        criteria = ["<img src=x onerror=alert(1)>", "Complete " + "long requirement " * 200]
        context = ["A plain note\nwith a second line", r"docs\work-items.md",
                   "https://example.com/brief", "javascript:alert(1)"]
        with write_transaction(self.path) as db:
            request = {"goal": "Immutable work goal", "acceptance_criteria": criteria,
                       "context_links": context, "private_result": {"secret": "not projected"}}
            db.execute("UPDATE work_items SET request_json=? WHERE id=?", (json.dumps(request), item_id))
        current = f"/v1/operator/items/{item_id}"
        timeline = f"{BOARD}/{execution_id}/timeline"
        for path in (current, timeline):
            detail = self.page(path)
            self.assertEqual(detail["item"]["acceptance_criteria"], criteria)
            self.assertEqual(detail["item"]["context_links"], context)
            self.assertNotIn("private_result", detail["item"])
            self.assertEqual(self.get(path)[0], 401)
        for row in self.page(BOARD + "?limit=3")["items"]:
            self.assertNotIn("acceptance_criteria", row)
            self.assertNotIn("context_links", row)
        for path in (f"{BOARD}/{identifier(2)}/timeline", f"/v1/operator/items/{identifier(100002)}"):
            empty = self.page(path)["item"]
            self.assertEqual((empty["acceptance_criteria"], empty["context_links"]), ([], []))
        with urlopen(Request(self.url + current, headers={"Authorization": f"Bearer {OWNER}"})) as response:
            validator = response.headers["ETag"]
        before = self.get(f"/v1/items/{item_id}", OWNER)[1]
        title = "Require independent review before auto-merging PRs"
        self.assertEqual(self.request(f"/v1/items/{item_id}/title", OWNER,
                                      {"expected_revision": 0, "title": title},
                                      {"Idempotency-Key": "title"}, method="PUT")[0], 200)
        self.assertEqual(self.request(current, OWNER, headers={"If-None-Match": validator})[0], 200)
        self.assertEqual(self.page(current)["item"]["title"], title)
        self.assertEqual(self.page(timeline)["item"]["title"], title)
        self.assertEqual(self.page(BOARD + "?q=auto-merging")["items"][0]["title"], title)
        self.assertEqual(self.get(f"/v1/items/{item_id}", OWNER)[1], before)
        self.assertEqual(self.request(f"/v1/items/{item_id}/title", OWNER,
                                      {"expected_revision": 1, "title": None},
                                      {"Idempotency-Key": "clear-title"}, method="PUT")[0], 200)
        self.assertEqual(self.page(current)["item"]["title"], "Immutable work goal")

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
        self.assertIsNone(newest["output_revision"])

        pr = PullRequest("https://github.com/example/project/pull/123", Revision("head", "base"))
        ready = Execution(Target.READY, Stage.DELIVERY, Status.COMPLETED, final_output=pr)
        merged = Execution(Target.MERGED, Stage.DELIVERY, Status.COMPLETED,
                           final_output=replace(pr, merge_commit="merge"))
        with write_transaction(self.path) as db:
            transition(db, 7, 2, ready, LATEST)
            transition(db, 8, 2, merged, LATEST)
        results = self.page(BOARD + "?kind=closed&limit=2")["items"]
        self.assertEqual([row["outcome"] for row in results], ["checked_in", "ready_for_check_in"])
        self.assertEqual(results[0]["output"], {"kind": "pr", "url": pr.url})
        self.assertEqual([row["output_revision"] for row in results],
                         [{"head": "head", "base": "base"}] * 2)
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
        self.assertEqual((row["goal"], row["target"], row["created_at"]), (goal, "report", EARLY))
        self.assertEqual(self.page(f"{BOARD}/{identifier(5000)}/timeline")["item"]["title"], row["title"])
        self.assertEqual(self.page(f"{BOARD}?limit=2")["items"][1]["target"], "report")
        self.assertEqual(short_title("word " * 40), ("word " * 15).strip() + "…")
        self.assertEqual(short_title("x" * 100), "x" * 79 + "…")
        self.assertEqual(short_title("  \n "), "Untitled")

    def test_closed_rerun_uses_execution_target_not_original_request(self):
        pr = PullRequest("https://github.com/example/project/pull/144", Revision("head", "base"))
        with write_transaction(self.path) as db:
            db.execute("UPDATE work_items SET request_json=? WHERE id=?",
                       (json.dumps({"goal": "Ready-only rerun", "target": "checked_in"}), identifier(100007)))
            transition(db, 7, 2, Execution(Target.READY, Stage.DELIVERY,
                                           Status.COMPLETED, final_output=pr), LATEST)
        row = self.page(BOARD + "?kind=closed&limit=1")["items"][0]
        self.assertEqual((row["id"], row["target"], row["outcome"]),
                         (identifier(7), "ready_for_check_in", "ready_for_check_in"))
        self.assertEqual(self.page(f"{BOARD}/{identifier(7)}/timeline")["item"]["target"],
                         "ready_for_check_in")

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
        self.assertEqual([row["version"] for row in first["events"]], [4, 3])
        with write_transaction(self.path) as db:
            db.execute("INSERT INTO attempts VALUES (?, ?, ?, ?, NULL)",
                       ("attempt", identifier(10), QUEUED.stage, LATEST))
            transition(db, 10, 5, replace(QUEUED, status=Status.RUNNING), LATEST, "attempt")
        later = self.page(f"{route}?limit=2&cursor={first['next_cursor']}")
        self.assertEqual(later["attempt"], "attempt")
        self.assertEqual(later["item"]["stage_owner"], later["attempt"])
        self.assertEqual([row["version"] for row in later["events"]], [2, 1])
        with write_transaction(self.path) as db:
            db.execute("UPDATE attempts SET ended_at=? WHERE id='attempt'", (LATEST,))
        ended = self.page(route)
        self.assertIsNone(ended["attempt"])
        self.assertIsNone(ended["item"]["stage_owner"])
        self.assertEqual(self.get(f"{BOARD}/{identifier(11)}/timeline?cursor={first['next_cursor']}", OWNER)[0], 400)
        self.assertEqual(self.get(f"{BOARD}?cursor={first['next_cursor']}", OWNER)[0], 400)
        self.assertIsNone(self.page(f"{BOARD}/{identifier(11)}/timeline")["next_cursor"])

    def test_item_deep_link_resolves_current_execution_and_preserves_rerun_links(self):
        item_id, previous_id, current_id = identifier(106000), identifier(6000), identifier(9000)
        route = f"/v1/operator/items/{item_id}"
        self.assertEqual(self.get(route)[0], 401)
        self.assertEqual(self.get("/v1/operator/items/invalid!", OWNER)[0], 422)
        self.assertEqual(self.get(f"/v1/operator/items/{identifier(0)}", OWNER)[0], 404)
        self.assertEqual(self.page(route)["item"]["id"], previous_id)
        with urlopen(Request(self.url + route, headers={"Authorization": f"Bearer {OWNER}"})) as response:
            etag = response.headers["ETag"]
        self.assertEqual(self.request(route, OWNER, headers={"If-None-Match": etag})[0], 304)
        self.assertEqual(self.request(route, headers={"If-None-Match": etag})[0], 401)
        with write_transaction(self.path) as db:
            data = json.dumps(asdict(QUEUED))
            db.execute(
                """INSERT INTO executions
                   (id, item_id, previous_id, version, state_json, created_at, updated_at)
                   VALUES (?, ?, ?, 1, ?, ?, ?)""",
                (current_id, item_id, previous_id, data, LATEST, LATEST),
            )
            record(db, current_id, 1, "rerun", {}, data, LATEST)
        status, current = self.get(route, RUNNER)
        self.assertEqual((status, current["item"]["id"]), (200, current_id))
        self.assertEqual(self.request(route, OWNER, headers={"If-None-Match": etag})[0], 200)
        old = self.page(f"{BOARD}/{previous_id}/timeline")
        new = self.page(f"{BOARD}/{current_id}/timeline")
        self.assertEqual((old["previous_execution_id"], old["next_execution_id"]), (None, current_id))
        self.assertEqual((new["previous_execution_id"], new["next_execution_id"]), (previous_id, None))
        self.assertEqual(old["item"]["status"], "cancelled")
        self.assertEqual(new["item"]["status"], "queued")

    def test_backward_result_reasons_correlate_the_recorded_attempt_and_revision(self):
        candidate, head = "a" * 40, "b" * 40
        pr_url = "https://github.com/a/b/pull/42"
        cases = [
            (Stage.SELF_REVIEW, {
                "outcome": "changes_required", "prevalidated_candidate": candidate,
                "local_checks": [{"name": "unit", "outcome": "failed",
                                  "reason": "Timed out after 1800 seconds"},
                                 {"name": "lint", "outcome": "passed"}],
            }, candidate, "changes_required; local check unit failed: "
                         "Timed out after 1800 seconds; at aaaaaaaaaaaa"),
            (Stage.PUBLICATION, {
                "outcome": "changes_required", "issue": "merge_conflict",
                "pull": {"head_sha": head}, "pr_url": pr_url,
            }, head, "changes_required; merge_conflict; at bbbbbbbbbbbb"),
            (Stage.SELF_REVIEW, {
                "outcome": "changes_required", "reviewed_candidate": candidate,
            }, candidate, "changes_required; at aaaaaaaaaaaa"),
        ]
        with write_transaction(self.path) as db:
            insert(db, 9000, Execution(Target.READY, cases[0][0]))
            for index, (stage, details, _, _) in enumerate(cases):
                version = 2 * index + 2
                if index:
                    transition(db, 9000, version - 1, Execution(Target.READY, stage))
                attempt = f"result-attempt-{index}"
                db.execute("INSERT INTO attempts VALUES (?, ?, ?, ?, ?)",
                           (attempt, identifier(9000), stage, EARLY, LATER))
                transition(db, 9000, version, Execution(Target.READY, Stage.DEVELOPMENT), attempt=attempt)
                db.execute("UPDATE transition_history SET details_json=? WHERE execution_id=? AND version=?",
                           (json.dumps(details), identifier(9000), version))
        route = f"{BOARD}/{identifier(9000)}/timeline"
        page = self.page(route + "?limit=2")
        self.assertEqual((page["item"]["item_id"], page["item"]["id"]),
                         (identifier(109000), identifier(9000)))
        events = page["events"]
        while page["next_cursor"]:
            page = self.page(f"{route}?limit=2&cursor={page['next_cursor']}")
            events.extend(page["events"])
        for index, (stage, _, sha, reason) in enumerate(cases):
            event = next(row for row in events if row["version"] == 2 * index + 2)
            self.assertEqual((event["from_stage"], event["stage"]), (stage, "development"))
            self.assertEqual(event["attempt_id"], f"result-attempt-{index}")
            self.assertEqual((event["candidate"], event["reason"]), (sha, reason))
            self.assertEqual(event["pr_url"], pr_url if index == 1 else None)
            self.assertNotIn("details", event)

    def test_forward_results_preserve_persisted_wait_and_explicit_reasons(self):
        wait_reason = "Waiting for required checks on " + "a" * 40
        explicit_reason = "Writer recorded why publication must wait"
        with write_transaction(self.path) as db:
            insert(db, 9000, Execution(Target.READY, Stage.SELF_REVIEW))
            for version, stage, reason in (
                (2, Stage.DELIVERY, None),
                (3, Stage.PUBLICATION, explicit_reason),
            ):
                state = Execution(Target.READY, stage, Status.WAITING_FOR_EXTERNAL,
                                  wait=Wait(wait_reason, "recheck"))
                transition(db, 9000, version, state)
                details = {"outcome": "succeeded", "candidate": "a" * 40, "reason": reason}
                db.execute("UPDATE transition_history SET details_json=? WHERE execution_id=? AND version=?",
                           (json.dumps(details), identifier(9000), version))
        events = self.page(f"{BOARD}/{identifier(9000)}/timeline")["events"]
        self.assertEqual((events[0]["from_stage"], events[0]["stage"]),
                         ("delivery", "publication"))
        self.assertEqual(events[0]["reason"], explicit_reason)
        self.assertEqual((events[1]["from_stage"], events[1]["stage"]),
                         ("self_review", "delivery"))
        self.assertEqual(events[1]["reason"], wait_reason)

    def test_result_reasons_are_bounded_redacted_and_only_use_cause_fields(self):
        candidate = "c" * 40
        secrets = ["ghp_" + "x" * 36, "github_pat_" + "y" * 60, "bearer-value",
                   "two word secret", "lease-value", "api-value", "runner-value", "json-value"]
        cause = (f"token={secrets[0]} {secrets[1]} Authorization: Bearer {secrets[2]} "
                 f'password="{secrets[3]}" lease: {secrets[4]} api_key={secrets[5]} '
                 f'PHALANX_RUNNER_TOKEN={secrets[6]} "access_token": "{secrets[7]}"')
        details = {
            "outcome": "failed", "candidate": candidate, "failure_reason": "Push rejected: " + cause,
            "local_checks": [{"name": "unit", "outcome": "failed", "reason": cause}],
            "goal": "PRIVATE TASK BODY", "report_markdown": "PRIVATE REPORT",
            "review_notes": "PRIVATE REVIEW", "question": {"prompt": "PRIVATE QUESTION"},
            "credentials": {"token": "PRIVATE CREDENTIAL"}, "lease_token": "PRIVATE LEASE",
        }
        with write_transaction(self.path) as db:
            insert(db, 9000, Execution(Target.READY, Stage.SELF_REVIEW))
            for version in (2, 3):
                transition(db, 9000, version, Execution(Target.READY, Stage.DEVELOPMENT))
                db.execute("UPDATE transition_history SET details_json=? WHERE execution_id=? AND version=?",
                           (json.dumps(details), identifier(9000), version))
                details["failure_reason"] = "Push rejected: " + cause + " " + "failure " * 200
                details["local_checks"] *= 50
        events = self.page(f"{BOARD}/{identifier(9000)}/timeline")["events"]
        for event in events[:2]:
            reason = event["reason"]
            self.assertTrue(reason.startswith("failed; local check unit failed: "))
            self.assertIn("[redacted]", reason)
            self.assertLessEqual(len(reason), 400)
            self.assertTrue(reason.endswith("; at cccccccccccc"))
            for secret in secrets + ["PRIVATE"]:
                self.assertNotIn(secret, reason)
            self.assertIn("; Push rejected: [redacted]", reason)

    def test_deep_queue_pages_seek_to_cursor(self):
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
        self.assertIn(f"Waiting on dependency {upstream}", blocked["item"]["wait_reason"])
        self.assertEqual(blocked["prerequisites"], [{
            "item_id": upstream, "title": "Work 1", "milestone": "report",
            "reason": "Waiting for prerequisite milestone report",
        }])
        with write_transaction(self.path) as db:
            steer(db, item["id"], execution["id"],
                  Steering(expected_version=execution["version"], dependencies=[]))
        ready = self.page(route)
        self.assertEqual(ready["item"]["blocked_prerequisite_count"], 0)
        self.assertEqual(ready["item"]["wait_reason"], "No live active Captain with pickup authority")
        self.assertIsNone(ready["item"]["next_action"])
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
        initialize(self.path)
        with write_transaction(self.path) as db:
            intake.register(db, "repo", "https://github.com/a/b.git")
            item = intake.create(db, Intake(repository_id="repo", goal="Ship it", target="ready_for_check_in", label="wave-1"))
            self.execution = item["execution"]["id"]
            pull = {"number": 5, "state": "open", "draft": True, "merged": False, "head_sha": "a" * 40}
            observe(db, self.execution, 2, Stage.SELF_REVIEW, pull,
                    gate_result={"state": "satisfied", "reasons": []}, duplicate_numbers=[6])
            observe(db, self.execution, 3, Stage.DEVELOPMENT, pull)
            observe(db, self.execution, 4, Stage.SELF_REVIEW, pull,
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
            observe(db, self.execution, 5, Stage.DEVELOPMENT, pull)
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

    def test_draft_completion_outcome_is_consistent_across_dashboard_reads(self):
        draft = PullRequest("https://github.com/a/b/pull/5", Revision("a" * 40, "b" * 40), draft=True)
        completed = Execution(Target.READY, Stage.DELIVERY, Status.COMPLETED, final_output=draft)
        with write_transaction(self.path) as db:
            transition(db, int(self.execution, 16), 5, completed, datetime.now(UTC).isoformat())
        with connect(self.path, mode="ro") as db:
            self.assertEqual(db.execute("SELECT outcome FROM executions WHERE id=?",
                                        (self.execution,)).fetchone()[0], "draft_published")
        self.assertEqual(self.row(BOARD + "?kind=closed")["outcome"], "draft_published")
        filtered = self.page(BOARD + "?kind=closed&outcome=draft_published")["items"]
        self.assertEqual([row["id"] for row in filtered], [self.execution])
        overview = self.page("/v1/operator/overview")
        self.assertEqual(overview["closed_24h"]["draft_published"], 1)
        self.assertEqual(overview["closed_24h"]["ready_for_check_in"], 0)
        self.assertEqual(overview["sections"]["delivered_24h"], 1)
        group = self.page("/v1/operator/groups/wave-1")
        self.assertEqual(group["rollup"]["outcomes"]["draft_published"], 1)
        self.assertEqual(group["rollup"]["outcomes"]["ready_for_check_in"], 0)
        self.assertEqual(group["items"][0]["outcome"], "draft_published")

    def test_conditional_reads_are_authenticated_scoped_and_invalidated_by_writes(self):
        routes = (BOARD, "/v1/operator/overview", "/v1/operator/groups/wave-1",
                  f"/v1/operator/items/{self.row(BOARD)['item_id']}")
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

    def test_read_context_is_shared_with_validators_and_304_skips_summary(self):
        from fastapi import Response
        item_id = self.row(BOARD)["item_id"]
        handlers = ((operator_api.list_executions, ()), (operator_api.overview, ()),
                    (operator_api.group, ("wave-1",)), (operator_api.current_item, (item_id,)))
        for handler, args in handlers:
            with self.subTest(handler=handler.__name__), ExitStack() as stack:
                reads = [stack.enter_context(patch.object(module, name, wraps=getattr(module, name)))
                         for module, name in ((dispatch, "settings"), (dispatch, "first_rework"),
                                              (operator_api, "availability_reports"), (operator_api.pickup, "active_holds"))]
                request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(database_path=self.path)),
                                          url=SimpleNamespace(path=handler.__name__, query=""), headers={})
                response = Response()
                handler(request, response, *args)
                self.assertEqual([read.call_count for read in reads], [1, 1, 1, 1])
                for read in reads:
                    read.reset_mock()
                request.headers["if-none-match"] = response.headers["ETag"]
                stack.enter_context(patch.object(operator_api, "SUMMARY", "INVALID SUMMARY"))
                self.assertEqual(handler(request, Response(), *args).status_code, 304)
                self.assertEqual([read.call_count for read in reads], [1, 1, 1, 1])

    def test_retry_expiry_changes_token_and_queue_reason_without_a_write(self):
        from fastapi import Response
        from starlette.requests import Request as APIRequest
        before = datetime.fromisoformat(LATER)
        with write_transaction(self.path) as db:
            ready_at = int((before + timedelta(seconds=1)).timestamp() * 1000)
            db.execute("UPDATE executions SET retry_at=?, ready_since=? WHERE id=?",
                       (ready_at, ready_at, self.execution))
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
        self.assertEqual(summary(row, before + timedelta(seconds=2), context)["wait_reason"],
                         "No live active Captain with pickup authority")

    def test_open_rows_always_say_why_without_capacity_inference(self):
        for row in self.page(BOARD)["items"]:
            self.assertTrue(row["wait_reason"] or row["next_action"], row)
            self.assertEqual(row["wait_reason"], "No live active Captain with pickup authority")
            self.assertIsNone(row["next_action"])
        overview = self.page("/v1/operator/overview")
        self.assertEqual(overview["sections"]["queued"], 2)
        self.assertEqual(overview["dispatch"], {"paused": False})
        self.assertNotIn("slots", overview)
        self.assertNotIn("runners", overview)
        self.assertIsNotNone(overview["oldest_queued_age_seconds"])

        from test_bootstrap import claim_stage

        queued_rows = self.page(BOARD)["items"]
        selected = next(row for row in queued_rows if row["id"] != self.execution)
        claimed = claim_stage(self.path, selected["item_id"], "x" * 32, "runner-a")
        rows = self.page(BOARD)["items"]
        running = next(row for row in rows if row["status"] == "running")
        self.assertIsNone(running["next_action"])
        self.assertIn("Native writer launch unconfirmed", running["wait_reason"])
        self.assertIn("fresh full native inventory", running["resume_trigger"])
        queued = next(row for row in rows if row["id"] == self.execution)
        self.assertEqual(queued["wait_reason"], "Captain pickup scope/readiness is uninitialized")
        self.assertIsNone(queued["next_action"])
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
        self.assertEqual(self.page(BOARD + "?stage=publication")["items"], [])
        self.assertEqual([row["id"] for row in self.page(BOARD + "?stage=self_review")["items"]], [self.execution])
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

    def test_group_rollup_is_bounded_to_its_item_page(self):
        with write_transaction(self.path) as db:
            for number in range(20, 25):
                insert(db, number)
                db.execute("UPDATE work_items SET request_json=json_set(request_json, '$.label', 'wave-1') "
                           "WHERE id=?", (identifier(number + 100000),))
            transition(db, 23, 2, replace(QUEUED, status=Status.CANCELLED,
                                         final_output=StoredReport("/v1/report")))
        cursor = None
        seen = set()
        totals = {"items": 0, "open": 0, "cancelled": 0}
        while True:
            url = "/v1/operator/groups/wave-1?limit=2"
            group = self.page(url + ("&cursor=" + cursor if cursor else ""))
            rollup = group["rollup"]
            self.assertEqual(rollup["scope"], "page")
            self.assertEqual(rollup["items"], len(group["items"]))
            self.assertLessEqual(rollup["items"], 2)
            for row in group["items"]:
                self.assertNotIn(row["id"], seen)
                seen.add(row["id"])
            totals["items"] += rollup["items"]
            totals["open"] += rollup["open"]
            totals["cancelled"] += rollup["outcomes"]["cancelled"]
            cursor = group["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(totals, {"items": 6, "open": 5, "cancelled": 1})

from dataclasses import asdict, replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from phalanx import dependencies, dispatch, finalization, intake
from phalanx.dispatch_api import Claim
from phalanx.intake_models import Intake, Rerun
from phalanx.store import MIGRATIONS, connect, migrate, write_transaction
from phalanx.transitions import Event, Evidence, Outcome, Stage, Status, StoredReport, Target, Wait, start

from test_bootstrap import OWNER, RUNNER, ServerFixture


class DependencyDispatchTests(ServerFixture, unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.path = Path(directory) / "queue.sqlite3"
        migrate(self.path)
        with write_transaction(self.path) as db:
            intake.register(db, "source", "https://example.com/source.git")
            intake.register(db, "consumer", "https://example.com/consumer.git")

    def file(self, db, repo="consumer", upstream=(), priority="normal"):
        links = [{"upstream_id": item["id"], "milestone": "report"} for item in upstream]
        return intake.create(db, Intake(repository_id=repo, goal="Review this repository",
                                        target=Target.REPORT, dependencies=links, priority=priority))

    def complete_report(self, db, item, *, simulate_missed_refresh=False):
        row = intake.current_execution(db, item["id"])
        markdown = "# Completed report"
        revision = intake.digest(markdown)
        output = StoredReport("/report/" + row["id"], revision)
        state = replace(intake.STATE.validate_json(row["state_json"]),
                        stage=Stage.REPORT_STORAGE, status=Status.RUNNING, report_draft=revision)
        db.execute("INSERT INTO reports VALUES (?, ?)", (row["id"], markdown))
        if simulate_missed_refresh:
            completed = replace(state, status=Status.COMPLETED, final_output=output)
            db.execute("UPDATE executions SET state_json=? WHERE id=?", (intake.encode(asdict(completed)), row["id"]))
        else:
            intake.apply(db, row, state, Event.RESULT, details={}, outcome=Outcome.SUCCEEDED,
                         evidence=Evidence(report_draft=revision, reviewed_report=revision, stored_report=output))
        return revision

    def claim(self, db, name):
        return dispatch.claim(db, Claim(runner_id="local", repository_ids=["consumer"]), intake.digest(name))

    def test_claim_skips_blocked_backlog_then_pins_both_upstreams(self):
        with write_transaction(self.path) as db:
            first = self.file(db, "source")
            second = self.file(db, "source")
            backlog_source = self.file(db, "source")
            dependent = self.file(db, upstream=[first, second], priority="urgent")
            for _ in range(100):
                self.file(db, upstream=[backlog_source], priority="urgent")
            older = self.file(db, priority="low")
            urgent = self.file(db, priority="urgent")
            db.execute("UPDATE executions SET ready_since=? WHERE item_id=?",
                       (int(time() * 1000) - 900_000, older["id"]))

            first_revision = self.complete_report(db, first)
            waiting = intake.inspect(db, dependent["id"])["execution"]["unmet_dependencies"]
            self.assertEqual([link["upstream_id"] for link in waiting], [second["id"]])
            self.assertEqual(self.claim(db, "older")["item_id"], older["id"])
            self.assertEqual(self.claim(db, "urgent")["item_id"], urgent["id"])
            self.assertEqual(self.claim(db, "blocked"), {"status": "no_eligible_work"})

            second_revision = self.complete_report(db, second)
            execution = dependent["execution"]
            assignment = self.claim(db, "resolved")
            self.assertEqual(assignment["item_id"], dependent["id"])
            queued = self.file(db, upstream=[first])
            expected = {
                first["execution"]["id"]: first_revision,
                second["execution"]["id"]: second_revision,
            }
            self.assertEqual({pin["execution_id"]: pin["revision"] for pin in assignment["dependencies"]}, expected)
            current = intake.current_execution(db, first["id"])
            intake.rerun(db, first["id"], current["id"],
                         Rerun(expected_version=current["version"], target=Target.REPORT))
            waits = intake.inspect(db, queued["id"])["execution"]["unmet_dependencies"]
            self.assertEqual([wait["upstream_id"] for wait in waits], [first["id"]])
            self.assertEqual(self.claim(db, "resolved"), assignment)
            history = intake.timeline(db, dependent["id"], execution["id"])
            self.assertEqual(history[-1]["details"]["dependencies"], assignment["dependencies"])

    def test_fanout_resumes_from_durable_cursor_in_bounded_batches(self):
        batch = dependencies.REFRESH_BATCH_SIZE
        with write_transaction(self.path) as db:
            source = self.file(db, "source")
            ids = sorted(self.file(db, upstream=[source])["id"] for _ in range(2 * batch + 1))
            self.complete_report(db, source)

        def checkpoint(expected, cursor):
            with connect(self.path, mode="ro") as db:
                self.assertEqual(db.execute(
                    "SELECT count(*) FROM executions WHERE dependency_wait_json IS NOT NULL"
                ).fetchone()[0], expected)
                job = db.execute("SELECT cursor FROM dependency_refresh_jobs").fetchone()
                self.assertEqual(None if job is None else job["cursor"], cursor)

        checkpoint(batch + 1, ids[batch - 1])
        self.assertTrue(dependencies.process_refresh(self.path))
        checkpoint(1, ids[2 * batch - 1])
        self.assertTrue(dependencies.process_refresh(self.path))
        checkpoint(0, None)
        self.assertFalse(dependencies.process_refresh(self.path))

    def test_pending_report_finalization_keeps_consumer_blocked_until_storage(self):
        with write_transaction(self.path) as db:
            source = self.file(db, "source")
            consumer = self.file(db, upstream=[source])
            row = intake.current_execution(db, source["id"])
            markdown = "# Reviewed report"
            revision = intake.digest(markdown)
            db.execute("INSERT INTO report_drafts VALUES (?, ?, ?)", (row["id"], revision, markdown))
            state = replace(intake.STATE.validate_json(row["state_json"]), stage=Stage.REPORT_STORAGE,
                            status=Status.RUNNING, report_draft=revision)
            pending = intake.apply(db, row, state, Event.RESULT, details={}, outcome=Outcome.SUCCEEDED,
                                   evidence=Evidence(report_draft=revision, reviewed_report=revision))
            self.assertEqual(pending["state"]["pending_output"], Status.COMPLETED)
            waits = intake.inspect(db, consumer["id"])["execution"]["unmet_dependencies"]
            self.assertEqual(waits[0]["reason"], "Waiting for prerequisite report finalization")
            self.assertEqual(self.claim(db, "pending")["status"], "no_eligible_work")

        self.assertEqual(finalization.attempt(self.path, row["id"])["state"]["status"], Status.COMPLETED)
        with write_transaction(self.path) as db:
            assignment = self.claim(db, "stored")
            self.assertEqual(assignment["item_id"], consumer["id"])
            self.assertEqual(assignment["dependencies"][0]["revision"], revision)

    def test_released_schema_migrates_pending_report_and_operator_progress(self):
        path = self.path.with_name("released.sqlite3")
        markdown = "# Reviewed report before upgrade"
        revision = intake.digest(markdown)
        pending = replace(
            start(Target.REPORT), stage=Stage.REPORT_STORAGE, status=Status.WAITING_FOR_EXTERNAL,
            pending_output=Status.COMPLETED, report_draft=revision, wait=Wait("Store report", "finalize"),
        )
        with connect(path, mode="rwc") as db:
            for migration in MIGRATIONS[:5]:
                for statement in migration:
                    db.execute(statement)
            db.execute("PRAGMA user_version = 5")
            intake.register(db, "source", "https://example.com/source.git")
            stamp = intake.timestamp()
            db.execute("INSERT INTO work_items VALUES ('item','source','{}',?)", (stamp,))
            data = intake.encode(asdict(pending))
            db.execute(
                """INSERT INTO executions(id,item_id,version,state_json,created_at,updated_at)
                   VALUES ('execution','item',1,?,?,?)""",
                (data, stamp, stamp),
            )
            intake.record(db, "execution", 1, "result", {}, data, stamp)
            db.execute("INSERT INTO report_drafts VALUES ('execution',?,?)", (revision, markdown))
            db.execute("INSERT INTO report_finalizations(execution_id,intent_version,due_at) VALUES ('execution',1,0)")
        migrate(path)
        with connect(path, mode="ro") as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 6)
            self.assertEqual(db.execute("SELECT wait_since FROM operator_progress").fetchone()[0], stamp)
        completed = finalization.attempt(path, "execution")
        self.assertEqual(completed["state"]["final_output"]["draft_revision"], revision)

    def test_restart_rechecks_missed_durable_milestone(self):
        server = self.start_server(self.path)
        with write_transaction(self.path) as db:
            source = self.file(db, "source")
            consumer = self.file(db, upstream=[source])
            self.complete_report(db, source, simulate_missed_refresh=True)
            self.assertEqual(self.claim(db, "before-restart")["status"], "no_eligible_work")
        self.stop_server(server)
        self.start_server(self.path)
        with write_transaction(self.path) as db:
            self.assertEqual(self.claim(db, "after-restart")["item_id"], consumer["id"])

    def test_owner_steering_is_versioned_and_rejects_active_changes(self):
        self.start_server(self.path)
        with write_transaction(self.path) as db:
            source = self.file(db, "source")
        status, consumer = self.request("/v1/items", OWNER, {
            "goal": "Review this repository", "repository_id": "consumer", "target": "report",
            "dependencies": [{"upstream_id": source["id"], "milestone": "report"}],
        })
        self.assertEqual(status, 201, consumer)
        self.assertEqual(consumer["execution"]["unmet_dependencies"][0]["upstream_id"], source["id"])
        execution = consumer["execution"]
        path = f"/v1/items/{consumer['id']}/executions/{execution['id']}"

        def steer(token, version):
            data = json.dumps({"expected_version": version, "goal": "New goal", "dependencies": []}).encode()
            request = Request(self.url + path, method="PATCH", data=data,
                              headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
            try:
                with urlopen(request, timeout=5) as response:
                    return response.status, json.load(response)
            except HTTPError as error:
                with error:
                    return error.code, json.load(error)

        self.assertEqual(steer(RUNNER, execution["version"])[0], 403)
        status, changed = steer(OWNER, execution["version"])
        self.assertEqual(status, 200, changed)
        self.assertEqual(changed["execution"]["unmet_dependencies"], [])
        self.assertEqual(steer(OWNER, execution["version"])[0], 409)
        with write_transaction(self.path) as db:
            claimed = self.claim(db, "steered")
            self.assertEqual(claimed["request"]["goal"], "New goal")
        self.assertEqual(steer(OWNER, claimed["execution"]["version"])[0], 409)
        with connect(self.path, mode="ro") as db:
            history = intake.timeline(db, consumer["id"], execution["id"])
            self.assertEqual(history[-2]["details"]["before"]["dependencies"][0]["upstream_id"], source["id"])

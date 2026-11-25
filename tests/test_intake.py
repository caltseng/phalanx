import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep, time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from phalanx.finalization import BACKOFF_SECONDS, attempt
from phalanx.intake import apply, check_current, claim_attempt, digest, encode
from phalanx.store import connect, write_transaction
from phalanx.transitions import Event, Evidence, Outcome, PullRequest, Revision, Stage, Status, Wait

from cli_runner import run_cli
from test_bootstrap import OWNER, RUNNER, ServerFixture, claim_stage
from timing import WAIT_SECONDS


LEASE = "x" * 32
REMOTE = "https://github.com/example/project.git"


class IntakeTest(ServerFixture, unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "queue.sqlite3"
        self.server = self.start_server(self.path)

    def submit(self, path, body, *, token=OWNER, key=None):
        headers = {}
        if key:
            headers["Idempotency-Key"] = key
        status, response = self.request(path, token, body, headers)
        if isinstance(response, dict) and "writer" in response:
            response = {key: value for key, value in response.items()
                        if key not in {"writer", "item_id"}}
        return status, response

    def create(self, target="report"):
        self.assertEqual(self.submit("/v1/repositories", {"id": "example", "remote": REMOTE})[0], 200)
        body = {"repository_id": "example", "goal": "Review the project", "target": target}
        status, item = self.submit("/v1/items", body, key="first")
        self.assertEqual(status, 201, item)
        base = f"/v1/items/{item['id']}"
        execution = f"{base}/executions/{item['execution']['id']}"
        return item, body, base, execution

    def claim(self, item, execution):
        claim = claim_stage(self.path, item["id"], LEASE)
        updated = claim["execution"]
        return {"expected_version": updated["version"], "attempt_id": claim["attempt_id"],
                "stage": updated["state"]["stage"], "outcome": "succeeded"}

    def test_title_metadata_is_owner_only_fenced_and_does_not_change_active_or_closed_work(self):
        item, _, base, execution = self.create()
        self.claim(item, execution)
        route = base + "/title"
        self.assertEqual(self.get(route, OWNER)[1]["revision"], 0)
        before = self.get(base, OWNER)[1]
        with connect(self.path) as db:
            attempts = list(map(tuple, db.execute("SELECT * FROM attempts")))
            writers = list(map(tuple, db.execute("SELECT * FROM writers")))
            history = list(map(tuple, db.execute("SELECT * FROM transition_history")))
        title = {"expected_revision": 0, "title": "Require independent review before auto-merging PRs"}
        headers = {"Idempotency-Key": "rename"}
        for token, status in ((None, 401), (RUNNER, 403), (LEASE, 403)):
            self.assertEqual(self.request(route, token, title, headers, method="PUT")[0], status)
        self.assertEqual(self.request(route, OWNER, title, headers | {"Origin": "null"},
                                      method="PUT")[0], 403)
        saved = self.request(route, OWNER, title, headers, method="PUT")
        self.assertEqual(saved[0], 200, saved)
        self.assertEqual((saved[1]["title"], saved[1]["revision"], saved[1]["changed_by"]),
                         (title["title"], 1, "owner"))
        self.assertEqual(self.request(route, OWNER, title, headers, method="PUT"), saved)
        self.assertEqual(self.request(route, OWNER, title, {"Idempotency-Key": "stale"},
                                      method="PUT")[0], 409)
        self.assertEqual(self.get(base, OWNER)[1], before)
        with connect(self.path) as db:
            self.assertEqual(list(map(tuple, db.execute("SELECT * FROM attempts"))), attempts)
            self.assertEqual(list(map(tuple, db.execute("SELECT * FROM writers"))), writers)
            self.assertEqual(list(map(tuple, db.execute("SELECT * FROM transition_history"))), history)
        for invalid in ("", " ", "two\nlines", "x" * 201):
            self.assertEqual(self.request(route, OWNER, dict(title, title=invalid),
                                          headers, method="PUT")[0], 422)
        # Complete real report stages without replacing or interrupting the open attempt.
        with connect(self.path) as db:
            attempt_id = db.execute("SELECT id FROM attempts WHERE ended_at IS NULL").fetchone()[0]
        state = before["execution"]
        draft = "A completed report."
        from hashlib import sha256
        revision = sha256(draft.encode()).hexdigest()
        for stage, evidence in (
            ("report_drafting", {"report_markdown": draft}),
            ("report_review", {"report_revision": revision}),
            ("report_storage", {"report_revision": revision}),
        ):
            payload = {"expected_version": state["version"], "attempt_id": attempt_id, "stage": stage,
                       "outcome": "succeeded", **evidence}
            status, state = self.request(execution + "/result", LEASE, payload)
            self.assertEqual(status, 200, state)
            with connect(self.path) as db:
                attempt = db.execute("SELECT id FROM attempts WHERE ended_at IS NULL").fetchone()
                attempt_id = attempt["id"] if attempt else None
        closed = self.get(base, OWNER)[1]
        self.assertEqual(closed["execution"]["state"]["status"], "completed")
        cleared = self.request(route, OWNER, {"expected_revision": 1, "title": None},
                               {"Idempotency-Key": "clear"}, method="PUT")
        self.assertEqual((cleared[0], cleared[1]["revision"], cleared[1]["title"]), (200, 2, None))
        self.assertEqual(self.get(base, OWNER)[1], closed)
        with connect(self.path) as db:
            audit = list(db.execute("SELECT title, changed_by FROM item_titles ORDER BY revision"))
            self.assertEqual([tuple(row) for row in audit], [(title["title"], "owner"), (None, "owner")])

    def test_busy_store_returns_retryable_503(self):
        item, body, _, _ = self.create()
        request = Request(self.url + "/v1/items", data=json.dumps(body).encode("utf-8"), method="POST",
                          headers={"Authorization": f"Bearer {OWNER}", "Content-Type": "application/json",
                                   "Idempotency-Key": "first"})
        with connect(self.path) as db:
            db.execute("BEGIN EXCLUSIVE")
            with self.assertRaises(HTTPError) as caught:
                urlopen(request, timeout=30)
            db.execute("ROLLBACK")
        with caught.exception as response:
            self.assertEqual(response.code, 503)
            self.assertEqual(response.headers["Retry-After"], "1")
            self.assertIn("retry", json.load(response)["detail"])
        with urlopen(request, timeout=30) as response:
            self.assertEqual((response.code, json.load(response)), (201, item))
        # A permanent store error must stay a 500 rather than look retryable.
        with connect(self.path) as db:
            db.execute("ALTER TABLE work_items RENAME TO work_items_gone")
        with self.assertRaises(HTTPError) as caught:
            urlopen(Request(self.url + f"/v1/items/{item['id']}",
                            headers={"Authorization": f"Bearer {OWNER}"}), timeout=30)
        with caught.exception as response:
            self.assertEqual(response.code, 500)

    def test_intake_restart_idempotency_auth_and_cli(self):
        self.assertEqual(self.submit("/v1/repositories", {"id": "example", "remote": REMOTE},
                                     token=RUNNER)[0], 403)
        item, body, base, execution = self.create()
        self.assertEqual(self.get(base)[0], 401)
        self.assertEqual(self.get(base, RUNNER), (200, item))
        self.assertEqual(self.submit("/v1/items", body, token=RUNNER, key="first")[0], 403)
        self.assertEqual(self.get(execution + "/timeline", OWNER)[1][0]["event"], "intake")

        self.stop_server(self.server)
        self.server = self.start_server(self.path)
        self.assertEqual(self.submit("/v1/items", body, key="first"), (201, item))
        self.assertEqual(self.submit("/v1/items", dict(body, goal="Changed"), key="first")[0], 409)
        for command in (["inspect", item["id"]],
                        ["intake", "example", "Review the project", "--target", "report",
                         "--key", "first"]):
            cli = run_cli(
                ["--api-url", self.url, *command],
                env={"PHALANX_OWNER_TOKEN": OWNER},
            )
            self.assertEqual(cli.returncode, 0, cli.stderr)
            self.assertEqual(json.loads(cli.stdout), item)

    def test_label_cli_intake_steering_and_clear(self):
        self.create()
        command = ["--api-url", self.url, "intake",
                   "example", "Grouped report", "--target", "report", "--label", "PH-29a"]
        cli = run_cli(command, env={"PHALANX_OWNER_TOKEN": OWNER})
        self.assertEqual(cli.returncode, 0, cli.stderr)
        item = json.loads(cli.stdout)
        self.assertEqual(item["label"], "PH-29a")
        route = f"/v1/items/{item['id']}/executions/{item['execution']['id']}"
        for label in ("wave.2", None):
            status, item = self.request(route, OWNER, {
                "expected_version": item["execution"]["version"], "label": label,
            }, method="PATCH")
            self.assertEqual(status, 200, item)
            self.assertEqual(item.get("label"), label)
        status, _ = self.request(route, OWNER, {
            "expected_version": item["execution"]["version"], "label": "bad label",
        }, method="PATCH")
        self.assertEqual(status, 422)

    def test_pr_intake_defaults_to_checked_in_and_keeps_explicit_targets(self):
        self.assertEqual(self.submit("/v1/repositories", {"id": "example", "remote": REMOTE})[0], 200)
        status, item = self.submit("/v1/items", {"repository_id": "example", "goal": "Fix the bug"})
        self.assertEqual(status, 201, item)
        self.assertEqual((item["target"], item["execution"]["state"]["target"]),
                         ("checked_in", "checked_in"))
        for extra, target in (([], "checked_in"), (["--target", "ready_for_check_in"], "ready_for_check_in"),
                              (["--target", "report"], "report")):
            with self.subTest(target=target):
                cli = run_cli(
                    ["--api-url", self.url,
                     "intake", "example", "Fix the bug", *extra],
                    env={"PHALANX_OWNER_TOKEN": OWNER},
                )
                self.assertEqual(cli.returncode, 0, cli.stderr)
                self.assertEqual(json.loads(cli.stdout)["execution"]["state"]["target"], target)

    def test_default_publication_mode_keeps_the_pre_upgrade_fingerprint(self):
        item, body, _, _ = self.create(target="ready_for_check_in")
        # The request as fingerprinted before publication_mode existed.
        before = {**body, "context_links": [], "acceptance_criteria": [], "target_branch": None,
                  "priority": "normal"}
        with connect(self.path, mode="ro") as db:
            stored, = db.execute("SELECT request_digest FROM request_receipts WHERE idempotency_key='first'"
                                 ).fetchone()
        self.assertEqual(stored, digest(encode(before)))
        # A retry from before the upgrade, or one naming the default, returns the original item.
        self.assertEqual(self.submit("/v1/items", body, key="first"), (201, item))
        self.assertEqual(self.submit("/v1/items", dict(body, publication_mode="ready"), key="first"), (201, item))
        self.assertEqual(self.submit("/v1/items", dict(body, publication_mode="draft"), key="first")[0], 409)

    def test_cli_cancel_uses_current_execution_and_version(self):
        item, _, base, execution = self.create()

        def cancel():
            return run_cli(
                ["--api-url", self.url, "cancel", item["id"],
                 "--reason", "Stale item"],
                env={"PHALANX_OWNER_TOKEN": OWNER},
            )

        cli = cancel()
        self.assertEqual(cli.returncode, 0, cli.stderr)
        cancelled = json.loads(cli.stdout)
        self.assertEqual(cancelled["id"], item["execution"]["id"])
        self.assertGreater(cancelled["version"], item["execution"]["version"])
        events = self.get(execution + "/timeline", OWNER)[1]
        self.assertEqual(sum(event["event"] == "cancel" for event in events), 1)
        self.assertEqual(next(event for event in events if event["event"] == "cancel")["details"]["reason"],
                         "Stale item")
        again = cancel()
        self.assertEqual(again.returncode, 1)
        self.assertIn("HTTP 409", again.stderr)

    def test_report_review_loop_and_verified_output_survive_restart(self):
        item, _, base, route = self.create()
        path = route + "/result"
        claim = self.claim(item, item["execution"])
        self.assertEqual(self.submit(path, claim, token=LEASE)[0], 409)
        body = dict(claim, report_markdown="# Draft 1\n")
        self.assertEqual(self.submit(path, body, token=OWNER)[0], 403)
        self.assertEqual(self.submit(path, body, token="z" * 32)[0], 401)
        self.assertEqual(self.submit(path, dict(body, stage="report_review"),
                                     token=LEASE)[0], 409)
        status, reviewing = self.submit(path, body, token=LEASE, key="draft")
        self.assertEqual(status, 200, reviewing)
        revision = reviewing["state"]["report_draft"]
        self.assertEqual(self.get(route + "/drafts/" + revision, RUNNER), (200, "# Draft 1\n"))
        self.assertEqual(self.submit(path, body, token=LEASE)[0], 409)
        self.assertEqual(self.submit(path, body, token=LEASE, key="draft"), (200, reviewing))

        claim = self.claim(item, reviewing)
        bad_review = dict(claim, report_revision="0" * 64)
        self.assertEqual(self.submit(path, bad_review, token=LEASE)[0], 409)
        status, redraft = self.submit(path, dict(claim, outcome="changes_required", report_revision=revision),
                                     token=LEASE)
        self.assertEqual(status, 200, redraft)
        self.assertEqual(redraft["state"]["stage"], "report_drafting")
        claim = self.claim(item, redraft)
        status, reviewing = self.submit(path, dict(claim, report_markdown="# Final\n"),
                                       token=LEASE)
        self.assertEqual(status, 200, reviewing)
        revision = reviewing["state"]["report_draft"]
        claim = self.claim(item, reviewing)
        status, storage = self.submit(path, dict(claim, report_revision=revision), token=LEASE)
        self.assertEqual(status, 200, storage)
        claim = self.claim(item, storage)
        self.assertEqual(self.submit(path, claim, token=LEASE)[0], 409)
        self.assertEqual(self.get(route + "/report", OWNER)[0], 404)
        # Reconcile a matching artifact already present from an interrupted write.
        with write_transaction(self.path) as db:
            db.execute("INSERT INTO reports VALUES (?, ?)", (storage["id"], "# Final\n"))
            db.execute("""CREATE TRIGGER no_duplicate_report BEFORE INSERT ON reports
                          BEGIN SELECT RAISE(ABORT, 'duplicate report write'); END""")
        status, complete = self.submit(path, dict(claim, report_revision=revision), token=LEASE)
        self.assertEqual(status, 200, complete)
        self.assertEqual(complete["state"]["status"], "completed")
        self.stop_server(self.server)
        self.server = self.start_server(self.path)
        self.assertEqual(self.get(base, OWNER)[1]["execution"], complete)
        self.assertEqual(self.get(complete["state"]["final_output"]["reference"], OWNER), (200, "# Final\n"))

    def test_pr_result_rejects_fabricated_provider_evidence_and_stale_fence(self):
        item, _, base, route = self.create("ready_for_check_in")
        path = route + "/result"
        claim = self.claim(item, item["execution"])
        with write_transaction(self.path) as db:
            db.execute("UPDATE attempts SET lease_digest='revoked' WHERE id=?", (claim["attempt_id"],))
        body = dict(claim, candidate="abc")
        self.assertEqual(self.submit(path, body, token=LEASE)[0], 409)
        with write_transaction(self.path) as db:
            db.execute("UPDATE attempts SET lease_digest=? WHERE id=?",
                       (digest(LEASE), claim["attempt_id"]))
        status, publication = self.submit(path, body, token=LEASE)
        self.assertEqual(status, 200, publication)
        claim = self.claim(item, publication)
        fake_pr = {"url": "https://github.com/example/project/pull/1", "draft": True,
                   "revision": {"head": "abc", "base": "def"}}
        self.assertEqual(self.submit(path, dict(claim, pr=fake_pr), token=LEASE)[0], 422)
        self.assertEqual(self.submit(path, claim, token=LEASE)[0], 409)
        active = self.get(base, OWNER)[1]["execution"]["state"]
        self.assertEqual(active["status"], "running")
        self.assertIsNone(active["primary_pr"])
        self.assertIsNone(active["final_output"])

    def test_cancel_active_rerun_and_preserve_history(self):
        item, _, base, route = self.create()
        claim = self.claim(item, item["execution"])
        cancellation = {"expected_version": claim["expected_version"], "reason": "Scope changed"}
        self.assertEqual(self.submit(route + "/cancel", cancellation, token=RUNNER)[0], 403)
        status, stopping = self.submit(route + "/cancel", cancellation, key="cancel")
        self.assertEqual(status, 200, stopping)
        self.assertEqual(stopping["state"]["stopping"], "cancelled")
        external = {
            "expected_version": stopping["version"], "attempt_id": claim["attempt_id"],
            "stage": claim["stage"], "reason": "Waiting for CI", "gate": "ci",
            "revision": {"head": "head-one", "base": "base-one"},
        }
        self.assertEqual(self.submit(route + "/external-wait", external, token=LEASE, key="stopping")[0], 409)
        self.assertIsNone(stopping["state"]["final_output"])
        self.assertEqual(self.submit(route + "/cancel", cancellation, key="cancel"), (200, stopping))
        self.assertEqual(self.submit(route + "/result", dict(claim, report_markdown="# Late"),
                                     token=LEASE)[0], 409)
        stop = {field: claim[field] for field in ("attempt_id", "stage")}
        stop["expected_version"] = stopping["version"]
        status, cancelled = self.submit(route + "/stop-confirmed", stop, token=LEASE)
        self.assertEqual(status, 200, cancelled)
        self.assertEqual(cancelled["state"]["status"], "cancelled")
        report = self.get(route + "/report", RUNNER)
        self.assertEqual(report[0], 200)
        self.assertIn("Scope changed", report[1])
        rerun = {"expected_version": cancelled["version"], "target": "report"}
        status, replacement = self.submit(route + "/rerun", rerun, key="rerun")
        self.assertEqual(status, 200, replacement)
        self.assertEqual(self.submit(route + "/rerun", rerun, key="rerun"), (200, replacement))
        self.assertEqual(replacement["previous_id"], cancelled["id"])
        self.assertEqual(self.get(base + "/executions", OWNER)[1], [cancelled, replacement])
        self.assertEqual([event["event"] for event in self.get(route + "/timeline", OWNER)[1]],
                         ["intake", "claim", "cancel", "stop_confirmed", "finalize"])
        self.assertEqual(self.submit(route + "/rerun", rerun)[0], 409)

    def test_cancel_before_first_claim(self):
        _, _, _, route = self.create()
        status, closed = self.submit(route + "/cancel", {"expected_version": 1, "reason": "Withdrawn"})
        self.assertEqual(status, 200, closed)
        self.assertEqual(closed["state"]["status"], "cancelled")
        self.assertIn("No stages claimed", self.get(route + "/report", OWNER)[1])

    def test_cancel_scheduled_external_wait_without_an_open_attempt(self):
        item, _, _, route = self.create("ready_for_check_in")
        claim = self.claim(item, item["execution"])
        status, waiting = self.submit(route + "/external-wait", {
            key: claim[key] for key in ("expected_version", "attempt_id", "stage")
        } | {"reason": "Waiting for CI", "gate": "ci", "revision": {"head": "head", "base": "base"},
             "retry_after_seconds": 60}, token=LEASE, key="wait")
        self.assertEqual(status, 200, waiting)
        status, closed = self.submit(
            route + "/cancel", {"expected_version": waiting["version"], "reason": "Withdrawn"},
        )
        self.assertEqual(status, 200, closed)
        self.assertEqual(closed["state"]["status"], "cancelled")
        self.assertIn("development", self.get(route + "/report", OWNER)[1])
        self.assertEqual([event["event"] for event in self.get(route + "/timeline", OWNER)[1]],
                         ["intake", "claim", "external_wait", "cancel", "stop_confirmed", "finalize"])

    def test_input_wait_reply_survives_restart_and_queues_same_stage_once(self):
        item, _, _, route = self.create()
        claim = self.claim(item, item["execution"])
        question = {"prompt": "Which scope?", "options": ["Current", "Expanded"],
                    "proposed_default": "Current", "handoff": "Draft is in notes.md"}
        result = dict(claim, outcome="needs_input")
        self.assertEqual(self.submit(route + "/result", result, token=LEASE)[0], 409)
        status, waiting = self.submit(route + "/result", dict(result, question=question),
                                      token=LEASE, key="wait")
        self.assertEqual(status, 200, waiting)
        self.assertEqual(waiting["state"]["status"], "waiting_for_input")
        with connect(self.path) as db:
            self.assertIsNotNone(db.execute("SELECT ended_at FROM attempts WHERE id=?",
                                            (claim["attempt_id"],)).fetchone()[0])

        self.stop_server(self.server)
        self.server = self.start_server(self.path)
        self.assertEqual(self.get("/v1/owner/inbox", RUNNER)[0], 403)
        self.assertEqual(self.get("/v1/owner/inbox")[0], 401)
        status, inbox = self.get("/v1/owner/inbox", OWNER)
        self.assertEqual(status, 200)
        self.assertEqual(len(inbox), 1)
        self.assertEqual(inbox[0]["question"], question)
        self.assertEqual(inbox[0]["wait"]["trigger"], "owner reply")
        path = route + "/reply"
        answer = {"expected_version": waiting["version"], "answer": "Use current scope"}
        self.assertEqual(self.submit(path, answer, token=RUNNER, key="answer")[0], 403)
        self.assertEqual(self.submit(path, answer)[0], 422)
        self.assertEqual(self.submit(path, dict(answer, expected_version=claim["expected_version"]),
                                     key="stale")[0], 409)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.submit, path, answer, key="answer")
            replay = pool.submit(self.submit, path, answer, key="answer")
            status, queued = first.result()
            self.assertEqual(replay.result(), (status, queued))
        self.assertEqual(status, 200, queued)
        self.assertEqual(queued["version"], waiting["version"] + 1)
        self.assertEqual(queued["state"]["stage"], waiting["state"]["stage"])
        self.assertEqual(queued["state"]["status"], "queued")
        self.assertEqual(self.submit(path, answer, key="another")[0], 409)
        self.assertEqual(self.submit(path, dict(answer, answer="Expanded"), key="answer")[0], 409)
        self.assertEqual(self.get("/v1/owner/inbox", OWNER), (200, []))

        self.stop_server(self.server)
        self.server = self.start_server(self.path)
        cli = run_cli(
            ["--api-url", self.url, "reply", item["id"],
             waiting["id"], answer["answer"], "--version", str(waiting["version"]),
             "--key", "answer"],
            env={"PHALANX_OWNER_TOKEN": OWNER},
        )
        self.assertEqual(cli.returncode, 0, cli.stderr)
        self.assertEqual(json.loads(cli.stdout), queued)
        from test_bootstrap import claim_stage

        resumed = claim_stage(self.path, item["id"], "y" * 32)
        self.assertEqual(resumed["execution"]["state"]["stage"], waiting["state"]["stage"])
        self.assertEqual(resumed["input_reply"], {
            "answer": answer["answer"], "answered_by": "owner", "wait_version": waiting["version"],
            "question": question,
        })
        self.assertEqual([entry["event"] for entry in self.get(route + "/timeline", OWNER)[1]],
                         ["intake", "claim", "result", "reply", "claim"])

    def test_captain_answered_reply_resumes_and_records_who_answered(self):
        item, _, _, route = self.create()
        claim = self.claim(item, item["execution"])
        question = {"prompt": "Which test runner?", "options": ["unittest", "pytest"],
                    "proposed_default": "unittest", "handoff": "Tests are drafted"}
        status, waiting = self.submit(route + "/result",
                                      dict(claim, outcome="needs_input", question=question),
                                      token=LEASE, key="wait")
        self.assertEqual(status, 200, waiting)
        self.assertIsNone(self.get(f"/v1/items/{item['id']}", OWNER)[1]["last_reply"])
        answer = {"expected_version": waiting["version"], "answer": "unittest"}
        self.assertEqual(self.submit(route + "/reply", dict(answer, answered_by="worker"),
                                     key="bad")[0], 422)
        cli = run_cli(
            ["--api-url", self.url, "reply", item["id"],
             waiting["id"], "unittest", "--version", str(waiting["version"]),
             "--key", "captain", "--answered-by", "captain"],
            env={"PHALANX_OWNER_TOKEN": OWNER},
        )
        self.assertEqual(cli.returncode, 0, cli.stderr)
        self.assertEqual(json.loads(cli.stdout)["state"]["status"], "queued")
        self.assertEqual(self.submit(route + "/reply", dict(answer, answered_by="captain"),
                                     key="captain")[1], json.loads(cli.stdout))
        reply = self.get(route + "/timeline", OWNER)[1][-1]
        self.assertEqual((reply["event"], reply["details"]["answered_by"]), ("reply", "captain"))
        board = self.get(f"/v1/operator/executions/{waiting['id']}/timeline", OWNER)[1]["events"][0]
        self.assertEqual((board["event"], board["answered_by"], board["reason"]),
                         ("reply", "captain", "Answered by captain"))
        last_reply = self.get(f"/v1/items/{item['id']}", OWNER)[1]["last_reply"]
        self.assertEqual((last_reply["answer"], last_reply["answered_by"]), ("unittest", "captain"))
        from test_bootstrap import claim_stage

        resumed = claim_stage(self.path, item["id"], "y" * 32)
        self.assertEqual(resumed["execution"]["state"]["stage"], waiting["state"]["stage"])
        self.assertEqual(resumed["input_reply"], {
            "answer": "unittest", "answered_by": "captain", "wait_version": waiting["version"],
            "question": question,
        })

    def test_external_wait_is_due_after_restart_and_recheck_does_not_pass_gate(self):
        item, intake_body, _, route = self.create("ready_for_check_in")
        claim = self.claim(item, item["execution"])
        body = {key: claim[key] for key in ("expected_version", "attempt_id", "stage")}
        body.update(reason="Waiting for CI", gate="ci",
                    revision={"head": "head-one", "base": "base-one"}, retry_after_seconds=60)
        path = route + "/external-wait"
        self.assertEqual(self.submit(path, body, token="z" * 32, key="wait")[0], 401)
        earliest = int(time() * 1000) + 60_000
        status, waiting = self.submit(path, body, token=LEASE, key="wait")
        self.assertEqual(status, 200, waiting)
        self.assertEqual(waiting["state"]["status"], "waiting_for_external")
        self.assertEqual(self.submit(path, body, token=LEASE, key="wait"),
                         (200, waiting))
        due_path = "/v1/runner/due-rechecks?limit=1"
        self.assertEqual(self.get(due_path, OWNER)[0], 403)
        self.assertEqual(self.get(due_path, RUNNER), (200, []))
        recheck = {"expected_version": waiting["version"]}
        self.assertEqual(self.submit(route + "/recheck", recheck, token=RUNNER, key="poll")[0], 409)
        status, unrelated = self.submit("/v1/items", intake_body, key="unrelated")
        self.assertEqual(status, 201, unrelated)
        with write_transaction(self.path) as db:
            due_at = db.execute("SELECT due_at_ms FROM external_rechecks").fetchone()[0]
            self.assertGreaterEqual(due_at, earliest)
            self.assertLessEqual(due_at, int(time() * 1000) + 65_000)
            db.execute("UPDATE external_rechecks SET due_at_ms=1")
            db.execute("INSERT INTO external_rechecks VALUES (?, ?, 0)",
                       (unrelated["execution"]["id"], unrelated["execution"]["version"] + 1))
        self.stop_server(self.server)
        self.server = self.start_server(self.path)
        status, batch = self.get(due_path, RUNNER)
        self.assertEqual(status, 200)
        self.assertEqual(len(batch), 1)
        self.assertEqual(batch[0]["revision"], body["revision"])
        self.assertEqual(batch[0]["version"], waiting["version"])
        self.assertEqual(self.submit(route + "/recheck", recheck, key="owner")[0], 403)
        status, queued = self.submit(route + "/recheck", recheck, token=RUNNER, key="poll")
        self.assertEqual(status, 200, queued)
        self.assertEqual(queued["state"]["status"], "queued")
        self.assertEqual(queued["state"]["stage"], waiting["state"]["stage"])
        self.assertIsNone(queued["state"]["final_output"])
        self.assertEqual(self.submit(route + "/recheck", recheck, token=RUNNER, key="poll"),
                         (200, queued))
        self.assertEqual(self.submit(route + "/recheck", recheck, token=RUNNER, key="dupe")[0], 409)
        self.assertEqual(self.get(due_path, RUNNER), (200, []))

    def test_external_wait_limit_asks_owner_after_elapsed_time_on_one_revision(self):
        item, _, _, route = self.create("ready_for_check_in")
        claim = self.claim(item, item["execution"])

        def wait(claim, key):
            body = {name: claim[name] for name in ("expected_version", "attempt_id", "stage")}
            body.update(reason="Draft checks pending", gate="draft_checks", retry_after_seconds=5,
                        revision={"head": "head-one", "base": "base-one"}, wait_limit_seconds=60)
            status, updated = self.submit(route + "/external-wait", body, token=LEASE, key=key)
            self.assertEqual(status, 200, updated)
            return updated

        def recheck(waiting, key):
            with write_transaction(self.path) as db:
                db.execute("UPDATE external_rechecks SET due_at_ms=1")
            status, queued = self.submit(route + "/recheck", {"expected_version": waiting["version"]},
                                         token=RUNNER, key=key)
            self.assertEqual(status, 200, queued)
            return self.claim(item, queued)

        def pending_since(waiting):
            with write_transaction(self.path) as db:
                return db.execute("""SELECT json_extract(details_json, '$.pending_since_ms')
                    FROM transition_history WHERE execution_id=? AND version=?""",
                                  (item["execution"]["id"], waiting["version"])).fetchone()[0]

        first = wait(claim, "wait-1")
        self.assertEqual(first["state"]["status"], "waiting_for_external")
        second = wait(recheck(first, "poll-1"), "wait-2")
        self.assertEqual(second["state"]["status"], "waiting_for_external")
        since = pending_since(first)
        self.assertEqual(pending_since(second), since)
        with write_transaction(self.path) as db:
            db.execute("""UPDATE transition_history SET details_json=json_set(details_json,
                '$.pending_since_ms', ?) WHERE execution_id=? AND version=?""",
                       (since - 60_000, item["execution"]["id"], second["version"]))
        asked = wait(recheck(second, "poll-2"), "wait-3")
        self.assertEqual(asked["state"]["status"], "waiting_for_input")
        with write_transaction(self.path) as db:
            question = db.execute("""SELECT json_extract(details_json, '$.question.prompt')
                FROM transition_history WHERE execution_id=? AND version=?""",
                                  (item["execution"]["id"], asked["version"])).fetchone()[0]
        self.assertIn("Draft-only checks never reported", question)

    def test_trusted_success_can_open_a_human_wait_without_worker_question(self):
        item, _, _, route = self.create("ready_for_check_in")
        url = "https://github.com/example/project/pull/1"
        revision = Revision("head-one", "base-one")
        execution = item["execution"]
        with write_transaction(self.path) as db:
            row, state = check_current(db, item["id"], execution["id"], execution["version"])
            state = replace(state, stage=Stage.DELIVERY, primary_pr=url,
                            candidate=revision.head)
            claim = claim_attempt(db, row, state, runner_id="local", lease_digest=digest(LEASE),
                                  lease_expires_at=int(time()) + 3600)
            row, state = check_current(db, item["id"], execution["id"], claim["execution"]["version"])
            waiting = apply(
                db, row, replace(state, status=Status.WAITING_FOR_EXTERNAL), Event.PR_OBSERVED,
                attempt_id=claim["attempt_id"], details={},
                evidence=Evidence(pr=PullRequest(url, revision, closed=True)),
                wait=Wait("PR closed without merge", "owner reply"),
            )
        self.assertEqual(waiting["state"]["status"], "waiting_for_input")
        status, queued = self.submit(
            route + "/reply", {"expected_version": waiting["version"], "answer": "Investigate"},
            key="resume-closed-pr",
        )
        self.assertEqual(status, 200, queued)
        self.assertEqual(queued["state"]["stage"], "delivery")

    def test_cancel_storage_error_rolls_back_and_recovers_after_restart(self):
        item, _, base, route = self.create()
        execution_id = item["execution"]["id"]
        with write_transaction(self.path) as db:
            # Fail after inserting the report, at the terminal timeline write.
            db.execute("""CREATE TRIGGER terminal_write_outage BEFORE INSERT ON transition_history
                          WHEN json_extract(NEW.state_json, '$.status') = 'cancelled'
                          BEGIN SELECT RAISE(ABORT, 'simulated outage'); END""")
        body = {"expected_version": 1, "reason": "Withdrawn during outage"}
        status, pending = self.submit(route + "/cancel", body, key="cancel")
        self.assertEqual(status, 200, pending)
        self.assertEqual(pending["state"]["pending_output"], "cancelled")
        self.assertEqual(pending["state"]["status"], "waiting_for_external")
        self.assertIsNone(pending["state"]["final_output"])
        self.assertEqual(self.get(route + "/report", OWNER)[0], 404)

        self.stop_server(self.server)
        with write_transaction(self.path) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM attempts").fetchone()[0], 0)
            db.execute("DROP TRIGGER terminal_write_outage")
            db.execute("UPDATE report_finalizations SET due_at=0 WHERE execution_id=?", (execution_id,))
        self.server = self.start_server(self.path)
        # Background finalization runs on the server's schedule; poll generously.
        deadline = monotonic() + WAIT_SECONDS
        while monotonic() < deadline:
            closed = self.get(base, OWNER)[1]["execution"]
            if closed["state"]["status"] == "cancelled":
                break
            sleep(0.05)
        self.assertEqual(closed["state"]["status"], "cancelled")
        self.assertEqual(self.submit(route + "/cancel", body, key="cancel"), (200, closed))
        self.assertIn("Withdrawn during outage", self.get(route + "/report", OWNER)[1])
        with connect(self.path, mode="ro") as db:
            self.assertEqual(db.execute("SELECT count(*) FROM reports").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM report_finalizations").fetchone()[0], 0)

    def test_failed_attempt_ends_and_stalled_storage_can_be_retried(self):
        item, _, _, route = self.create("checked_in")
        claim = self.claim(item, item["execution"])
        result = dict(claim, outcome="failed", wait={"reason": "Build unavailable", "trigger": "owner reply"})
        status, waiting = self.submit(route + "/result", result, token=LEASE)
        self.assertEqual(status, 200, waiting)
        body = {"expected_version": waiting["version"], "reason": "Build remains unavailable"}
        with write_transaction(self.path) as db:
            db.execute("INSERT INTO reports VALUES (?, ?)", (waiting["id"], "# Conflicting report"))
        self.assertEqual(self.submit(route + "/close-failure", body, token=RUNNER)[0], 403)
        status, pending = self.submit(route + "/close-failure", body)
        self.assertEqual(status, 200, pending)
        self.assertEqual(pending["state"]["pending_output"], "failed")
        self.stop_server(self.server)
        for _ in BACKOFF_SECONDS:
            with write_transaction(self.path) as db:
                db.execute("UPDATE report_finalizations SET due_at=0 WHERE execution_id=?", (pending["id"],))
            stalled = attempt(self.path, pending["id"])
        self.assertIn("stalled after 5 failures", stalled["state"]["wait"]["reason"])
        self.assertEqual(stalled["state"]["wait"]["trigger"], "owner retry finalization")
        self.assertIsNone(stalled["state"]["final_output"])
        self.assertEqual(attempt(self.path, pending["id"]), stalled)

        with write_transaction(self.path) as db:
            self.assertEqual(db.execute("SELECT markdown FROM reports").fetchone()[0], "# Conflicting report")
            self.assertEqual(db.execute("SELECT count(*) FROM attempts WHERE ended_at IS NULL").fetchone()[0], 0)
            db.execute("DELETE FROM reports WHERE execution_id=?", (pending["id"],))
        self.server = self.start_server(self.path)
        retry = {"expected_version": stalled["version"]}
        self.assertEqual(self.submit(route + "/retry-finalization", retry, token=RUNNER)[0], 403)
        status, closed = self.submit(route + "/retry-finalization", retry)
        self.assertEqual(status, 200, closed)
        self.assertEqual(closed["state"]["status"], "failed")
        report = self.get(route + "/report", OWNER)[1]
        for expected in ("# Failure report", "development (1)", "Build remains unavailable", "Next action"):
            self.assertIn(expected, report)

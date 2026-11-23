from datetime import UTC, datetime
from pathlib import Path
from secrets import token_urlsafe
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from pydantic import TypeAdapter

from phalanx import dispatch, finalization, intake, operator_api, pr_models, pr_observations, waits
from phalanx.dispatch_api import Claim, DispatchSettings, ReconciledStop
from phalanx.intake_models import AttemptEvent, Cancel, ExternalWait, Intake, Rerun, Result
from phalanx.store import connect, migrate, write_transaction
from phalanx.transitions import RUNNER_ONLY_SLOTS, RUNNER_ONLY_STAGES
from phalanx.workspaces import _item_branch

from test_bootstrap import OWNER, RUNNER, ServerFixture


NOW = 1_800_000_000
HEAD = "a" * 40
BASE = "b" * 40
ALL = sorted(["development", "pr_creation", "self_review", "pre_pr_validation", "pr_promotion",
              "pr_validation", "pr_reconciliation", "adversarial_review", "pr_revision",
              "report_drafting", "report_review", "report_storage"])
RESULTS = {"development": {"candidate": HEAD}, "self_review": {"reviewed_candidate": HEAD},
           "pre_pr_validation": {"prevalidated_candidate": HEAD}}
OBSERVATION = TypeAdapter(pr_models.Observation)
GATES = {"pr_promotion": "draft", "pr_validation": "ready", "pr_reconciliation": "ready"}
AGENT = sorted(set(ALL) - set(map(str, RUNNER_ONLY_STAGES)))
CAPACITIES = (1, 5, 10)


def offer(stages=ALL):
    return {"lease": token_urlsafe(32), "stages": sorted(stages)}


class Clock:
    def __init__(self, now):
        self.now = now

    def __enter__(self):
        self.patches = [patch.object(module, "time", lambda: self.now) for module in (dispatch, intake)]
        for active in self.patches:
            active.start()
        return self

    def __exit__(self, *exc):
        for active in self.patches:
            active.stop()


class StageContinuationTest(ServerFixture, unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "queue.sqlite3"
        migrate(self.path)
        with write_transaction(self.path) as db:
            intake.register(db, "repo", "git@github.com:Example/Repo.git")
        self.numbers = {}

    def create(self, *, priority="normal", target="ready_for_check_in", at=NOW, **fields):
        with write_transaction(self.path) as db:
            item = intake.create(db, Intake(repository_id="repo", goal="Ship it", target=target,
                                            target_branch="main", priority=priority, **fields))
            db.execute("UPDATE executions SET ready_since=? WHERE item_id=?", (at * 1000, item["id"]))
        self.numbers[item["id"]] = len(self.numbers) + 1
        return item

    def claim(self, runner_id="local", stages=ALL):
        lease = token_urlsafe(32)
        with write_transaction(self.path) as db:
            assignment = dispatch.claim(db, Claim(runner_id=runner_id, repository_ids=["repo"],
                                                  stages=stages), intake.digest(lease))
        return assignment, lease

    def finish(self, assignment, lease, continuation=None, **fields):
        """Submit a passing result for the assignment's stage, then run the server handoff."""
        item_id, execution = assignment["item_id"], assignment["execution"]
        stage = execution["state"]["stage"]
        claim = {"attempt_id": assignment["attempt_id"], "stage": stage,
                 "expected_version": execution["version"]}
        with write_transaction(self.path) as db:
            if fields or stage in RESULTS or stage.startswith("report"):
                body = Result(outcome="succeeded", **claim, **(fields or RESULTS.get(stage, {})))
                view = intake.result(db, item_id, execution["id"], body, intake.digest(lease))
            else:
                body = OBSERVATION.validate_python(self.observation(item_id, claim))
                view = pr_observations.submit(db, item_id, execution["id"], body, intake.digest(lease))
            return dispatch.hand_off(db, item_id, assignment["attempt_id"], view, continuation)

    def observation(self, item_id, claim):
        stage = claim["stage"]
        expected = {"head_ref": _item_branch(item_id), "base_ref": "main", "base_sha": BASE}
        number = self.numbers[item_id]
        body = dict(claim, expected=expected, pull={
            **expected, "number": number, "head_sha": HEAD, "draft": stage == "pr_creation",
            "head_repository": "Example/Repo", "base_repository": "example/repo",
            "state": "open", "merged": False,
        })
        if stage in GATES:
            requirement = {"name": "build", "kind": "check_run", "app_id": 123}
            body["gates"] = {"phase": GATES[stage], "number": number, "head_sha": HEAD, "base_sha": BASE,
                             "required": [requirement],
                             "observations": [dict(requirement, tested_sha=HEAD, outcome="passed")]}
        if stage == "adversarial_review":
            body["review"] = {"reviewer": "independent-review-chat", "summary": "Reviewed the diff",
                              "findings": []}
        return body

    def release(self, execution_id):
        with connect(self.path) as db:
            row = db.execute(
                """SELECT json_extract(details_json, '$.capacity_release') FROM transition_history
                   WHERE execution_id=? AND json_extract(details_json, '$.capacity_release') IS NOT NULL
                   ORDER BY version DESC LIMIT 1""", (execution_id,),
            ).fetchone()
        return row and row[0]

    def reserved(self, runner_id="local"):
        with connect(self.path) as db:
            return db.execute("SELECT count(*) FROM attempts WHERE runner_id=? AND ended_at IS NULL",
                              (runner_id,)).fetchone()[0]

    def state(self, item_id):
        with connect(self.path) as db:
            return intake.inspect(db, item_id)["execution"]["state"]

    def test_next_stage_keeps_the_slot_with_a_new_fenced_attempt(self):
        with write_transaction(self.path) as db:
            dispatch.set_capacity(db, "local", 1)
        with Clock(NOW):
            item = self.create()
            assignment, lease = self.claim()
            older = self.create(priority="urgent", at=NOW - 3600)
            continuation = offer()
            view = self.finish(assignment, lease, continuation)
            handed = view["continuation"]
            self.assertEqual((handed["status"], handed["item_id"], handed["runner_id"]),
                             ("claimed", item["id"], "local"))
            self.assertEqual(handed["execution"]["id"], assignment["execution"]["id"])
            self.assertEqual(handed["execution"]["state"]["stage"], "pr_creation")
            self.assertNotEqual(handed["attempt_id"], assignment["attempt_id"])
            self.assertEqual(self.reserved(), 1)
            # The continued pr_creation is runner-only, so the agent slot still takes the older item.
            self.assertEqual(self.claim()[0]["item_id"], older["id"])
            self.assertEqual(self.claim()[0]["status"], "at_capacity")
            with write_transaction(self.path) as db:
                with self.assertRaisesRegex(intake.Conflict, "no longer reserved"):
                    dispatch.renew(db, "local", assignment["attempt_id"], intake.digest(lease))
                with self.assertRaisesRegex(intake.Conflict, "no longer current"):
                    intake.result(db, item["id"], handed["execution"]["id"], Result(
                        expected_version=handed["execution"]["version"], attempt_id=assignment["attempt_id"],
                        stage="pr_creation", outcome="succeeded"), intake.digest(lease))
                dispatch.renew(db, "local", handed["attempt_id"], intake.digest(continuation["lease"]))
                history = intake.timeline(db, item["id"], handed["execution"]["id"])
                self.assertEqual(history[-1]["details"]["continued_from"], assignment["attempt_id"])
                self.assertEqual(db.execute("SELECT count(*) FROM attempts WHERE execution_id=? "
                                            "AND ended_at IS NULL", (handed["execution"]["id"],)).fetchone()[0], 1)

    def test_http_offer_is_part_of_the_replayable_receipt(self):
        with write_transaction(self.path) as db:
            dispatch.set_capacity(db, "local", 1)
        item = self.create(at=0)
        self.start_server(self.path)
        lease = token_urlsafe(32)
        status, assigned = self.request("/v1/dispatch/claim", RUNNER, {
            "runner_id": "local", "repository_ids": ["repo"], "stages": ALL}, {"Phalanx-Lease": lease})
        self.assertEqual(status, 200)
        execution = assigned["execution"]
        path = f"/v1/items/{item['id']}/executions/{execution['id']}/result"
        body = {"expected_version": execution["version"], "attempt_id": assigned["attempt_id"],
                "stage": "development", "outcome": "succeeded", "candidate": HEAD}
        headers = {"Phalanx-Lease": lease, "Idempotency-Key": "dev-1"}
        bad = dict(headers, **{"Phalanx-Next-Lease": "short", "Phalanx-Next-Stages": "pr_creation"})
        self.assertEqual(self.request(path, RUNNER, body, bad)[0], 422)
        bad = dict(headers, **{"Phalanx-Next-Lease": "n" * 32, "Phalanx-Next-Stages": "nonsense"})
        self.assertEqual(self.request(path, RUNNER, body, bad)[0], 422)
        offered = dict(headers, **{"Phalanx-Next-Lease": "n" * 32, "Phalanx-Next-Stages": "pr_creation,development"})
        status, accepted = self.request(path, RUNNER, body, offered)
        self.assertEqual(status, 200, accepted)
        handed = accepted["continuation"]
        self.assertEqual(handed["execution"]["state"]["stage"], "pr_creation")
        self.assertEqual(self.request(path, RUNNER, body, offered), (200, accepted))
        self.assertEqual(self.request(path, RUNNER, body, dict(offered, **{"Phalanx-Next-Lease": "m" * 32}))[0], 409)
        renew = f"/v1/dispatch/runners/local/attempts/{{}}/renew"
        self.assertEqual(self.request(renew.format(assigned["attempt_id"]), RUNNER, {},
                                      {"Phalanx-Lease": lease})[0], 409)
        self.assertEqual(self.request(renew.format(handed["attempt_id"]), RUNNER, {},
                                      {"Phalanx-Lease": "n" * 32})[0], 200)
        board = self.get(f"/v1/operator/executions/{execution['id']}/timeline", OWNER)[1]["item"]
        self.assertEqual((board["stage_owner"], board["slot_retained"], board["capacity_release"]),
                         (handed["attempt_id"], True, None))

    def test_each_result_release_path_records_its_reason_and_frees_the_slot(self):
        cases = {
            "runner did not offer a continuation": lambda a, l: self.finish(a, l),
            "runner cannot run pr_creation": lambda a, l: self.finish(a, l, offer(["development"])),
            "input wait": lambda a, l: self.submit(a, l, outcome="needs_input", question={
                "prompt": "Which API?", "handoff": "Two APIs fit."}),
            "retry backoff": lambda a, l: self.submit(a, l, outcome="failed", failure_reason="Network down"),
        }
        for reason, step in cases.items():
            with self.subTest(reason), Clock(NOW):
                item = self.create()
                assignment, lease = self.claim(stages=["development"])
                self.assertEqual(assignment["item_id"], item["id"])
                self.assertNotIn("continuation", step(assignment, lease))
                self.assertEqual(self.reserved(), 0)
                self.assertEqual(self.release(assignment["execution"]["id"]), reason)
                if reason.startswith("runner"):
                    # Left queued for general admission, as before continuations.
                    self.assertEqual(self.state(item["id"])["stage"], "pr_creation")
                    self.assertEqual(self.claim("publisher", ["pr_creation"])[0]["item_id"], item["id"])

    def submit(self, assignment, lease, **fields):
        execution = assignment["execution"]
        with write_transaction(self.path) as db:
            view = intake.result(db, assignment["item_id"], execution["id"], Result(
                expected_version=execution["version"], attempt_id=assignment["attempt_id"],
                stage=execution["state"]["stage"], **fields), intake.digest(lease))
            return dispatch.hand_off(db, assignment["item_id"], assignment["attempt_id"], view, offer())

    def test_external_wait_stop_pause_and_dependency_release_the_slot(self):
        with Clock(NOW):
            self.create()
            assignment, lease = self.claim()
            execution = assignment["execution"]
            with write_transaction(self.path) as db:
                view = waits.external_wait(db, assignment["item_id"], execution["id"], ExternalWait(
                    expected_version=execution["version"], attempt_id=assignment["attempt_id"],
                    stage="development", reason="Waiting for CI", gate="ci",
                    revision={"head": "h", "base": "b"}), intake.digest(lease))
                self.assertNotIn("continuation", dispatch.hand_off(
                    db, assignment["item_id"], assignment["attempt_id"], view, offer()))
            self.assertEqual(self.release(execution["id"]), "external wait")

            self.create()
            assignment, lease = self.claim()
            execution = assignment["execution"]
            with write_transaction(self.path) as db:
                stopping = intake.cancel(db, assignment["item_id"], execution["id"], Cancel(
                    expected_version=execution["version"], reason="Scope changed"))
                view = intake.stop_confirmed(db, assignment["item_id"], execution["id"], AttemptEvent(
                    expected_version=stopping["version"], attempt_id=assignment["attempt_id"],
                    stage="development"), intake.digest(lease))
                view = dispatch.hand_off(db, assignment["item_id"], assignment["attempt_id"], view, offer())
            self.assertNotIn("continuation", view)
            self.assertIn(self.release(execution["id"]), {"closed as cancelled", "finalizing output"})

            self.create()
            assignment, lease = self.claim()
            with write_transaction(self.path) as db:
                dispatch.set_dispatch(db, DispatchSettings(paused=True))
            self.assertNotIn("continuation", self.finish(assignment, lease, offer()))
            self.assertEqual(self.release(assignment["execution"]["id"]), "dispatch paused")
            self.assertEqual(self.reserved(), 0)
            with write_transaction(self.path) as db:
                dispatch.set_dispatch(db, DispatchSettings(paused=False))

        with write_transaction(self.path) as db:
            upstream = intake.create(db, Intake(repository_id="repo", goal="Upstream", target="report"))
        upstream_claim, upstream_lease = self.claim(stages=["report_drafting"])
        for stage in ("report_drafting", "report_review", "report_storage"):
            if stage != "report_drafting":
                upstream_claim, upstream_lease = self.claim(stages=[stage])
            fields = {"report_markdown": "# Up\n"} if stage == "report_drafting" else {}
            if stage != "report_drafting":
                fields = {"report_revision": intake.digest("# Up\n")}
            self.submit_plain(upstream_claim, upstream_lease, **fields)
        finalization.attempt(self.path, upstream["execution"]["id"])
        self.assertEqual(self.state(upstream["id"])["status"], "completed")
        dependent = self.create(at=0, dependencies=[{"upstream_id": upstream["id"], "milestone": "report"}])
        assignment, lease = self.claim(stages=["development"])
        self.assertEqual(assignment["item_id"], dependent["id"])
        with write_transaction(self.path) as db:
            current = intake.current_execution(db, upstream["id"])
            intake.rerun(db, upstream["id"], current["id"], Rerun(expected_version=current["version"],
                                                                 target="report"))
        self.assertNotIn("continuation", self.finish(assignment, lease, offer()))
        self.assertEqual(self.release(assignment["execution"]["id"]), "dependency wait")
        self.assertEqual(self.reserved(), 0)

    def submit_plain(self, assignment, lease, **fields):
        execution = assignment["execution"]
        with write_transaction(self.path) as db:
            intake.result(db, assignment["item_id"], execution["id"], Result(
                expected_version=execution["version"], attempt_id=assignment["attempt_id"],
                stage=execution["state"]["stage"], outcome="succeeded", **fields), intake.digest(lease))

    def test_crashed_continuation_expires_and_recovers_without_a_leak(self):
        with write_transaction(self.path) as db:
            dispatch.set_capacity(db, "local", 1)
        with Clock(NOW) as clock:
            item = self.create()
            assignment, lease = self.claim()
            handed = self.finish(assignment, lease, offer())["continuation"]
            clock.now = handed["lease_expires_at"]
            # The expired attempt stays reserved until recovery; nothing else is eligible meanwhile.
            self.assertEqual(self.claim()[0]["status"], "no_eligible_work")
            self.assertEqual(self.reserved(), 1)
            with write_transaction(self.path) as db:
                recovered = dispatch.recover_stopped(db, "local", handed["attempt_id"], ReconciledStop(
                    item_id=item["id"], expected_version=handed["execution"]["version"],
                    reconciliation="Runner crashed before launching the continuation"))
            self.assertEqual(self.release(handed["execution"]["id"]), "lease expired")
            self.assertEqual(self.reserved(), 0)
            clock.now = recovered["ready_since_ms"] // 1000
            again, _ = self.claim()
            self.assertEqual((again["item_id"], again["execution"]["state"]["stage"]), (item["id"], "pr_creation"))
            self.assertEqual(self.claim("other")[0]["status"], "no_eligible_work")
            self.assertEqual(self.reserved(), 1)

    def test_admitted_work_finishes_under_urgent_arrivals_and_new_work_ages_in(self):
        capacity = 2
        with write_transaction(self.path) as db:
            dispatch.set_capacity(db, "local", capacity)
            dispatch.set_dispatch(db, DispatchSettings(paused=False, aging_seconds=60))
        admitted, slots, finished = {}, [], []
        with Clock(NOW) as clock:
            first = [self.create(), self.create()]
            backlog = self.create(priority="low")
            for tick in range(60):
                self.create(priority="urgent", at=clock.now)
                while len(slots) < capacity:
                    assignment, lease = self.claim()
                    if assignment["status"] != "claimed":
                        break
                    admitted[assignment["item_id"]] = admitted.get(assignment["item_id"], 0) + 1
                    slots.append((assignment, lease))
                self.assertLessEqual(self.reserved(), capacity)
                held = []
                for assignment, lease in slots:
                    continuation = offer()
                    view = self.finish(assignment, lease, continuation)
                    if "continuation" in view:
                        held.append((view["continuation"], continuation["lease"]))
                    else:
                        finished.append((assignment["item_id"], view["state"]["status"]))
                slots = held
                clock.now += 10
                if backlog["id"] in admitted and all(item["id"] in dict(finished) for item in first):
                    break
        self.assertEqual([admitted[item["id"]] for item in first], [1, 1])
        for item in first:
            state = self.state(item["id"])
            self.assertEqual((state["status"], state["target"]), ("completed", "ready_for_check_in"))
        self.assertIn(backlog["id"], admitted)
        self.assertTrue(all(count == 1 for count in admitted.values()))
        self.assertTrue(all(status == "completed" for _, status in finished))

    def queue_at(self, stage, at, **fields):
        """Create an item, pass it through earlier stages, and leave `stage` queued since `at`."""
        item = self.create(**fields)
        while self.state(item["id"])["stage"] != stage:
            assignment, lease = self.claim("driver", [name for name in ALL if name != stage])
            self.assertEqual(assignment["item_id"], item["id"])
            self.finish(assignment, lease)
        with write_transaction(self.path) as db:
            db.execute("UPDATE executions SET ready_since=? WHERE item_id=?", (at * 1000, item["id"]))
        return item

    def fill_agents(self, capacity):
        with write_transaction(self.path) as db:
            dispatch.set_capacity(db, "local", capacity)
        for _ in range(capacity + 1):
            self.create(priority="urgent")
        for _ in range(capacity):
            self.assertEqual(self.claim(stages=AGENT)[0]["execution"]["state"]["stage"], "development")
        self.assertEqual(self.claim(stages=AGENT)[0]["status"], "at_capacity")

    def test_runner_only_stage_is_admitted_while_agents_fill_capacity(self):
        for capacity in CAPACITIES:
            with self.subTest(capacity=capacity), Clock(NOW):
                self.setUp()
                promotions = [self.queue_at("pr_promotion", NOW - 100 + n) for n in range(RUNNER_ONLY_SLOTS + 1)]
                self.fill_agents(capacity)
                self.assertIsNone(self.board(promotions[0]["id"])["wait_reason"])
                admitted_leases = [self.claim() for _ in range(RUNNER_ONLY_SLOTS)]
                admitted = [assignment for assignment, _ in admitted_leases]
                self.assertEqual([(row["item_id"], row["execution"]["state"]["stage"]) for row in admitted],
                                 [(item["id"], "pr_promotion") for item in promotions[:RUNNER_ONLY_SLOTS]])
                # The lane is bounded, and it never lends room to agent stages.
                self.assertEqual(self.claim()[0]["status"], "at_capacity")
                self.assertEqual(self.claim(stages=AGENT)[0]["status"], "at_capacity")
                self.assertEqual(self.reserved(), capacity + RUNNER_ONLY_SLOTS)
                self.assertEqual(self.state(promotions[-1]["id"])["status"], "queued")
                # A runner-only stage that can't be admitted shows why on the board.
                self.assertEqual(self.board(promotions[-1]["id"])["wait_reason"],
                                 "Waiting for runner-only lane capacity")
                self.finish(*admitted_leases[0])
                self.assertIsNone(self.board(promotions[-1]["id"])["wait_reason"])

    def board(self, item_id):
        with connect(self.path) as db:
            row = db.execute(f"{operator_api.SUMMARY} WHERE execution.item_id=?", (item_id,)).fetchone()
            now = datetime.now(UTC)
            return operator_api.summary(row, now, operator_api.dispatch_context(db, now))

    def test_runner_only_stages_keep_rank_order_under_sustained_agent_intake(self):
        for capacity in CAPACITIES:
            with self.subTest(capacity=capacity), Clock(NOW) as clock:
                self.setUp()
                promotions = [self.queue_at("pr_promotion", NOW - 100 + n)
                              for n in range(2 * RUNNER_ONLY_SLOTS + 1)]
                self.fill_agents(capacity)
                first = []
                for tick in range(20):
                    self.create(priority="urgent", at=clock.now)
                    lane = []
                    while (assignment := self.claim())[0]["status"] == "claimed":
                        self.assertIn(assignment[0]["execution"]["state"]["stage"], RUNNER_ONLY_STAGES)
                        lane.append(assignment)
                    self.assertTrue(lane)
                    self.assertLessEqual(len(lane), RUNNER_ONLY_SLOTS)
                    for assignment, lease in lane:
                        if assignment["execution"]["state"]["stage"] == "pr_promotion":
                            first.append(assignment["item_id"])
                        self.finish(assignment, lease)
                    clock.now += 10
                    if len(first) == len(promotions):
                        break
                self.assertEqual(first, [item["id"] for item in promotions])
                self.assertEqual(self.reserved(), capacity)

    def test_agent_stages_are_admitted_under_a_runner_only_stream(self):
        for capacity in CAPACITIES:
            with self.subTest(capacity=capacity), Clock(NOW) as clock:
                self.setUp()
                with write_transaction(self.path) as db:
                    dispatch.set_capacity(db, "local", capacity)
                for n in range(3 * RUNNER_ONLY_SLOTS):
                    self.queue_at("pr_promotion", NOW - 1000 + n, priority="urgent")
                agents = [self.create(at=NOW) for _ in range(capacity)]
                admitted = []
                for tick in range(3):
                    # Worker-first, as the Captain loop's general slots claim.
                    while (assignment := self.claim(stages=AGENT)[0])["status"] == "claimed":
                        admitted.append(assignment["item_id"])
                    self.assertEqual(len(admitted), capacity)
                    lane = []
                    while (assignment := self.claim())[0]["status"] == "claimed":
                        lane.append(assignment)
                    self.assertEqual(len(lane), RUNNER_ONLY_SLOTS)
                    for assignment, lease in lane:
                        self.finish(assignment, lease)
                    clock.now += 10
                self.assertCountEqual(admitted, [item["id"] for item in agents])
                self.assertEqual(self.reserved(), capacity)


if __name__ == "__main__":
    unittest.main()

"""Queue admission under sustained urgent arrivals and a mostly stalled backlog."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from secrets import token_urlsafe
from tempfile import TemporaryDirectory
from threading import Barrier
from unittest import TestCase
from unittest.mock import patch

from phalanx import dispatch, intake
from phalanx.dispatch_api import Claim, DispatchSettings
from phalanx.intake_models import Intake
from phalanx.store import connect, migrate, write_transaction
from phalanx.transitions import Event, Outcome, Wait


NOW = 1_800_000_000
CAPACITY = 8
CONTENDERS = 24
PARKED = 2_000
AGE_STEP = 300


class BacklogAdmissionTest(TestCase):
    def test_aged_work_advances_under_urgent_arrivals_without_overbooking(self):
        with TemporaryDirectory() as temporary:
            self.path = Path(temporary) / "queue.sqlite3"
            migrate(self.path)
            with write_transaction(self.path) as db:
                intake.register(db, "repo", "https://github.com/example/repo.git")
                dispatch.set_capacity(db, "local", CAPACITY)
                dispatch.set_dispatch(db, DispatchSettings(paused=False, aging_seconds=AGE_STEP))
                for _ in range(PARKED):
                    item = self.enqueue(db, "urgent", NOW - 3 * AGE_STEP)
                    intake.apply(db, *self.current(db, item), Event.CLAIM, details={})
                    claimed = dict(item, execution=dict(item["execution"], version=item["execution"]["version"] + 1))
                    self.ask(db, *self.current(db, claimed))
                aged = [self.enqueue(db, "low", NOW - 2 * AGE_STEP - offset)
                        for offset in reversed(range(CAPACITY + 2))]
                for _ in range(4 * CAPACITY):
                    self.enqueue(db, "urgent", NOW - 1)

            with patch.object(dispatch, "time", return_value=NOW):
                barrier = Barrier(CONTENDERS)

                def contend(_):
                    barrier.wait(timeout=10)
                    return self.claim()

                with ThreadPoolExecutor(CONTENDERS) as pool:
                    results = list(pool.map(contend, range(CONTENDERS)))

                claims = [result for result in results if result["status"] == "claimed"]
                self.assertEqual(len(claims), CAPACITY)
                self.assertEqual({claim["item_id"] for claim in claims},
                                 {item["id"] for item in aged[:CAPACITY]})
                self.assertEqual(sum(result["status"] == "at_capacity" for result in results),
                                 CONTENDERS - CAPACITY)

                for claim, next_item in zip(claims, aged[CAPACITY:]):
                    with write_transaction(self.path) as db:
                        row, state = intake.check_current(db, claim["item_id"],
                                                          claim["execution"]["id"],
                                                          claim["execution"]["version"])
                        self.ask(db, row, state, attempt_id=claim["attempt_id"])
                        self.enqueue(db, "urgent", NOW)
                    self.assertEqual(self.claim()["item_id"], next_item["id"])

            with connect(self.path, mode="ro") as db:
                self.assertEqual(db.execute(
                    "SELECT count(*) FROM attempts WHERE ended_at IS NULL"
                ).fetchone()[0], CAPACITY)
                self.assertEqual(db.execute(
                    "SELECT count(*) FROM executions WHERE status = 'waiting_for_input'"
                ).fetchone()[0], PARKED + 2)
                self.assertEqual(db.execute(
                    "SELECT count(*) FROM request_receipts WHERE scope = 'dispatch:claim'"
                ).fetchone()[0], CAPACITY + 2)

    @staticmethod
    def enqueue(db, priority, ready_at):
        item = intake.create(db, Intake(repository_id="repo", goal="Inspect the repository",
                                        target="report", priority=priority))
        db.execute("UPDATE executions SET ready_since=? WHERE id=?",
                   (ready_at * 1_000, item["execution"]["id"]))
        return item

    @staticmethod
    def current(db, item):
        execution = item["execution"]
        return intake.check_current(db, item["id"], execution["id"], execution["version"])

    @staticmethod
    def ask(db, row, state, attempt_id=None):
        intake.apply(db, row, state, Event.RESULT, details={"question": {"prompt": "Needs owner judgment"}},
                     attempt_id=attempt_id, outcome=Outcome.NEEDS_INPUT,
                     wait=Wait("Needs owner judgment", "Owner reply"))

    def claim(self):
        with write_transaction(self.path) as db:
            return dispatch.claim(db, Claim(runner_id="local", repository_ids=["repo"]),
                                  intake.digest(token_urlsafe(32)))

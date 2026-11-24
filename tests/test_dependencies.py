from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest

from phalanx.dependencies import Dependency, dependents, replace_dependencies, resolve_dependencies
from phalanx.intake import Conflict, current_execution, digest, encode, new_execution, register, timestamp
from phalanx.store import connect, migrate, write_transaction
from phalanx.transitions import PullRequest, Revision, Stage, Status, StoredReport, Target, start


class DependencyTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.path = Path(directory) / "dependencies.sqlite3"
        migrate(self.path)
        with write_transaction(self.path) as db:
            register(db, "consumer", "https://example.com/consumer")
            register(db, "producer", "https://example.com/producer")
            for item, repository, target in (
                ("consumer", "consumer", Target.REPORT),
                ("ready", "consumer", Target.REPORT),
                ("report", "producer", Target.REPORT),
                ("pr", "producer", Target.MERGED),
            ):
                db.execute(
                    "INSERT INTO work_items (id, repository_id, request_json, created_at) VALUES (?, ?, ?, ?)",
                    (item, repository, "{}", timestamp()),
                )
                new_execution(db, item, target)

    def set_state(self, db, item, state, markdown=None):
        execution_id = current_execution(db, item)["id"]
        db.execute("UPDATE executions SET state_json=? WHERE id=?", (encode(asdict(state)), execution_id))
        if markdown:
            db.execute("INSERT INTO reports VALUES (?, ?)", (execution_id, markdown))
        return execution_id

    def test_waits_for_both_upstreams_and_recovers_without_events(self):
        revision = Revision("reviewed-head", "reviewed-base")
        url = "https://example.com/producer/pull/1"
        with write_transaction(self.path) as db:
            replace_dependencies(db, "consumer", [
                Dependency("report", Target.REPORT), Dependency("pr", Target.MERGED),
            ])
            replace_dependencies(db, "ready", [Dependency("pr", Target.READY)])
            self.assertEqual(dependents(db, "pr"), ("consumer", "ready"))
            resolved, unmet = resolve_dependencies(db, "consumer")
            self.assertEqual(resolved, ())
            self.assertEqual({link.upstream_id for link in unmet}, {"report", "pr"})
            report_revision = digest("# Report")
            report = replace(start(Target.REPORT), stage=Stage.REPORT_STORAGE, status=Status.COMPLETED,
                             final_output=StoredReport("/report", report_revision))
            report_execution = self.set_state(db, "report", report, "# Report")
            ready = replace(start(Target.MERGED), stage=Stage.DELIVERY,
                            primary_pr=url, candidate=revision.head, ready_revision=revision)
            pr_execution = self.set_state(db, "pr", ready)

        with write_transaction(self.path) as db:
            resolved, unmet = resolve_dependencies(db, "consumer")
            self.assertEqual((resolved[0].execution_id, resolved[0].revision), (report_execution, report_revision))
            self.assertEqual([link.upstream_id for link in unmet], ["pr"])
            (pinned,), unmet = resolve_dependencies(db, "ready")
            self.assertEqual(unmet, ())
            self.assertEqual((pinned.execution_id, pinned.reviewed_revision), (pr_execution, revision))
            output = PullRequest(url, revision)
            self.set_state(db, "pr", replace(ready, target=Target.READY, status=Status.COMPLETED, final_output=output))
            self.assertIn("primary PR to merge", resolve_dependencies(db, "consumer")[1][0].reason)
            self.assertEqual(resolve_dependencies(db, "ready"), ((pinned,), ()))

            rerun = new_execution(db, "pr", Target.MERGED, previous_id=pr_execution)
            self.assertEqual(resolve_dependencies(db, "ready")[1][0].execution_id, rerun["id"])
            merged = replace(ready, stage=Stage.DELIVERY, status=Status.COMPLETED,
                             final_output=replace(output, merge_commit="verified-merge"))
            self.set_state(db, "pr", merged)
            resolved, unmet = resolve_dependencies(db, "consumer")
            self.assertEqual(unmet, ())
            self.assertEqual((resolved[0].execution_id, resolved[0].revision), (rerun["id"], "verified-merge"))
            self.assertEqual(resolved[0].reviewed_revision, revision)

    def test_failure_report_cannot_satisfy_report_prerequisite(self):
        with write_transaction(self.path) as db:
            replace_dependencies(db, "consumer", [Dependency("report", Target.REPORT)])
            state = replace(start(Target.REPORT), status=Status.FAILED,
                            final_output=StoredReport("/failure", digest("# Failure")))
            self.set_state(db, "report", state, "# Failure")
            resolved, (unmet,) = resolve_dependencies(db, "consumer")
            self.assertEqual(resolved, ())
            self.assertIn("failed", unmet.reason)

    def test_resolution_serializes_with_rerun(self):
        with write_transaction(self.path) as db:
            replace_dependencies(db, "consumer", [Dependency("report", Target.REPORT)])
            state = replace(start(Target.REPORT), status=Status.COMPLETED,
                            final_output=StoredReport("/report", digest("# Report")))
            previous_id = self.set_state(db, "report", state, "# Report")

        start_race = Barrier(2, timeout=5)

        def rerun():
            start_race.wait()
            with connect(self.path) as db:
                db.execute("BEGIN IMMEDIATE")
                execution = new_execution(db, "report", Target.REPORT, previous_id=previous_id)
                db.execute("COMMIT")
                return execution["id"]

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(rerun)
            start_race.wait()
            with write_transaction(self.path) as db:
                resolved, unmet = resolve_dependencies(db, "consumer")
                self.assertEqual(resolve_dependencies(db, "consumer"), (resolved, unmet))
            new_id = future.result()

        if resolved:
            self.assertEqual(resolved[0].execution_id, previous_id)
            self.assertEqual(unmet, ())
        else:
            self.assertEqual(unmet[0].execution_id, new_id)
        with write_transaction(self.path) as db:
            self.assertEqual(resolve_dependencies(db, "consumer")[1][0].execution_id, new_id)

    def test_invalid_replacement_preserves_previous_links(self):
        with write_transaction(self.path) as db:
            replace_dependencies(db, "consumer", [Dependency("report", Target.REPORT)])
            with self.assertRaisesRegex(Conflict, "itself"):
                replace_dependencies(db, "consumer", [Dependency("consumer", Target.REPORT)])
            with self.assertRaisesRegex(Conflict, "cycle"):
                replace_dependencies(db, "report", [Dependency("consumer", Target.REPORT)])
            self.assertEqual(dependents(db, "report"), ("consumer",))

    def test_concurrent_edits_cannot_create_cycle(self):
        start_edits = Barrier(2, timeout=5)

        def edit(item, upstream):
            start_edits.wait()
            try:
                with connect(self.path) as db:
                    db.execute("BEGIN IMMEDIATE")
                    replace_dependencies(db, item, [Dependency(upstream, Target.REPORT)])
                    db.execute("COMMIT")
            except Conflict:
                return False
            return True

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(edit, "consumer", "report")
            second = pool.submit(edit, "report", "consumer")
            self.assertCountEqual([first.result(), second.result()], [True, False])


if __name__ == "__main__":
    unittest.main()

from hashlib import sha256
from pathlib import Path
import runpy
from tempfile import TemporaryDirectory
import unittest

from phalanx import finalization, intake
from phalanx.intake_models import Cancel, Intake
from phalanx.store import connect, initialize, write_transaction

from test_bootstrap import OWNER, ServerFixture, claim_stage

conversion = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts" / "convert_store.py"))
convert, objects = conversion["convert"], conversion["objects"]


class StoreConversionTest(ServerFixture, unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.source = self.root / "source.sqlite3"
        initialize(self.source)
        with write_transaction(self.source) as db:
            intake.register(db, "repo", "https://github.com/example/repo")
            self.item = intake.create(db, Intake(repository_id="repo", goal="Keep active work", target="report"))
            self.closed = intake.create(db, Intake(repository_id="repo", goal="Keep history", target="report"))
            intake.cancel(db, self.closed["id"], self.closed["execution"]["id"],
                          Cancel(expected_version=1, reason="Keep historical report"))
        finalization.attempt(self.source, self.closed["execution"]["id"])
        self.token = "w" * 32
        self.claim = claim_stage(self.source, self.item["id"], self.token)
        with write_transaction(self.source) as db:
            for name in ("runner_id", "lease_digest", "blocker"):
                db.execute(f"ALTER TABLE attempts ADD COLUMN {name} TEXT NOT NULL DEFAULT 'retired'")
            db.execute("ALTER TABLE attempts ADD COLUMN lease_expires_at INTEGER NOT NULL DEFAULT 42")
            db.execute("CREATE TABLE attempt_workers (attempt_id TEXT PRIMARY KEY, reference TEXT NOT NULL)")
            db.execute("INSERT INTO attempt_workers VALUES (?, 'historical session')", (self.claim["attempt_id"],))
            db.execute("PRAGMA user_version=24")

    def test_conversion_matches_fresh_schema_preserves_records_and_resumes_active_writer(self):
        before = sha256(self.source.read_bytes()).digest()
        destination = self.root / "converted.sqlite3"
        result = convert(self.source, destination)
        self.assertTrue(result["sqlite_master_matches"])
        archive = result["source_archive"]
        self.assertEqual(archive["sha256"], before.hex())
        self.assertEqual(archive["path"], str(self.source.resolve()))
        self.assertEqual(archive["tables"]["attempt_workers"]["rows"], 1)
        self.assertIn("lease_digest", archive["tables"]["attempts"]["columns"])
        self.assertEqual(sha256(self.source.read_bytes()).digest(), before)
        fresh = self.root / "fresh.sqlite3"
        initialize(fresh)
        with connect(fresh) as new, connect(destination) as converted, connect(self.source) as old:
            self.assertEqual(objects(new), objects(converted))
            for table in ("executions", "transition_history", "reports", "writers",
                          "operator_progress", "operator_change"):
                self.assertEqual(list(map(tuple, old.execute(f"SELECT * FROM {table}"))),
                                 list(map(tuple, converted.execute(f"SELECT * FROM {table}"))))
        initialize(destination)
        self.start_server(destination)
        status, closed = self.get(f"/v1/items/{self.closed['id']}", OWNER)
        self.assertEqual(status, 200, closed)
        self.assertEqual(closed["execution"]["state"]["status"], "cancelled")
        status, report = self.get(closed["execution"]["state"]["final_output"]["reference"], OWNER)
        self.assertEqual(status, 200, report)
        self.assertIn("Keep historical report", report)
        status, board = self.get("/v1/operator/executions?kind=closed", OWNER)
        self.assertEqual(status, 200, board)
        self.assertIn(self.closed["execution"]["id"], {row["id"] for row in board["items"]})
        status, current = self.get("/v1/writer", self.token)
        self.assertEqual(status, 200, current)
        self.assertEqual(current["writer"]["attempt_id"], self.claim["attempt_id"])
        execution = current["id"]
        status, advanced = self.request(
            f"/v1/items/{self.item['id']}/executions/{execution}/result", self.token, {
                "expected_version": current["version"], "attempt_id": self.claim["attempt_id"],
                "stage": "report_drafting", "outcome": "succeeded", "report_markdown": "Preserved work.",
            })
        self.assertEqual(status, 200, advanced)
        self.assertEqual(advanced["state"]["stage"], "report_review")

    def test_conversion_refuses_unknown_columns_and_never_replaces_files(self):
        destination = self.root / "converted.sqlite3"
        with write_transaction(self.source) as db:
            db.execute("ALTER TABLE attempts ADD COLUMN unreviewed TEXT")
        with self.assertRaisesRegex(ValueError, "Unexpected columns"):
            convert(self.source, destination)
        self.assertFalse(destination.exists())
        destination.write_bytes(b"keep")
        with self.assertRaises(FileExistsError):
            convert(self.source, destination)
        self.assertEqual(destination.read_bytes(), b"keep")

    def test_bootstrap_rejects_schema_24_without_changing_it(self):
        before = self.source.read_bytes()
        with self.assertRaisesRegex(ValueError, "offline conversion"):
            initialize(self.source)
        self.assertEqual(self.source.read_bytes(), before)

    def test_conversion_also_accepts_current_source_without_retired_metadata(self):
        with write_transaction(self.source) as db:
            db.execute("DROP TABLE attempt_workers")
            for name in ("runner_id", "lease_digest", "lease_expires_at", "blocker"):
                db.execute(f"ALTER TABLE attempts DROP COLUMN {name}")
        result = convert(self.source, self.root / "current.sqlite3")
        self.assertEqual(result["rows"]["attempts"], 1)
        self.assertTrue(result["sqlite_master_matches"])

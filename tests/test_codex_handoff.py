import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from uuid import UUID

from phalanx.codex_handoff import (
    MAX_RESULT_BYTES,
    CodexHandoff,
    InvalidResult,
    Outcome,
    WorkPacket,
)


class CodexHandoffTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name).resolve()
        packet = WorkPacket(
            item_id="item-11",
            attempt_id=UUID(int=11),
            state_version=4,
            stage="development",
            repository="example/project",
            worktree=root / "isolated worktree",
            branch="codex/docs",
            expected_revision="a" * 40,
            goal="Correct the README",
            output_type="pull_request",
            evidence_requirements="Record the candidate commit",
            context="Docs only",
            handoff="The owner approved the wording",
            base_branch="main",
        )
        spool = root / "results"
        spool.mkdir()
        self.handoff = CodexHandoff(packet, spool)
        self.result = packet.result_identity() | {
            "outcome": "succeeded",
            "details": {"evidence": "Candidate " + "b" * 40},
        }

    def write_result(self, **changes):
        self.handoff.result_path.write_text(
            json.dumps(self.result | changes), encoding="utf-8",
        )

    def test_captain_arguments_include_worktree_and_stable_marker(self):
        handoff = self.handoff
        arguments = handoff.create_thread_arguments("project-1")
        self.assertEqual(arguments["target"], {
            "type": "project", "projectId": "project-1", "environment": {"type": "local"},
        })
        self.assertIn(handoff.packet.marker, arguments["title"])
        prompt = arguments["prompt"]
        self.assertIn(handoff.packet.marker, prompt)
        for path in (handoff.result_path, handoff.temporary_path):
            self.assertIn(str(path), prompt)
        packet = json.JSONDecoder().raw_decode(prompt.split("Work packet:\n", 1)[1])[0]
        self.assertEqual(packet["result_identity"], handoff.packet.result_identity())
        self.assertEqual(packet["handoff"], "The owner approved the wording")
        self.assertIn("explicit working directory", prompt)
        self.assertIn("leave GitHub writes to the", prompt)

    def test_attempt_specific_final_file_only(self):
        handoff = self.handoff
        handoff.temporary_path.write_text(json.dumps(self.result), encoding="utf-8")
        other = replace(handoff, packet=replace(handoff.packet, attempt_id=UUID(int=12)))
        other.result_path.write_text(json.dumps(self.result), encoding="utf-8")
        self.assertIsNone(handoff.read_result())
        handoff.temporary_path.rename(handoff.result_path)
        result = handoff.read_result()
        self.assertEqual(result.packet, handoff.packet)
        self.assertEqual(result.outcome, Outcome.SUCCEEDED)
        self.assertEqual(result.details, self.result["details"])
        with self.assertRaisesRegex(InvalidResult, "attempt_id"):
            other.read_result()

    def test_rejects_inexact_identity(self):
        for field, bad_value in {
            "contract_version": True,
            "item_id": "other",
            "state_version": 4.0,
            "stage": "self_review",
            "repository": "other/repo",
            "worktree": "/different",
            "branch": "main",
            "expected_revision": "b" * 40,
        }.items():
            with self.subTest(field=field):
                self.write_result(**{field: bad_value})
                with self.assertRaisesRegex(InvalidResult, field):
                    self.handoff.read_result()

    def test_outcome_details_are_structured_and_inert(self):
        for outcome, details in {
            "changes_required": {"findings": "Update the link"},
            "needs_input": {"question": "Keep the link?", "options": "Yes or no", "handoff": "Draft ready"},
            "failed": {"reason": "No checkout", "attempted": "raise RuntimeError()", "retry_guidance": "Restore checkout"},
        }.items():
            with self.subTest(outcome=outcome):
                self.write_result(outcome=outcome, details=details)
                self.assertEqual(self.handoff.read_result().details, details)

    def test_rejects_malformed_or_oversized_files(self):
        for raw in (
            b"\xff", b"{", b"[]", b'{"outcome": "failed", "outcome": "succeeded"}',
            b"[" * 1500 + b"]" * 1500, b" " * (MAX_RESULT_BYTES + 1),
        ):
            with self.subTest(prefix=raw[:60]):
                self.handoff.result_path.write_bytes(raw)
                with self.assertRaises(InvalidResult):
                    self.handoff.read_result()
        for changes in (
            {"extra": "field"},
            {"outcome": "cancelled"},
            {"outcome": []},
            {"outcome": "needs_input"},
            {"details": {"evidence": ["not a string"]}},
            {"details": {"evidence": "  "}},
        ):
            with self.subTest(changes=changes):
                self.write_result(**changes)
                with self.assertRaises(InvalidResult):
                    self.handoff.read_result()

    def test_rejects_directory(self):
        self.handoff.result_path.mkdir()
        with self.assertRaises(InvalidResult):
            self.handoff.read_result()

    def test_missing_result_directory_is_an_error(self):
        self.handoff.result_directory.rmdir()
        with self.assertRaises(InvalidResult):
            self.handoff.read_result()

    def test_rejects_hard_link(self):
        self.handoff.temporary_path.touch()
        os.link(self.handoff.temporary_path, self.handoff.result_path)
        with self.assertRaises(InvalidResult):
            self.handoff.read_result()

    def test_rejects_symbolic_link(self):
        try:
            self.handoff.result_path.symlink_to(self.handoff.temporary_path)
        except OSError as exc:
            self.skipTest(f"Symlink creation unavailable: {exc}")
        with self.assertRaises(InvalidResult):
            self.handoff.read_result()

    def test_rejects_relative_paths_and_non_uuid_attempt(self):
        with self.assertRaises(ValueError):
            replace(self.handoff, result_directory=Path("relative"))
        with self.assertRaises(ValueError):
            replace(self.handoff, packet=replace(self.handoff.packet, attempt_id="../other"))

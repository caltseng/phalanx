from dataclasses import replace
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from phalanx.attended import RunnerError
from phalanx.candidate_local import CandidateLocal, LocalCheck, _kill_tree
from phalanx.codex_handoff import Outcome, WorkerResult
from phalanx.validation import PreCheck, PreOutcome
from phalanx.workspaces import WorkspaceError, WorkspaceManager


class CandidateLocalTest(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve()
        checkout = root / "checkout"
        checkout.mkdir()
        self.git(checkout, "init", "-b", "main")
        self.git(checkout, "remote", "add", "origin", str(checkout))
        (checkout / "marker").write_text("candidate")
        self.git(checkout, "add", "marker")
        self.git(checkout, "commit", "-m", "Base")
        self.manager = WorkspaceManager(checkout, str(checkout), root / "worktrees")
        self.workspace = self.manager.prepare("item", base="refs/heads/main")
        self.api = Mock()

    @staticmethod
    def git(path, *args):
        subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.test",
                        *args], cwd=path, check=True, capture_output=True)

    def operation(self, stage):
        execution = {"id": "execution", "version": 3, "state": {
            "stage": stage, "candidate": self.workspace.head, "status": "running",
        }}
        claim = {"item_id": "item", "attempt_id": str(uuid4()), "execution": execution,
                 "request": {"repository_id": "repo", "goal": "Candidate fixture"}}
        self.api.call.return_value = {"execution": execution}
        return CandidateLocal(self.api, self.manager, claim, runner_id="desk", lease="secret")

    @staticmethod
    def command(name, script):
        return LocalCheck(PreCheck(name), (sys.executable, "-I", "-c", script))

    def test_self_review_packet_and_result_match_claim_and_clean_head(self):
        operation = self.operation("self_review")
        packet = operation.self_review_packet(base_branch="main")
        result = WorkerResult(packet, Outcome.SUCCEEDED, {"evidence": "Reviewed the candidate diff"})
        body = operation.self_review(result)
        self.assertEqual(body["reviewed_candidate"], self.workspace.head)
        self.assertEqual(body["review_notes"], "Reviewed the candidate diff")
        operation.submit(body)
        self.api.call.assert_called_with("/items/item/executions/execution/result", body,
                                         lease="secret", key=f"candidate-local-{packet.attempt_id}")
        question = WorkerResult(packet, Outcome.NEEDS_INPUT, {
            "question": "Which scope should be reviewed?", "options": "Current or expanded",
            "handoff": "Reviewed the diff against main",
        })
        waiting = operation.self_review(question)
        self.assertEqual(waiting["question"], {"prompt": question.details["question"],
                                               "options": [question.details["options"]],
                                               "handoff": question.details["handoff"]})
        stale = replace(result, packet=replace(packet, expected_revision="b" * 40))
        with self.assertRaisesRegex(RunnerError, "does not match"):
            operation.self_review(stale)
        (self.workspace.path / "marker").write_text("dirty")
        with self.assertRaisesRegex(WorkspaceError, "clean"):
            operation.submit(body)

    def test_checks_run_at_candidate_and_record_authorized_bypass(self):
        operation = self.operation("pre_pr_validation")
        check = self.command("local", "from pathlib import Path; "
                             "assert Path('marker').read_text() == 'candidate'; "
                             f"assert Path.cwd().resolve() == Path({str(self.workspace.path)!r})")
        bypass = LocalCheck(PreCheck("platform", allow_inapplicable=True),
                            disposition=PreOutcome.INAPPLICABLE, reason="Different target platform",
                            authorization="Repository policy section 2")
        body = operation.pre_validate((check, bypass))
        self.assertEqual(body["outcome"], "succeeded")
        self.assertEqual(body["prevalidated_candidate"], self.workspace.head)
        self.assertEqual([check["outcome"] for check in body["local_checks"]],
                         ["passed", "inapplicable"])
        self.assertEqual(body["local_checks"][1]["authorization"], bypass.authorization)
        denied = replace(bypass, policy=PreCheck("platform"))
        waiting = operation.pre_validate((denied,))
        self.assertEqual(waiting["outcome"], "needs_input")
        self.assertIn("policy-authorized disposition", waiting["question"]["prompt"])
        self.assertTrue(waiting["question"]["handoff"])
        self.assertEqual(operation.pre_validate(None)["outcome"], "needs_input")
        self.assertEqual(operation.pre_validate(())["outcome"], "succeeded")

    def test_failed_check_and_worktree_change_cannot_advance(self):
        operation = self.operation("pre_pr_validation")
        failed = operation.pre_validate((self.command("fails", "raise SystemExit(7)"),))
        self.assertEqual(failed["outcome"], "changes_required")
        self.assertEqual(failed["local_checks"][0]["reason"], "Exited with status 7")
        writes = self.command("mutates", "from pathlib import Path; Path('unexpected').touch()")
        body = operation.pre_validate((writes,))
        self.assertEqual(body["outcome"], "needs_input")
        self.assertEqual(body["local_checks"][0]["outcome"], "failed")
        self.assertNotIn("prevalidated_candidate", body)
        self.assertIn("clean", body["question"]["prompt"])
        self.assertTrue(body["question"]["handoff"])

    def test_ignored_build_output_does_not_block_checks(self):
        exclude = Path(subprocess.run(
            ["git", "rev-parse", "--git-path", "info/exclude"], cwd=self.workspace.path,
            check=True, capture_output=True, text=True,
        ).stdout.strip())
        (self.workspace.path / exclude).write_text("build/\n")
        operation = self.operation("pre_pr_validation")
        builds = self.command("builds", "from pathlib import Path; "
                              "Path('build').mkdir(); Path('build/out').touch()")
        self.assertEqual(operation.pre_validate((builds,))["outcome"], "succeeded")

    def test_timed_out_check_fails_and_stops_its_process_tree(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        started, survived = Path(temp.name) / "started", Path(temp.name) / "survived"
        grandchild = (f"import pathlib, time; time.sleep(3); "
                      f"pathlib.Path({str(survived)!r}).touch()")
        check = replace(self.command("hangs", (
            "import pathlib, subprocess, sys, time; "
            f"subprocess.Popen([sys.executable, '-I', '-c', {grandchild!r}]); "
            f"pathlib.Path({str(started)!r}).touch(); time.sleep(60)"
        )), timeout_seconds=2)
        body = self.operation("pre_pr_validation").pre_validate((check,))
        self.assertEqual(body["outcome"], "changes_required")
        self.assertEqual(body["local_checks"][0]["reason"], "Timed out after 2 seconds")
        self.assertTrue(started.exists())
        time.sleep(4)
        self.assertFalse(survived.exists())

    @unittest.skipIf(sys.platform == "win32", "POSIX process groups")
    def test_process_group_kill_tolerates_permission_error(self):
        process = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(60)"],
                                   start_new_session=True)
        with patch("phalanx.candidate_local.os.killpg", side_effect=PermissionError):
            _kill_tree(process)
        self.assertIsNotNone(process.returncode)

    def test_stopping_claim_blocks_commands(self):
        operation = self.operation("pre_pr_validation")
        self.api.call.return_value = {"execution": {
            **operation.execution, "state": {**operation.execution["state"], "stopping": "cancelled"},
        }}
        with self.assertRaisesRegex(RunnerError, "claim changed"):
            operation.pre_validate((self.command("forbidden", "from pathlib import Path; "
                                                 "Path('unexpected').touch()"),))
        self.assertFalse((self.workspace.path / "unexpected").exists())

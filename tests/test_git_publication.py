from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from phalanx.git_publication import GitPublication, PublicationError
from phalanx.workspaces import WorkspaceManager


def git(path, *args):
    return subprocess.run(
        ["git", *args], cwd=path, check=True, capture_output=True, text=True,
    ).stdout.strip()


def bare_git(repository, *args):
    # Address bare repositories explicitly so hosts with safe.bareRepository=explicit work.
    return git(repository.parent, f"--git-dir={repository}", *args)


class GitPublicationTests(unittest.TestCase):
    def setUp(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.remote = directory / "remote.git"
        self.checkout = directory / "checkout"
        git(directory, "init", "--bare", str(self.remote))
        git(directory, "init", str(self.checkout))
        git(self.checkout, "config", "user.name", "Phalanx")
        git(self.checkout, "config", "user.email", "phalanx@example.invalid")
        git(self.checkout, "remote", "add", "origin", str(self.remote))
        self.old = self.commit("old")
        self.candidate = self.commit("candidate")
        workspace = WorkspaceManager(
            self.checkout, str(self.remote), directory / "workspaces",
        ).prepare("item-a", base=self.old)
        self.publisher = GitPublication(workspace.path, str(self.remote), "item-a")
        self.ref = f"refs/heads/{workspace.branch}"
        self.assertEqual(self.publisher.ref, self.ref)

    def commit(self, message):
        git(self.checkout, "commit", "--allow-empty", "-m", message)
        return git(self.checkout, "rev-parse", "HEAD")

    def push(self, oid, ref=None):
        git(self.checkout, "push", "origin", f"{oid}:{ref or self.ref}")

    def test_creates_missing_ref_then_fast_forwards_only_the_item_ref(self):
        self.push(self.old, self.ref + "-neighbor")
        self.assertIsNone(self.publisher.read_head())
        git(self.checkout, "tag", "-a", "-m", "tag", "test-tag", self.old)
        git(self.checkout, "config", "push.followTags", "true")
        git(self.checkout, "config", "remote.origin.mirror", "true")
        self.assertEqual(self.publisher.publish(self.old, expected=None).status, "success")
        git(self.checkout, "branch", self.candidate, self.old)
        self.assertEqual(self.publisher.publish(self.candidate, expected=self.old).status, "success")
        self.assertEqual(self.publisher.read_head(), self.candidate)
        self.assertEqual(bare_git(self.remote, "for-each-ref", "--format=%(refname)").splitlines(),
                         [self.ref, self.ref + "-neighbor"])

    def test_expected_absence_and_deleted_ref_are_distinct(self):
        self.push(self.old)
        self.assertEqual(self.publisher.publish(self.candidate, expected=None).status, "conflict")
        bare_git(self.remote, "update-ref", "-d", self.ref)
        self.assertEqual(
            self.publisher.publish(self.candidate, expected=self.old).status, "conflict",
        )
        self.assertIsNone(self.publisher.read_head())

    def test_unrelated_history_is_rejected(self):
        self.push(self.old)
        tree = git(self.checkout, "rev-parse", f"{self.old}^{{tree}}")
        unrelated = git(self.checkout, "commit-tree", tree, "-m", "unrelated")
        self.assertEqual(self.publisher.publish(unrelated, expected=self.old).status, "conflict")
        self.assertEqual(self.publisher.read_head(), self.old)

    def test_competing_creation_or_advance_after_preflight_preserves_other_push(self):
        rival = self.commit("rival")
        for expected in (None, self.old):
            with self.subTest(expected=expected):
                if expected:
                    self.push(expected)
                run_git = self.publisher._git

                def competing_push(*args):
                    if "push" in args and "--porcelain" in args:
                        self.push(rival)
                    return run_git(*args)

                try:
                    with patch.object(self.publisher, "_git", side_effect=competing_push):
                        result = self.publisher.publish(self.candidate, expected=expected)
                    self.assertEqual(result.status, "conflict")
                    self.assertEqual(self.publisher.read_head(), rival)
                finally:
                    bare_git(self.remote, "update-ref", "-d", self.ref)

    def test_lost_push_response_is_uncertain_and_can_be_read_back(self):
        run_git = self.publisher._git

        def lose_response(*args):
            result = run_git(*args)
            if "push" in args and "--porcelain" in args:
                self.assertEqual(result.returncode, 0)
                raise PublicationError("lost response")
            return result

        with patch.object(self.publisher, "_git", side_effect=lose_response) as calls:
            result = self.publisher.publish(self.candidate, expected=None)
        self.assertEqual(result.status, "uncertain")
        self.assertEqual(sum("--porcelain" in call.args for call in calls.call_args_list), 1)
        self.assertEqual(self.publisher.read_head(), self.candidate)

    def test_acknowledged_push_with_failed_readback_remains_uncertain(self):
        with patch.object(
            self.publisher, "read_head",
            side_effect=[None, PublicationError("read-back unavailable")],
        ):
            result = self.publisher.publish(self.candidate, expected=None)
        self.assertEqual(result.status, "uncertain")
        self.assertEqual(self.publisher.read_head(), self.candidate)

    def test_verifies_development_checkpoint_on_the_item_worktree(self):
        self.publisher.verify_candidate(self.old, base="main", base_sha=self.old)
        for candidate, base, base_sha in (
            (self.candidate, "main", self.old),
            (self.old, "main", self.candidate),
            (self.old, "invalid base", self.old),
            (self.old, self.ref.removeprefix("refs/heads/"), self.old),
        ):
            with self.subTest(candidate=candidate, base=base, base_sha=base_sha):
                with self.assertRaises(PublicationError):
                    self.publisher.verify_candidate(candidate, base=base, base_sha=base_sha)

    def test_claim_is_checked_after_preflight_and_before_push(self):
        class Expired(Exception):
            pass

        calls = 0

        def authorize():
            nonlocal calls
            calls += 1
            if calls == 2:
                raise Expired

        self.push(self.old, "refs/heads/main")
        self.assertEqual(self.publisher.read_branch("main"), self.old)
        self.assertIsNone(self.publisher.read_branch("missing"))
        with self.assertRaises(Expired):
            self.publisher.publish(self.candidate, expected=None, authorize=authorize)
        self.assertEqual(calls, 2)
        self.assertIsNone(self.publisher.read_head())

    def test_wrong_push_destination_is_rejected(self):
        other = self.remote.with_name("other.git")
        git(other.parent, "init", "--bare", str(other))
        git(self.checkout, "remote", "set-url", "--push", "origin", str(other))
        with self.assertRaisesRegex(PublicationError, "registered repository"):
            self.publisher.publish(self.candidate, expected=None)
        self.assertEqual(bare_git(other, "for-each-ref"), "")
        self.assertEqual(bare_git(self.remote, "for-each-ref"), "")

    def test_candidate_requires_full_commit_id(self):
        blob = git(self.checkout, "hash-object", "-w", ".git/config")
        for candidate in ("HEAD", self.candidate[:8], blob):
            with self.subTest(candidate=candidate), self.assertRaises(PublicationError):
                self.publisher.publish(candidate, expected=None)
        self.assertIsNone(self.publisher.read_head())


if __name__ == "__main__":
    unittest.main()

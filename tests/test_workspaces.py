import concurrent.futures
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from phalanx import workspaces
from phalanx.workspaces import WorkspaceError, WorkspaceManager

from test_bootstrap import git


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="phalanx test ")
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name).resolve()
        self.checkout = self.directory / "checkout"
        self.root = self.directory / "workspaces"
        self.checkout.mkdir()
        git(self.checkout, "init", "--initial-branch=main")
        git(self.checkout, "config", "user.name", "Phalanx tests")
        git(self.checkout, "config", "user.email", "tests@example.invalid")
        git(self.checkout, "config", "core.hooksPath", str(self.directory / "no-hooks"))
        git(self.checkout, "config", "commit.gpgSign", "false")
        git(self.checkout, "remote", "add", "origin", "https://github.com/Owner/Repo.git")
        (self.checkout / "source").write_text("original\n")
        (self.checkout / ".gitignore").write_text("ignored/\n")
        git(self.checkout, "add", ".")
        git(self.checkout, "commit", "-m", "base")
        self.base = git(self.checkout, "rev-parse", "HEAD")
        self.manager = WorkspaceManager(self.checkout, "git@github.com:owner/repo", self.root)

    def prepare(self, item="item"):
        return self.manager.prepare(item, base="refs/heads/main")

    def test_isolation_and_dirty_recovery_at_current_head(self):
        first = self.prepare("../item:α")
        second = self.prepare("item:α")
        self.assertEqual(first.head, self.base)
        self.assertFalse(first.dirty)
        self.assertEqual(first.path.parent, self.root)
        self.assertNotEqual(first.path, second.path)
        self.assertNotEqual(first.branch, second.branch)
        self.assertRegex(first.branch, r"^phalanx/[0-9a-f]{64}$")

        (first.path / "source").write_text("committed\n")
        git(first.path, "commit", "-am", "worker checkpoint")
        head = git(first.path, "rev-parse", "HEAD")
        (first.path / "source").write_text("staged\n")
        git(first.path, "add", "source")
        (first.path / "source").write_text("unstaged\n")
        (first.path / "untracked").write_text("untracked\n")
        status = git(first.path, "status", "--porcelain")
        new_manager = WorkspaceManager(self.checkout, "https://github.com/owner/repo", self.root)
        recovered = new_manager.prepare("../item:α", base="missing-base")
        self.assertEqual((recovered.path, recovered.branch, recovered.head),
                         (first.path, first.branch, head))
        self.assertTrue(recovered.dirty)
        self.assertEqual(git(first.path, "status", "--porcelain"), status)
        self.assertEqual((first.path / "source").read_text(), "unstaged\n")
        self.assertEqual((first.path / "untracked").read_text(), "untracked\n")
        self.assertEqual((second.path / "source").read_text(), "original\n")
        self.assertEqual(git(self.checkout, "status", "--porcelain"), "")

    def test_rejects_mismatched_remote_and_nested_checkout(self):
        git(self.checkout, "remote", "set-url", "origin", "https://github.com/owner/other")
        with self.assertRaisesRegex(WorkspaceError, "does not match"):
            self.prepare()
        nested = self.checkout / "nested"
        nested.mkdir()
        with self.assertRaisesRegex(WorkspaceError, "worktree root"):
            WorkspaceManager(nested, "https://github.com/owner/other", self.root).prepare(
                "item", base="HEAD",
            )
        self.assertFalse(self.root.exists())

    def test_origin_preparation_verifies_checkout_once_and_preserves_existing_work(self):
        remote = self.directory / "origin.git"
        git(self.directory, "init", "--bare", str(remote))
        git(self.checkout, "remote", "set-url", "origin", remote.as_uri())
        git(self.checkout, "push", "origin", "main")
        git(self.checkout, "remote", "set-url", "origin", "https://github.com/owner/repo.git")
        git(self.checkout, "config", f"url.{remote.as_uri()}.insteadOf", "https://github.com/owner/repo.git")
        manager = WorkspaceManager(self.checkout, "git@github.com:owner/repo", self.root)
        for existing in (False, True):
            with self.subTest(existing=existing), patch("phalanx.workspaces._git", wraps=workspaces._git) as calls:
                workspace = manager.prepare_from_origin("item", branch="main")
            checkout_calls = [call.args[1:] for call in calls.call_args_list if call.args[0] == self.checkout]
            self.assertEqual(sum(args[:2] == ("rev-parse", "--show-toplevel")
                                 for args in checkout_calls), 1)
            self.assertEqual(sum(args[:3] == ("config", "--get-all", "remote.origin.url")
                                 for args in checkout_calls), 1)
            self.assertEqual(workspace.head, self.base)
            self.assertEqual(workspace.dirty, existing)
            (workspace.path / "source").write_text("uncommitted work\n")
            git(self.checkout, "commit", "--allow-empty", "-m", "origin advanced")
            git(self.checkout, "push", "origin", "main")
        self.assertEqual(git(self.checkout, "rev-parse", "origin/main"),
                         git(self.checkout, "rev-parse", "main"))
        self.assertEqual((workspace.path / "source").read_text(), "uncommitted work\n")

    def test_origin_preparation_reverifies_each_operation_before_fetch(self):
        self.prepare()
        git(self.checkout, "remote", "set-url", "origin", "https://github.com/owner/other")
        with patch("phalanx.workspaces._git", wraps=workspaces._git) as calls, \
                self.assertRaisesRegex(WorkspaceError, "does not match"):
            self.manager.prepare_from_origin("item", branch="main")
        self.assertFalse(any(call.args[1] == "fetch" for call in calls.call_args_list))

    def test_rejects_workspace_root_inside_checkout_or_git_metadata(self):
        linked = self.directory / "linked"
        git(self.checkout, "worktree", "add", "--detach", str(linked))
        for checkout, root, message in (
            (self.checkout, self.checkout / "workspaces", "mapped checkout"),
            (linked, self.checkout / ".git" / "workspaces", "Git metadata"),
        ):
            manager = WorkspaceManager(checkout, "git@github.com:owner/repo", root)
            with self.assertRaisesRegex(WorkspaceError, message):
                manager.verify_checkout()

    def test_snapshot_rejects_each_identity_mismatch(self):
        workspace = self.prepare()
        common = self.checkout / ".git"
        snapshot = self.manager._snapshot
        self.assertEqual(self.manager.inspect("item"), workspace)
        for path, branch, repo in ((self.checkout, workspace.branch, common),
                                   (workspace.path, "phalanx/other", common),
                                   (workspace.path, workspace.branch, self.directory)):
            with self.assertRaisesRegex(WorkspaceError, "identity changed"):
                snapshot(path, branch, repo)

        # A copied .git pointer reaches the real metadata but not its backlink.
        copy = self.directory / "copy"
        copy.mkdir()
        (copy / ".git").write_bytes((workspace.path / ".git").read_bytes())
        with self.assertRaisesRegex(WorkspaceError, "different checkout"):
            snapshot(copy, workspace.branch, common)

        # Metadata outside the repo's worktrees directory is refused.
        admin = self.directory / "admin"
        admin.mkdir()
        (admin / "commondir").write_text(f"{common.as_posix()}\n")
        (admin / "HEAD").write_text(f"ref: refs/heads/{workspace.branch}\n")
        (admin / "gitdir").write_text(f"{(copy / '.git').as_posix()}\n")
        (copy / ".git").write_text(f"gitdir: {admin.as_posix()}\n")
        with self.assertRaisesRegex(WorkspaceError, "outside the repo's worktrees"):
            snapshot(copy, workspace.branch, common)

        # A branch without a commit cannot produce a candidate head.
        git(self.checkout, "update-ref", "-d", f"refs/heads/{workspace.branch}")
        with self.assertRaisesRegex(WorkspaceError, "rev-parse failed"):
            snapshot(workspace.path, workspace.branch, common)

    def test_rejects_unowned_registration_and_occupied_path(self):
        workspace = self.prepare()
        refs = git(self.checkout, "for-each-ref", "--format=%(refname)",
                   "refs/phalanx/workspaces")
        git(self.checkout, "update-ref", "-d", refs)
        with self.assertRaisesRegex(WorkspaceError, "ownership"):
            self.prepare()
        git(self.checkout, "worktree", "remove", str(workspace.path))
        workspace.path.mkdir()
        (workspace.path / "keep").touch()
        with self.assertRaisesRegex(WorkspaceError, "occupied"):
            self.prepare()
        self.assertTrue((workspace.path / "keep").exists())

    def test_rejects_branch_changed_or_registered_elsewhere(self):
        workspace = self.prepare()
        git(workspace.path, "switch", "--detach")
        with self.assertRaisesRegex(WorkspaceError, "registration conflicts"):
            self.prepare()
        git(self.checkout, "worktree", "remove", str(workspace.path))
        elsewhere = self.directory / "elsewhere"
        git(self.checkout, "worktree", "add", str(elsewhere), workspace.branch)
        with self.assertRaisesRegex(WorkspaceError, "registration conflicts"):
            self.prepare()

    def test_reattaches_reserved_branch_without_resetting_tip(self):
        workspace = self.prepare()
        git(workspace.path, "commit", "--allow-empty", "-m", "checkpoint")
        head = git(workspace.path, "rev-parse", "HEAD")
        git(self.checkout, "worktree", "remove", str(workspace.path))
        self.assertEqual(self.manager.prepare("item", base="missing-base").head, head)

    def test_setup_bypasses_checkout_and_reference_hooks_and_git_env_overrides(self):
        hooks = self.directory / "hooks"
        hooks.mkdir()
        git(self.checkout, "config", "core.hooksPath", hooks.as_posix())
        for name in ("post-checkout", "reference-transaction"):
            hook = hooks / name
            hook.write_bytes(b"#!/bin/sh\nprintf 'hook ran\\n' >> hook-ran\n")
            hook.chmod(0o755)
        git(self.checkout, "branch", "control")
        self.assertTrue((self.checkout / "hook-ran").is_file())
        (self.checkout / "hook-ran").unlink()
        with patch.dict(os.environ, {"GIT_DIR": str(self.directory / "bad")}):
            workspace = self.prepare()
        self.assertFalse((self.checkout / "hook-ran").exists())
        self.assertFalse((workspace.path / "hook-ran").exists())
        self.assertFalse(workspace.dirty)

    def test_concurrent_processes_reuse_one_registration_per_item(self):
        script = (
            "import sys; from pathlib import Path; "
            "from phalanx.workspaces import WorkspaceManager; "
            "checkout, root, item = sys.argv[1:]; "
            "manager = WorkspaceManager(Path(checkout), 'git@github.com:owner/repo', Path(root)); "
            "print(manager.prepare(item, base='refs/heads/main').path.name)"
        )

        def prepare(item):
            result = subprocess.run(
                [sys.executable, "-c", script, str(self.checkout), str(self.root), item],
                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout.strip()

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            first, second, *rest = pool.map(prepare, ["a", "b", "a", "b", "a", "b"])
        self.assertNotEqual(first, second)
        self.assertEqual(rest, [first, second, first, second])
        self.assertEqual({entry.name for entry in self.root.iterdir()}, {first, second})

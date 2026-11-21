import concurrent.futures
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from phalanx.workspaces import WorkspaceError, WorkspaceManager


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="phalanx test ")
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name).resolve()
        self.checkout = self.directory / "checkout"
        self.root = self.directory / "workspaces"
        self.checkout.mkdir()
        self.git(self.checkout, "init", "--initial-branch=main")
        self.git(self.checkout, "config", "user.name", "Phalanx tests")
        self.git(self.checkout, "config", "user.email", "tests@example.invalid")
        self.git(self.checkout, "config", "core.hooksPath", str(self.directory / "no-hooks"))
        self.git(self.checkout, "config", "commit.gpgSign", "false")
        self.git(self.checkout, "remote", "add", "origin", "https://github.com/Owner/Repo.git")
        (self.checkout / "source").write_text("original\n")
        (self.checkout / ".gitignore").write_text("ignored/\n")
        self.git(self.checkout, "add", ".")
        self.git(self.checkout, "commit", "-m", "base")
        self.base = self.git(self.checkout, "rev-parse", "HEAD")
        self.manager = WorkspaceManager(self.checkout, "git@github.com:owner/repo", self.root)

    def git(self, path, *args):
        result = subprocess.run(
            ["git", *args], cwd=path, capture_output=True, text=True, encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

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

        (first.path / "source").write_text("committed\n")
        self.git(first.path, "commit", "-am", "worker checkpoint")
        head = self.git(first.path, "rev-parse", "HEAD")
        (first.path / "source").write_text("staged\n")
        self.git(first.path, "add", "source")
        (first.path / "source").write_text("unstaged\n")
        (first.path / "untracked").write_text("untracked\n")
        status = self.git(first.path, "status", "--porcelain")
        new_manager = WorkspaceManager(self.checkout, "https://github.com/owner/repo", self.root)
        recovered = new_manager.prepare("../item:α", base="missing-base")
        self.assertEqual((recovered.path, recovered.branch, recovered.head),
                         (first.path, first.branch, head))
        self.assertTrue(recovered.dirty)
        self.assertEqual(self.git(first.path, "status", "--porcelain"), status)
        self.assertEqual((first.path / "source").read_text(), "unstaged\n")
        self.assertEqual((first.path / "untracked").read_text(), "untracked\n")
        self.assertEqual((second.path / "source").read_text(), "original\n")
        self.assertEqual(self.git(self.checkout, "status", "--porcelain"), "")

    def test_rejects_mismatched_remote_and_nested_checkout(self):
        self.git(self.checkout, "remote", "set-url", "origin", "https://github.com/owner/other")
        with self.assertRaisesRegex(WorkspaceError, "does not match"):
            self.prepare()
        nested = self.checkout / "nested"
        nested.mkdir()
        with self.assertRaisesRegex(WorkspaceError, "worktree root"):
            WorkspaceManager(nested, "https://github.com/owner/other", self.root).prepare(
                "item", base="HEAD",
            )
        self.assertFalse(self.root.exists())

    def test_rejects_unowned_registration_and_occupied_path(self):
        workspace = self.prepare()
        refs = self.git(self.checkout, "for-each-ref", "--format=%(refname)",
                        "refs/phalanx/workspaces")
        self.git(self.checkout, "update-ref", "-d", refs)
        with self.assertRaisesRegex(WorkspaceError, "ownership"):
            self.prepare()
        with self.assertRaisesRegex(WorkspaceError, "ownership"):
            self.manager.cleanup("item", closed=True, expected_head=workspace.head)
        self.git(self.checkout, "worktree", "remove", str(workspace.path))
        workspace.path.mkdir()
        (workspace.path / "keep").touch()
        with self.assertRaisesRegex(WorkspaceError, "occupied"):
            self.prepare()
        self.assertTrue((workspace.path / "keep").exists())

    def test_rejects_branch_changed_or_registered_elsewhere(self):
        workspace = self.prepare()
        self.git(workspace.path, "switch", "--detach")
        with self.assertRaisesRegex(WorkspaceError, "registration conflicts"):
            self.prepare()
        self.git(self.checkout, "worktree", "remove", str(workspace.path))
        elsewhere = self.directory / "elsewhere"
        self.git(self.checkout, "worktree", "add", str(elsewhere), workspace.branch)
        with self.assertRaisesRegex(WorkspaceError, "registration conflicts"):
            self.prepare()

    def test_reattaches_reserved_branch_without_resetting_tip(self):
        workspace = self.prepare()
        self.git(workspace.path, "commit", "--allow-empty", "-m", "checkpoint")
        head = self.git(workspace.path, "rev-parse", "HEAD")
        self.git(self.checkout, "worktree", "remove", str(workspace.path))
        self.assertEqual(self.manager.prepare("item", base="missing-base").head, head)

    def test_cleanup_requires_closed_clean_at_recorded_head(self):
        workspace = self.prepare()
        remove = self.manager.cleanup
        with self.assertRaisesRegex(WorkspaceError, "closed"):
            remove("item", closed=False, expected_head=workspace.head)
        with self.assertRaisesRegex(WorkspaceError, "recorded checkpoint"):
            remove("item", closed=True, expected_head="0" * 40)

        for relative in ("untracked", "ignored/local"):
            extra = workspace.path / relative
            extra.parent.mkdir(exist_ok=True)
            extra.write_text("local data")
            self.assertEqual(self.prepare().dirty, relative == "untracked")
            with self.assertRaisesRegex(WorkspaceError, "local changes"):
                remove("item", closed=True, expected_head=workspace.head)
            self.assertEqual(extra.read_text(), "local data")
            extra.unlink()
        (workspace.path / "ignored").rmdir()

        tracked = workspace.path / "source"
        self.git(workspace.path, "update-index", "--assume-unchanged", "source")
        tracked.write_text("hidden data")
        with self.assertRaisesRegex(WorkspaceError, "index entries"):
            remove("item", closed=True, expected_head=workspace.head)
        self.assertEqual(tracked.read_text(), "hidden data")
        self.git(workspace.path, "update-index", "--no-assume-unchanged", "source")
        self.git(workspace.path, "restore", "source")

        head_path = Path(self.git(workspace.path, "rev-parse", "--path-format=absolute",
                                  "--git-path", "MERGE_HEAD"))
        head_path.write_text(workspace.head + "\n")
        with self.assertRaisesRegex(WorkspaceError, "in-progress"):
            remove("item", closed=True, expected_head=workspace.head)
        head_path.unlink()

        self.assertTrue(remove("item", closed=True, expected_head=workspace.head))
        self.assertFalse(workspace.path.exists())
        self.assertFalse(remove("item", closed=True, expected_head=workspace.head))
        self.assertEqual(self.git(self.checkout, "rev-parse", workspace.branch), workspace.head)

    def test_setup_bypasses_checkout_and_reference_hooks_and_git_env_overrides(self):
        hooks = self.directory / "hooks"
        hooks.mkdir()
        self.git(self.checkout, "config", "core.hooksPath", hooks.as_posix())
        for name in ("post-checkout", "reference-transaction"):
            hook = hooks / name
            hook.write_bytes(b"#!/bin/sh\nprintf 'hook ran\\n' >> hook-ran\n")
            hook.chmod(0o755)
        self.git(self.checkout, "branch", "control")
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

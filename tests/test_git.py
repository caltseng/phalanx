import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import Mock, patch

from phalanx.git import run_git
from phalanx.local_checks import CLEANUP_TIMEOUT_SECONDS, kill_tree
from phalanx.workspaces import WorkspaceError, WorkspaceManager
from timing import WAIT_SECONDS


class GitExecutionTest(unittest.TestCase):
    def test_hung_checkout_probe_bounds_caller_and_stops_only_owned_tree(self):
        with TemporaryDirectory() as root:
            root = Path(root)
            ready, unrelated = root / "ready", root / "unrelated"
            child = ("import pathlib, sys; "
                     f"pathlib.Path({str(ready)!r}).touch(); sys.stdin.buffer.read()")
            script = ("import subprocess, sys; "
                      f"child = subprocess.Popen([sys.executable, '-c', {child!r}], stdin=subprocess.PIPE); "
                      "child.wait()")
            original = subprocess.Popen
            processes = []
            launched = []

            def launch(argv, **kwargs):
                if argv[0] != "git":
                    return original(argv, **kwargs)
                process = original([sys.executable, "-c", script], **dict(kwargs, stdout=subprocess.PIPE))
                processes.append(process)
                deadline = time.monotonic() + WAIT_SECONDS
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(ready.exists(), "helper fixture did not start")
                launched.append(time.monotonic())
                return process

            sentinel = original([sys.executable, "-c",
                                 "import pathlib, sys; sys.stdin.buffer.read(1); "
                                 f"pathlib.Path({str(unrelated)!r}).touch()"],
                                stdin=subprocess.PIPE)
            try:
                manager = WorkspaceManager(root, "https://github.com/example/repo", root.parent / "work")
                with patch("phalanx.git.subprocess.Popen", side_effect=launch), \
                        patch("phalanx.git.GIT_TIMEOUT_SECONDS", 0.1):
                    with self.assertRaisesRegex(WorkspaceError, "Git rev-parse timed out"):
                        manager.verify_checkout()
                self.assertLess(time.monotonic() - launched[0], 2 * CLEANUP_TIMEOUT_SECONDS + 5)
                self.assertIsNotNone(processes[0].poll())
                # EOF proves no descendant retained the inherited pipe after cleanup.
                self.assertEqual(processes[0].communicate(timeout=WAIT_SECONDS)[0], b"")
                sentinel.communicate(b"x", timeout=WAIT_SECONDS)
                self.assertTrue(unrelated.exists())
            finally:
                for process in [*processes, sentinel]:
                    if process.poll() is None:
                        if process in processes:
                            kill_tree(process)
                        else:
                            process.kill()
                        process.wait(timeout=WAIT_SECONDS)
                    if process.stdout:
                        process.stdout.close()

    def test_files_preserve_bytes_stdin_and_git_environment_isolation(self):
        original = subprocess.Popen

        def launch(argv, **kwargs):
            script = ("import os, sys; assert 'GIT_DIR' not in os.environ; "
                      "sys.stdout.buffer.write(sys.stdin.buffer.read() + b'\\xff'); "
                      "sys.stderr.buffer.write(b'warning')")
            self.assertEqual(argv[:4], ["git", "--no-optional-locks", "-c", "core.fsmonitor=false"])
            return original([sys.executable, "-c", script], **kwargs)

        with patch.dict(os.environ, {"GIT_DIR": "untrusted"}), \
                patch("phalanx.git.subprocess.Popen", side_effect=launch):
            result = run_git(None, "hash-object", stdin="hello")
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"hello\xff", b"warning"))

    def test_start_wait_and_cleanup_errors_remain_caller_errors_even_unchecked(self):
        for failure in (FileNotFoundError("missing git"), OSError("transport failed")):
            with self.subTest(failure=failure), patch("phalanx.git.subprocess.Popen", side_effect=failure):
                with self.assertRaisesRegex(WorkspaceError, "Cannot run Git"):
                    run_git(None, "rev-parse", check=False, error=WorkspaceError)
        for failure in (OSError("wait failed"), subprocess.SubprocessError("transport failed"),
                        subprocess.TimeoutExpired("git", 0.1), KeyboardInterrupt()):
            process = Mock()
            process.wait.side_effect = failure
            expected = KeyboardInterrupt if isinstance(failure, KeyboardInterrupt) else WorkspaceError
            with self.subTest(failure=failure), patch("phalanx.git.subprocess.Popen", return_value=process), \
                    patch("phalanx.git.kill_tree") as cleanup, self.assertRaises(expected):
                run_git(None, "rev-parse", check=False, error=WorkspaceError)
            cleanup.assert_called_once_with(process)
        process = Mock()
        process.wait.side_effect = subprocess.TimeoutExpired("git", 0.1)
        with patch("phalanx.git.subprocess.Popen", return_value=process), \
                patch("phalanx.git.kill_tree", side_effect=OSError("denied")), \
                self.assertRaisesRegex(WorkspaceError, "cleanup failed.*denied"):
            run_git(None, "fetch", error=WorkspaceError)

    def test_unchecked_probe_accepts_only_absence_and_never_signal_termination(self):
        for code in (0, 1, 128, -15):
            process = Mock(returncode=code)
            with self.subTest(code=code), patch("phalanx.git.subprocess.Popen", return_value=process):
                if code in (0, 1):
                    self.assertEqual(run_git(None, "rev-parse", check=False,
                                             allowed_returncodes=(0, 1)).returncode, code)
                else:
                    with self.assertRaisesRegex(RuntimeError, f"exit {code}"):
                        run_git(None, "rev-parse", check=False, allowed_returncodes=(0, 1))

    def test_broken_ref_is_not_an_absent_ref_even_when_git_exits_one(self):
        with TemporaryDirectory() as root:
            run_git(root, "init", "-q")
            args = ("rev-parse", "--verify", "--quiet", "refs/heads/broken")
            self.assertEqual(run_git(root, *args, check=False, allowed_returncodes=(0, 1)).returncode, 1)
            (Path(root) / ".git" / "refs" / "heads" / "broken").write_text("not-a-ref\n")
            with self.assertRaisesRegex(RuntimeError, "ignoring broken ref"):
                run_git(root, *args, check=False, allowed_returncodes=(0, 1))

    @unittest.skipUnless(os.name == "nt", "Windows owned-tree cleanup")
    def test_stalled_tree_cleanup_is_bounded_and_still_reaps_direct_child(self):
        process = Mock(pid=1234)
        with patch("phalanx.local_checks.subprocess.run",
                   side_effect=subprocess.TimeoutExpired("taskkill", CLEANUP_TIMEOUT_SECONDS)) as run, \
                self.assertRaises(subprocess.TimeoutExpired):
            kill_tree(process)
        self.assertEqual(run.call_args.args[0], ["taskkill", "/T", "/F", "/PID", "1234"])
        self.assertEqual(run.call_args.kwargs["timeout"], CLEANUP_TIMEOUT_SECONDS)
        process.kill.assert_called_once()
        process.wait.assert_called_once_with(timeout=CLEANUP_TIMEOUT_SECONDS)

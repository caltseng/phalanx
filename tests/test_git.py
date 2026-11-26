import os
from pathlib import Path
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from phalanx.git import run_git
from phalanx.local_checks import CLEANUP_TIMEOUT_SECONDS, kill_tree
from phalanx.workspaces import WorkspaceError, WorkspaceManager
from timing import WAIT_SECONDS


class GitExecutionTest(unittest.TestCase):
    def test_hung_checkout_probe_bounds_caller_and_stops_only_owned_tree(self):
        with TemporaryDirectory() as root, socket.socket() as listener:
            root = Path(root)
            unrelated = root / "unrelated"
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            listener.settimeout(WAIT_SECONDS)
            child = ("import socket; "
                     f"connection = socket.create_connection({listener.getsockname()!r}); "
                     "connection.sendall(b'r'); connection.recv(1)")
            script = ("import subprocess, sys; "
                      f"child = subprocess.Popen([sys.executable, '-c', {child!r}]); "
                      "child.wait()")
            original = subprocess.Popen
            processes = []
            connections = []
            launched = []

            def launch(argv, **kwargs):
                if argv[0] != "git":
                    return original(argv, **kwargs)
                process = original([sys.executable, "-c", script], **dict(kwargs, stdout=subprocess.PIPE))
                processes.append(process)
                connection, _ = listener.accept()
                connections.append(connection)
                connection.settimeout(WAIT_SECONDS)
                self.assertEqual(connection.recv(1), b"r")
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
                self.assertEqual(processes[0].communicate(timeout=CLEANUP_TIMEOUT_SECONDS)[0], b"")
                sentinel.communicate(b"x", timeout=WAIT_SECONDS)
                self.assertTrue(unrelated.exists())
            finally:
                for connection in connections:
                    connection.close()
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
            return original([sys.executable, "-c", script], **kwargs)

        with patch.dict(os.environ, {"GIT_DIR": "untrusted"}), \
                patch("phalanx.git.subprocess.Popen", side_effect=launch):
            result = run_git(None, "hash-object", stdin="hello")
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"hello\xff", b"warning"))

    def test_launch_errors_remain_caller_errors_even_unchecked(self):
        for failure in (FileNotFoundError("missing git"), OSError("transport failed")):
            with self.subTest(failure=failure), patch("phalanx.git.subprocess.Popen", side_effect=failure):
                with self.assertRaisesRegex(WorkspaceError, "Cannot run Git"):
                    run_git(None, "rev-parse", check=False, error=WorkspaceError)

    def test_wait_errors_and_cancellation_reap_owned_process(self):
        original = subprocess.Popen
        for failure in (OSError("wait failed"), subprocess.SubprocessError("transport failed"),
                        KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__):
                processes = []

                def launch(argv, **kwargs):
                    process = original([sys.executable, "-c", "import time; time.sleep(120)"], **kwargs)
                    processes.append(process)
                    wait = process.wait

                    def interrupted_wait(*args, **options):
                        process.wait = wait
                        raise failure

                    process.wait = interrupted_wait
                    return process

                try:
                    expected = KeyboardInterrupt if isinstance(failure, KeyboardInterrupt) else WorkspaceError
                    with patch("phalanx.git.subprocess.Popen", side_effect=launch), \
                            self.assertRaises(expected):
                        run_git(None, "rev-parse", check=False, error=WorkspaceError)
                    self.assertIsNotNone(processes[0].poll())
                finally:
                    for process in processes:
                        if process.poll() is None:
                            kill_tree(process)

    def test_broken_ref_is_not_an_absent_ref_even_when_git_exits_one(self):
        with TemporaryDirectory() as root:
            run_git(root, "init", "-q")
            args = ("rev-parse", "--verify", "--quiet", "refs/heads/broken")
            self.assertEqual(run_git(root, *args, check=False, allowed_returncodes=(0, 1)).returncode, 1)
            (Path(root) / ".git" / "refs" / "heads" / "broken").write_text("not-a-ref\n")
            with self.assertRaisesRegex(RuntimeError, "ignoring broken ref"):
                run_git(root, *args, check=False, allowed_returncodes=(0, 1))

    @unittest.skipIf(os.name == "nt", "POSIX signal return codes")
    def test_signal_termination_is_not_an_absent_ref(self):
        original = subprocess.Popen

        def launch(argv, **kwargs):
            return original([sys.executable, "-c",
                             "import os, signal; os.kill(os.getpid(), signal.SIGTERM)"], **kwargs)

        with patch("phalanx.git.subprocess.Popen", side_effect=launch), \
                self.assertRaisesRegex(WorkspaceError, "exit -15"):
            run_git(None, "rev-parse", check=False, allowed_returncodes=(0, 1), error=WorkspaceError)

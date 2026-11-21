"""Bridge transport regressions runnable without the API dependency stack."""

import json
import os
from pathlib import Path
import signal
import sys
from tempfile import TemporaryDirectory
import time
import unittest

from phalanx.captain_adapter import CommandTransport, MAX_ADAPTER_REPLY_BYTES
from phalanx.runner_errors import RunnerError


class CommandTransportTests(unittest.TestCase):
    def probe(self, directory, *, parent_wait):
        identities = Path(directory) / "owned-probe-processes.json"
        code = (
            "import json, os, subprocess, sys, time; "
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(4)']); "
            f"open({str(identities)!r}, 'w').write(json.dumps([os.getpid(), child.pid])); "
            "print('{\"accepted\": true}', flush=True); "
            f"time.sleep({parent_wait})"
        )
        return identities, [sys.executable, "-c", code]

    def cleanup_probe(self, identities):
        if identities.exists():
            for pid in json.loads(identities.read_text()):
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    if os.name != "nt":
                        raise
                except OSError as error:
                    if os.name != "nt" or error.winerror != 87:
                        raise

    def test_timeout_does_not_drain_descendant_inherited_stdio(self):
        with TemporaryDirectory() as directory:
            identities, command = self.probe(directory, parent_wait=10)
            started = time.monotonic()
            try:
                with self.assertRaisesRegex(RunnerError, "unavailable"):
                    CommandTransport(command, timeout=0.3)("launch", {})
                self.assertLess(time.monotonic() - started, 1.2)
                self.assertTrue(identities.exists(), "Probe did not spawn its inheriting child")
            finally:
                self.cleanup_probe(identities)

    def test_success_does_not_wait_for_descendant_eof(self):
        with TemporaryDirectory() as directory:
            identities, command = self.probe(directory, parent_wait=0)
            started = time.monotonic()
            try:
                response = CommandTransport(command, timeout=0.5)("observe", {})
                self.assertEqual(response, {"accepted": True})
                self.assertLess(time.monotonic() - started, 1.2)
            finally:
                self.cleanup_probe(identities)

    def test_large_and_invalid_utf8_replies_fail_explicitly(self):
        for code, reason in (
            (f"import sys; sys.stdout.buffer.write(b'x' * {MAX_ADAPTER_REPLY_BYTES + 1})", "size limit"),
            ("import sys; sys.stdout.buffer.write(b'\\xff')", "invalid JSON"),
        ):
            with self.subTest(reason=reason), self.assertRaisesRegex(RunnerError, reason):
                CommandTransport([sys.executable, "-c", code])("observe", {})

    def test_file_stdin_preserves_utf8_request(self):
        command = [sys.executable, "-c", "import json, sys; print(json.dumps(json.load(sys.stdin)))"]
        request = {"marker": "fixture", "prompt": "Field report \u2713"}
        response = CommandTransport(command)("launch", request)
        self.assertEqual(response, {"contract_version": 1, "operation": "launch", "arguments": request})

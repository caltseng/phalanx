import ctypes
from contextlib import closing, redirect_stdout
import io
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from phalanx import priority, server_admin
from phalanx.store import APPLICATION_ID, SCHEMA_VERSION
from phalanx.workspaces import any_file_lock
from timing import WAIT_SECONDS


class ServerInitializationTest(unittest.TestCase):
    def test_new_store_is_initialized_without_http_and_existing_data_is_never_replaced(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary).resolve() / "db" / "new.sqlite3"
            with patch.dict(os.environ, {"PHALANX_DB": str(path)}), \
                    patch.object(server_admin, "serve") as serve:
                with redirect_stdout(io.StringIO()):
                    server_admin.initialize_database()
                serve.assert_not_called()
                with closing(sqlite3.connect(path)) as db:
                    self.assertEqual(db.execute("PRAGMA application_id").fetchone()[0], APPLICATION_ID)
                    self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
                    self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                before = path.read_bytes()
                with self.assertRaisesRegex(ValueError, "new database"):
                    server_admin.initialize_database()
                self.assertEqual(path.read_bytes(), before)
                path.write_bytes(b"not an empty database")
                with self.assertRaisesRegex(ValueError, "new database"):
                    server_admin.initialize_database()
                self.assertEqual(path.read_bytes(), b"not an empty database")

    def test_initialization_requires_explicit_path_and_sole_ownership(self):
        for selected in ("", "relative.sqlite3"):
            with self.subTest(path=selected), patch.dict(os.environ, {"PHALANX_DB": selected}):
                with self.assertRaisesRegex(ValueError, "explicit absolute"):
                    server_admin.initialize_database()
        with TemporaryDirectory() as temporary:
            path = Path(temporary).resolve() / "new.sqlite3"
            with patch.dict(os.environ, {"PHALANX_DB": str(path)}), \
                    any_file_lock([path.with_name(path.name + ".service.lock")]):
                with self.assertRaisesRegex(ValueError, "sole database ownership"):
                    server_admin.initialize_database()
                self.assertFalse(path.exists())


class ServerPriorityTest(unittest.TestCase):
    def test_non_windows_does_not_load_windows_api(self):
        with patch.object(priority.os, "name", "posix"), \
                patch.object(ctypes, "WinDLL", create=True) as load:
            priority.foreground_priority()
        load.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows process scheduling")
    def test_background_child_becomes_normal_without_changing_parent(self):
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.GetPriorityClass.argtypes = [wintypes.HANDLE]
        kernel.GetPriorityClass.restype = wintypes.DWORD
        original = kernel.GetPriorityClass(kernel.GetCurrentProcess())
        script = """
import ctypes
from ctypes import wintypes
from phalanx.priority import foreground_priority
kernel = ctypes.WinDLL("kernel32", use_last_error=True)
kernel.GetCurrentProcess.restype = wintypes.HANDLE
kernel.GetPriorityClass.argtypes = [wintypes.HANDLE]
kernel.GetPriorityClass.restype = wintypes.DWORD
assert kernel.GetPriorityClass(kernel.GetCurrentProcess()) == 0x4000
foreground_priority()
assert kernel.GetPriorityClass(kernel.GetCurrentProcess()) == 0x20
"""
        result = subprocess.run(
            [sys.executable, "-c", script], creationflags=subprocess.BELOW_NORMAL_PRIORITY_CLASS,
            capture_output=True, text=True, timeout=WAIT_SECONDS)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(kernel.GetPriorityClass(kernel.GetCurrentProcess()), original)

    @unittest.skipUnless(os.name == "nt", "Windows process scheduling")
    def test_idle_and_below_normal_rise_only_to_normal(self):
        for value in (0x40, 0x4000):
            with self.subTest(priority=value):
                kernel = Mock()
                kernel.GetPriorityClass.return_value = value
                with patch.object(ctypes, "WinDLL", return_value=kernel):
                    priority.foreground_priority()
                kernel.SetPriorityClass.assert_called_once_with(kernel.GetCurrentProcess.return_value, 0x20)

    @unittest.skipUnless(os.name == "nt", "Windows process scheduling")
    def test_normal_and_explicit_higher_priorities_are_unchanged(self):
        for value in (0x20, 0x8000, 0x80, 0x100):
            with self.subTest(priority=value):
                kernel = Mock()
                kernel.GetPriorityClass.return_value = value
                with patch.object(ctypes, "WinDLL", return_value=kernel):
                    priority.foreground_priority()
                kernel.SetPriorityClass.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows process scheduling")
    def test_priority_errors_abort_startup_instead_of_silently_serving(self):
        for value, changed in ((0, True), (0x4000, False), (0x40, False)):
            with self.subTest(priority=value):
                kernel = Mock()
                kernel.GetPriorityClass.return_value = value
                kernel.SetPriorityClass.return_value = changed
                with patch.object(ctypes, "WinDLL", return_value=kernel), \
                        patch.object(ctypes, "get_last_error", return_value=5), \
                        patch("uvicorn.run") as run:
                    with self.assertRaises(OSError):
                        server_admin.serve(host="127.0.0.1", port=8765)
                    run.assert_not_called()

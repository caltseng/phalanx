import ctypes
import os
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

from phalanx import priority, server_admin
from timing import WAIT_SECONDS


class ServerPriorityTest(unittest.TestCase):
    def test_api_normalizes_priority_before_serving(self):
        order = []
        with patch.object(server_admin, "foreground_priority", side_effect=lambda: order.append("priority")), \
                patch("uvicorn.run", side_effect=lambda *a, **k: order.append("serve")) as run:
            server_admin.serve(host="127.0.0.1", port=8765)
        self.assertEqual(order, ["priority", "serve"])
        run.assert_called_once_with("phalanx.api:app", host="127.0.0.1", port=8765, workers=1)

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

"""Own-process scheduling for critical foreground services, without server dependencies."""

import logging
import os


logger = logging.getLogger(__name__)


def foreground_priority():
    """Keep this critical Windows service out of an inherited background scheduling class."""
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.argtypes = []
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.GetPriorityClass.argtypes = [wintypes.HANDLE]
    kernel.GetPriorityClass.restype = wintypes.DWORD
    kernel.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.SetPriorityClass.restype = wintypes.BOOL
    process = kernel.GetCurrentProcess()
    priority = kernel.GetPriorityClass(process)
    if not priority:
        raise ctypes.WinError(ctypes.get_last_error())
    # Task Scheduler's default priority 7 is BelowNormal, which can starve control traffic.
    if priority in (0x40, 0x4000):  # Idle, BelowNormal
        if not kernel.SetPriorityClass(process, 0x20):  # Normal, never High/Realtime
            raise ctypes.WinError(ctypes.get_last_error())
        logger.warning("Service inherited background CPU priority; changed this process to Normal")

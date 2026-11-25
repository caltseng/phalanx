"""Server-host-only admin commands. They open the server's database directly."""

import logging
import os

from .config import database_path
from .store import snapshot


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


def serve(*, host, port):
    import uvicorn

    foreground_priority()
    uvicorn.run("phalanx.api:app", host=host, port=port, workers=1)


def run_server_command(args) -> None:
    if args.server_command == "backup":
        snapshot(database_path(), args.destination)
    else:
        snapshot(args.source, database_path())

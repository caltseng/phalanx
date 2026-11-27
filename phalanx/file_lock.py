"""Storage-free OS file locking shared by server administration and local workspaces."""

import os

if os.name == "nt":
    import msvcrt
else:
    import fcntl


def lock(handle) -> None:
    """Take a nonblocking exclusive lock; closing the handle releases it."""
    if os.name == "nt":
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

"""Trusted local checks from reconciler policy, and how to run one on a checkout."""

import os
import re
import signal
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .validation import PreCheck, PreOutcome

OUTPUT_TAIL_BYTES = 4096
# unittest/pytest failure lines, tracebacks, and progress lines with F or E markers.
_FAILURE = re.compile(
    rb"^(?:FAIL:|ERROR:|FAILED |Traceback \(most recent call last\))"
    rb"|^(?:\S+ )?[.sxXu]*[FE][.sxXuFE]*(?: +\[ *\d+%\])?\s*$"
)


class CheckCommandError(RuntimeError):
    """The configured check command could not start."""


@dataclass(frozen=True)
class LocalCheck:
    """One check from trusted reconciler policy for the registered repository."""

    policy: PreCheck
    argv: tuple[str, ...]
    timeout_seconds: int = 60

    def __post_init__(self):
        if not self.policy.name.strip():
            raise ValueError("A local check needs a name")
        if not self.argv or not Path(self.argv[0]).is_absolute() or self.timeout_seconds <= 0:
            raise ValueError("A check command needs an absolute executable and a positive timeout")
        if os.name == "nt" and Path(self.argv[0]).suffix.lower() != ".exe":
            raise ValueError("A Windows check command needs an executable (.exe)")


def run_check(check: LocalCheck, path: Path) -> tuple[PreOutcome, str, bool]:
    """Return the outcome, reason, and whether it only timed out with no failure identified."""
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith(("GIT_", "PHALANX_"))}
    # A file, unlike a pipe, cannot fill up and block the check or its children.
    with tempfile.TemporaryFile() as output:
        try:
            process = subprocess.Popen(check.argv, cwd=path, env=env, stdin=subprocess.DEVNULL,
                                       stdout=output, stderr=subprocess.STDOUT,
                                       start_new_session=os.name != "nt")
        except OSError as exc:
            raise CheckCommandError(f"Repair the configured check command (OS error {exc.errno})") from None
        timed_out = False
        try:
            returncode = process.wait(timeout=check.timeout_seconds)
            reason = f"Exited with status {returncode}" if returncode else ""
        except subprocess.TimeoutExpired:
            kill_tree(process)
            output.seek(0)
            timed_out = not any(_FAILURE.match(line) for line in output)
            returncode = None
            reason = (f"Timed out after {check.timeout_seconds} seconds"
                      + (", no failure identified" if timed_out else " with failures"))
        except BaseException:
            # An interrupt must not leave the check tree running in a worktree about to be removed.
            kill_tree(process)
            raise
        if returncode == 0:
            return PreOutcome.PASSED, "", False
        output.seek(max(0, output.seek(0, os.SEEK_END) - OUTPUT_TAIL_BYTES))
        tail = output.read().decode("utf-8", errors="replace").strip()
    return PreOutcome.FAILED, f"{reason}\n\nOutput tail:\n{tail}" if tail else reason, timed_out


def kill_tree(process: subprocess.Popen) -> None:
    """Stop a timed-out check and the processes it started, by its own process ID."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=False)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:  # The group may already be gone; macOS reports EPERM for zombies.
            pass
    process.kill()
    process.wait()

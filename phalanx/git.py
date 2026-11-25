"""Git execution confined to the caller's checkout, without inherited overrides."""

import os
import subprocess
import tempfile

from .local_checks import kill_tree

GIT_TIMEOUT_SECONDS = 60


def run_git(path, *args, check=True, stdin=None, error=RuntimeError,
            timeout=None, allowed_returncodes=None):
    timeout = GIT_TIMEOUT_SECONDS if timeout is None else timeout
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    command = ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", *args]
    try:
        # Files cannot leave communicate() waiting on a pipe inherited by a Git helper.
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as stderr, \
                tempfile.TemporaryFile() as input_file:
            if stdin is not None:
                input_file.write(stdin.encode())
                input_file.seek(0)
            process = subprocess.Popen(
                command, cwd=path, env=env, stdout=output, stderr=stderr,
                stdin=input_file if stdin is not None else subprocess.DEVNULL,
                start_new_session=os.name != "nt",
            )
            try:
                process.wait(timeout=timeout)
            except BaseException as exc:
                try:
                    kill_tree(process)
                except (OSError, subprocess.SubprocessError) as cleanup:
                    raise error(f"Git {args[0]} cleanup failed in {path} after "
                                f"{type(exc).__name__}: {cleanup}") from cleanup
                if isinstance(exc, subprocess.TimeoutExpired):
                    raise error(f"Git {args[0]} timed out after {timeout} seconds in {path}") from exc
                raise
            output.seek(0)
            stderr.seek(0)
            result = subprocess.CompletedProcess(
                command, process.returncode,
                output.read(os.fstat(output.fileno()).st_size),
                stderr.read(os.fstat(stderr.fileno()).st_size),
            )
    except (OSError, subprocess.SubprocessError) as exc:
        raise error(f"Cannot run Git in {path}: {exc}") from exc
    if (result.returncode < 0 or (check and result.returncode)
            or (allowed_returncodes is not None
                and (result.returncode not in allowed_returncodes or (result.returncode and result.stderr)))):
        detail = (result.stderr.decode("utf-8", errors="replace").strip()
                  or result.stdout.decode("utf-8", errors="replace").strip())
        raise error(f"Git {args[0]} failed in {path} (exit {result.returncode}): {detail}")
    return result


def git_text(path, *args, stdin=None, error=RuntimeError):
    return run_git(path, *args, stdin=stdin, error=error).stdout.decode("utf-8", errors="surrogateescape")

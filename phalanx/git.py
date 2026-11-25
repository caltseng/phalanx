"""Git execution confined to the caller's checkout, without inherited overrides."""

import os
import subprocess


def run_git(path, *args, check=True, stdin=None, error=RuntimeError):
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", *args],
            cwd=path, env=env, capture_output=True,
            **({"input": stdin.encode()} if stdin is not None else {"stdin": subprocess.DEVNULL}),
        )
    except OSError as exc:
        raise error(f"Cannot run Git in {path}: {exc}") from exc
    if check and result.returncode:
        detail = (result.stderr.decode("utf-8", errors="replace").strip()
                  or result.stdout.decode("utf-8", errors="replace").strip())
        raise error(f"Git {args[0]} failed in {path}: {detail}")
    return result


def git_text(path, *args, stdin=None, error=RuntimeError):
    return run_git(path, *args, stdin=stdin, error=error).stdout.decode("utf-8", errors="surrogateescape")

"""Runner startup checks that must pass before any claim."""

import importlib
import re
import shutil
import subprocess

from .attended import RunnerAPI
from .runner_errors import RunnerError


# `git worktree list --porcelain -z` needs Git 2.36.
MINIMUM_GIT = (2, 36)


def preflight(config: dict, token: str | None) -> None:
    """Report every startup blocker together, then stop before claiming work."""
    blockers = []
    try:
        # Draft publication imports these lazily; fail now instead of mid-claim.
        for module in ("phalanx.draft_stage", "phalanx.pr_models", "phalanx.validation_stages"):
            importlib.import_module(module)
    except ImportError as error:
        blockers.append(f"Missing package {error.name}; run `pip install -r requirements.lock` "
                        "and `pip install -e .` in the runner environment")
    if config.get("validation_policy"):
        from pathlib import Path
        from .validation_policy import ValidationPolicyError, load_validation_policy

        for repository in config["repositories"]:
            try:
                load_validation_policy(Path(config["validation_policy"]), repository)
            except ValidationPolicyError as error:
                # A repository without policy blocks only its own validation items.
                if not str(error).startswith("No validation policy for"):
                    blockers.append(str(error))
                    break
    git = shutil.which("git")
    try:
        found = subprocess.run([git, "--version"], capture_output=True, text=True, timeout=10).stdout if git else ""
    except (OSError, subprocess.SubprocessError):
        found = ""
    version = re.search(r"(\d+)\.(\d+)", found)
    if not version or tuple(map(int, version.groups())) < MINIMUM_GIT:
        blockers.append(f"Install Git 2.36 or later on PATH; found {found.strip() or 'no Git'}")
    if not token:
        blockers.append("Set PHALANX_RUNNER_TOKEN in the runner environment")
    else:
        try:
            RunnerAPI(config["base_url"], token).call("/repositories")
        except RunnerError as error:
            blockers.append(f"{error}; start the API server, check base_url {config['base_url']} "
                            "and the runner token")
    if blockers:
        raise RunnerError("Runner preflight failed: " + "; ".join(blockers))

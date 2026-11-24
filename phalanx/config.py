from dataclasses import dataclass, field
import os
from pathlib import Path
import re

@dataclass(frozen=True)
class Credentials:
    owner: str = field(repr=False)
    runner: str = field(repr=False)

    @classmethod
    def from_env(cls) -> "Credentials":
        owner = os.environ.get("PHALANX_OWNER_TOKEN", "")
        runner = os.environ.get("PHALANX_RUNNER_TOKEN", "")
        for role, token in (("OWNER", owner), ("RUNNER", runner)):
            if not re.fullmatch(r"[A-Za-z0-9_-]{32,}", token):
                raise ValueError(f"PHALANX_{role}_TOKEN requires at least 32 URL-safe characters")
        if owner == runner:
            raise ValueError("Owner and runner tokens must be distinct")
        return cls(owner, runner)


def database_path() -> Path:
    return Path(os.environ.get("PHALANX_DB", ".phalanx/phalanx.sqlite3")).resolve()


def local_board() -> bool:
    value = os.environ.get("PHALANX_LOCAL_BOARD", "")
    if value not in ("", "0", "1"):
        raise ValueError("PHALANX_LOCAL_BOARD must be 1 to open the local board, or 0 or unset")
    return value == "1"

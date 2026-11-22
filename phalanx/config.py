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


@dataclass(frozen=True)
class GoogleSignIn:
    client_id: str
    client_secret: str = field(repr=False)
    owner_emails: frozenset[str]

    @classmethod
    def from_env(cls) -> "GoogleSignIn | None":
        names = ("PHALANX_GOOGLE_CLIENT_ID", "PHALANX_GOOGLE_CLIENT_SECRET", "PHALANX_OWNER_EMAILS")
        values = [os.environ.get(name, "").strip() for name in names]
        if not any(values):
            return None
        if not all(values):
            raise ValueError("Google sign-in requires " + ", ".join(names))
        emails = frozenset(email.strip().lower() for email in values[2].split(",") if email.strip())
        if not all(re.fullmatch(r"[^@\s,]+@[^@\s,]+", email) for email in emails):
            raise ValueError("PHALANX_OWNER_EMAILS requires comma-separated email addresses")
        return cls(values[0], values[1], emails)


def database_path() -> Path:
    return Path(os.environ.get("PHALANX_DB", ".phalanx/phalanx.sqlite3")).resolve()

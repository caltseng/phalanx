"""Dispatch pause and lease renewal for the remaining runner-owned stages."""

from hmac import compare_digest
from time import time

from . import intake
from .config import DEFAULT_LEASE_SECONDS


LEASE_SECONDS = DEFAULT_LEASE_SECONDS


def set_dispatch(db, settings):
    db.execute("UPDATE dispatch_settings SET paused=?, aging_seconds=? WHERE id=1",
               (settings.paused, settings.aging_seconds))
    return settings.model_dump()


def renew(db, runner_id, attempt_id, lease_digest, worker=None, blocker=None):
    attempt = db.execute(
        "SELECT * FROM attempts WHERE id=? AND runner_id=? AND ended_at IS NULL",
        (attempt_id, runner_id),
    ).fetchone()
    if attempt is None:
        raise intake.Conflict("The attempt is no longer reserved by this runner")
    if not compare_digest(attempt["lease_digest"], lease_digest):
        raise intake.Conflict("The lease secret does not match the current attempt")
    now = time()
    if attempt["lease_expires_at"] <= now:
        raise intake.Conflict("Lease expired. Stop the worker and rerun the item through a direct Captain")
    expires = max(attempt["lease_expires_at"], int(now) + LEASE_SECONDS)
    db.execute("UPDATE attempts SET lease_expires_at=?, blocker=? WHERE id=?", (expires, blocker, attempt_id))
    if worker is not None:
        db.execute("INSERT OR REPLACE INTO attempt_workers VALUES (?, ?, ?)",
                   (attempt_id, worker.reference, worker.url))
    return {"attempt_id": attempt_id, "lease_expires_at": expires}

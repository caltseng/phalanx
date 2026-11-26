"""Server-derived flag representation shared by lifecycle and GitHub observations."""

from datetime import datetime


def flag(kind, reason, recovery, first_seen, *, row=None, item_id="", execution_id="", captain_id="",
         notify_owner=False):
    return {"kind": kind, "item_id": row["item_id"] if row is not None else item_id,
            "execution_id": row["execution_id"] if row is not None else execution_id, "captain_id": captain_id,
            "reason": reason, "recovery": recovery,
            "first_seen": first_seen.isoformat(timespec="microseconds") if isinstance(first_seen, datetime) else first_seen,
            "notify_owner": notify_owner, "source": "server"}

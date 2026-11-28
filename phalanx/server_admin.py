"""Server-host-only admin commands. They open the server's database directly."""

import os
from pathlib import Path

from .config import database_path
from .file_lock import lock
from .store import initialize, snapshot


def initialize_database():
    selected = os.environ.get("PHALANX_DB", "")
    if not selected or not Path(selected).is_absolute():
        raise ValueError("Initialization requires an explicit absolute PHALANX_DB")
    path = Path(selected).resolve()
    if path.exists():
        raise ValueError("Initialization requires a new database; never overwrite existing data")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(path.name + ".service.lock").open("a+b") as owner:
        try:
            lock(owner)
        except OSError as exc:
            raise ValueError("Cannot acquire sole database ownership for initialization") from exc
        # Reserve the new destination exclusively, including against a concurrent initializer.
        with path.open("xb"):
            pass
        initialize(path)
    print("Initialized a new Phalanx database; no HTTP server started.")


def serve(*, host, port):
    raise ValueError("Python HTTP server has been removed; use the verified packaged Go server. "
                     "See docs/python-server-deprecation.md; server initialize/backup/restore remain supported")


def run_server_command(args) -> None:
    if args.server_command == "initialize":
        initialize_database()
    elif args.server_command == "backup":
        snapshot(database_path(), args.destination)
    else:
        snapshot(args.source, database_path())

from contextlib import closing, contextmanager
from pathlib import Path
import os
import sqlite3
from tempfile import TemporaryDirectory
from threading import Lock

from .dependency_schema import DEPENDENCY_SCHEMA
from .dispatch_schema import DISPATCH_SCHEMA
from .finalization_schema import FINALIZATION_SCHEMA
from .intake_schema import INTAKE_SCHEMA


APPLICATION_ID = 0x50484C58  # PHLX
_write_lock = Lock()

# Each tuple is one migration.
MIGRATIONS = (
    (f"PRAGMA application_id = {APPLICATION_ID}",),
    INTAKE_SCHEMA,
    DISPATCH_SCHEMA,
    FINALIZATION_SCHEMA,
    DEPENDENCY_SCHEMA,
)


@contextmanager
def connect(path: Path, *, mode: str = "rw"):
    """Open a short-lived connection with explicit SQL transaction control."""
    uri = f"{path.resolve().as_uri()}?mode={mode}"
    with closing(sqlite3.connect(uri, uri=True, timeout=2, autocommit=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA synchronous = EXTRA")
        yield db


@contextmanager
def write_transaction(path: Path, *, create: bool = False):
    """Serialize short write transactions within the server process."""
    with _write_lock, connect(path, mode="rwc" if create else "rw") as db:
        db.execute("BEGIN IMMEDIATE")
        yield db
        db.execute("COMMIT")


def migrate(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with write_transaction(path, create=True) as db:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        app_id = db.execute("PRAGMA application_id").fetchone()[0]
        if version == 0:
            if app_id or db.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone():
                raise ValueError("Database is not an empty SQLite database or a Phalanx store")
        elif app_id != APPLICATION_ID or not 1 <= version <= len(MIGRATIONS):
            raise ValueError("Unsupported Phalanx database identity or schema version")
        if db.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
            raise ValueError("Phalanx requires SQLite DELETE journal mode")

        for next_version, statements in enumerate(MIGRATIONS[version:], start=version + 1):
            for statement in statements:
                db.execute(statement)
            db.execute(f"PRAGMA user_version = {next_version}")


def snapshot(source: Path, destination: Path) -> None:
    """Copy a validated SQLite snapshot to a new path without overwriting files."""
    if destination.exists():
        raise FileExistsError(destination)
    with TemporaryDirectory(prefix=".phalanx-copy-", dir=destination.parent) as staging:
        candidate = Path(staging) / "snapshot.sqlite3"
        with connect(source, mode="ro") as live, connect(candidate, mode="rwc") as copy:
            live.backup(copy)
            app_id = copy.execute("PRAGMA application_id").fetchone()[0]
            version = copy.execute("PRAGMA user_version").fetchone()[0]
            if app_id != APPLICATION_ID or not 1 <= version <= len(MIGRATIONS):
                raise ValueError("Unsupported Phalanx database identity or schema version")
            if [row[0] for row in copy.execute("PRAGMA integrity_check")] != ["ok"]:
                raise ValueError("SQLite snapshot failed integrity_check")
            copy.execute("PRAGMA journal_mode = DELETE")
        os.link(candidate, destination)

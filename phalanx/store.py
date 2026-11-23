from contextlib import ExitStack, closing, contextmanager
from pathlib import Path
import os
import sqlite3
from tempfile import TemporaryDirectory
from threading import Condition

from .dependency_schema import DEPENDENCY_SCHEMA
from .dispatch_schema import ADOPTION_SCHEMA, DISPATCH_SCHEMA, STAGE_QUEUE_SCHEMA
from .finalization_schema import FINALIZATION_SCHEMA
from .intake_schema import INTAKE_SCHEMA
from .operator_schema import BLOCKER_SCHEMA, OPERATOR_SCHEMA, WORKER_SCHEMA
from .wait_schema import WAIT_SCHEMA


# Version 11 rewrote pre-PH-15b rows. Its step stays empty so deployed stores keep their version.
REVISION_MIGRATION = ()


APPLICATION_ID = 0x50484C58  # PHLX
# Older stores needed row rewrites that were removed (decision 67), so they are refused.
OLDEST_SUPPORTED_VERSION = 12
BUSY_TIMEOUT = 2  # seconds


class _Gate:
    """Alternate serialized in-process writers with batches of waiting readers.

    In DELETE journal mode a writer blocks new readers while it commits, and SQLite's busy
    handler only polls. Writers queued here hand off to each other in microseconds, so a
    polling reader can miss every gap and time out. Readers instead wait here: those that
    arrive during a write start right after it, before the next writer, and those that arrive
    while a writer waits go after that writer. Each side waits at most one turn of the other,
    however deep the write queue gets, and neither adds a timeout of its own.
    """

    def __init__(self):
        self.condition = Condition()
        self.writing = False
        self.writers = 0  # writers waiting for their turn
        self.admitted = 0  # readers taking their read lock before the next writer
        self.queued = 0  # readers waiting for the current or next write to finish
        self.writes = 0  # finished writes

    @contextmanager
    def write(self):
        with self.condition:
            self.writers += 1
            self.condition.wait_for(lambda: not self.writing and not self.admitted)
            self.writers -= 1
            self.writing = True
        try:
            yield
        finally:
            with self.condition:
                self.writing = False
                self.writes += 1
                self.admitted += self.queued
                self.queued = 0
                self.condition.notify_all()

    @contextmanager
    def read_start(self):
        with self.condition:
            if self.writing or self.writers:
                self.queued += 1
                writes = self.writes
                # One write turn is already bounded by SQLite's busy timeout on BEGIN and COMMIT
                # but can outlast it in total, so a wall-clock bound here would fail healthy reads.
                self.condition.wait_for(lambda: self.writes != writes)
            else:
                self.admitted += 1
        try:
            yield
        finally:
            with self.condition:
                self.admitted -= 1
                self.condition.notify_all()


_gate = _Gate()

# Each tuple is one migration.
MIGRATIONS = (
    (f"PRAGMA application_id = {APPLICATION_ID}",),
    INTAKE_SCHEMA,
    DISPATCH_SCHEMA,
    FINALIZATION_SCHEMA,
    OPERATOR_SCHEMA,
    STAGE_QUEUE_SCHEMA,
    DEPENDENCY_SCHEMA,
    WAIT_SCHEMA,
    WORKER_SCHEMA,
    ADOPTION_SCHEMA,
    REVISION_MIGRATION,
    BLOCKER_SCHEMA,
)


@contextmanager
def connect(path: Path, *, mode: str = "rw"):
    """Open a short-lived connection with explicit SQL transaction control."""
    uri = f"{path.resolve().as_uri()}?mode={mode}"
    with closing(sqlite3.connect(uri, uri=True, timeout=BUSY_TIMEOUT, autocommit=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA synchronous = EXTRA")
        yield db


@contextmanager
def write_transaction(path: Path, *, create: bool = False):
    """Serialize short write transactions within the server process."""
    with _gate.write(), connect(path, mode="rwc" if create else "rw") as db:
        db.execute("BEGIN IMMEDIATE")
        yield db
        db.execute("COMMIT")


@contextmanager
def read_transaction(path: Path):
    """Read one consistent snapshot without being starved by back-to-back writers."""
    with ExitStack() as stack:
        # Opening runs pragmas that load the schema, so it needs the read lock too.
        with _gate.read_start():
            db = stack.enter_context(connect(path, mode="ro"))
            db.execute("BEGIN")
            db.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchall()
        yield db
        db.execute("COMMIT")


def _check_supported(app_id: int, version: int) -> None:
    if app_id != APPLICATION_ID or not 1 <= version <= len(MIGRATIONS):
        raise ValueError("Unsupported Phalanx database identity or schema version")
    if version < OLDEST_SUPPORTED_VERSION:
        raise ValueError(f"Phalanx store schema version {version} predates the oldest supported "
                         f"version {OLDEST_SUPPORTED_VERSION} and cannot be upgraded")


def migrate(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with write_transaction(path, create=True) as db:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        app_id = db.execute("PRAGMA application_id").fetchone()[0]
        if version == 0:
            if app_id or db.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone():
                raise ValueError("Database is not an empty SQLite database or a Phalanx store")
        else:
            _check_supported(app_id, version)
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
            _check_supported(app_id, version)
            if [row[0] for row in copy.execute("PRAGMA integrity_check")] != ["ok"]:
                raise ValueError("SQLite snapshot failed integrity_check")
            copy.execute("PRAGMA journal_mode = DELETE")
        os.link(candidate, destination)

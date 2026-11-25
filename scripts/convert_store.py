"""One-off, offline schema-24/25 conversion. Never called by the service."""

import argparse
from contextlib import closing
from hashlib import file_digest, sha256
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

from phalanx.store import APPLICATION_ID, SCHEMA_VERSION, connect, initialize


SCOUT_TABLES = {"issue_scout_settings", "issue_scout_passes", "issue_sources", "issue_scout_decisions"}


def quoted(name):
    return '"' + name.replace('"', '""') + '"'


def objects(db):
    """Compare logical sqlite_master, excluding physical page allocation."""
    return list(map(tuple, db.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name")))


def columns(db, table):
    return [row[1] for row in db.execute(f"PRAGMA table_xinfo({quoted(table)})") if row[6] == 0]


def check(db):
    if [row[0] for row in db.execute("PRAGMA integrity_check")] != ["ok"]:
        raise ValueError("Store failed integrity_check")
    if db.execute("PRAGMA foreign_key_check").fetchone():
        raise ValueError("Store failed foreign_key_check")


def inventory(db, table, names):
    """Hash ordered stored values, preserving duplicate rows and value types."""
    fields = ", ".join(map(quoted, names))
    digest, count = sha256(), 0
    for row in db.execute(f"SELECT {fields} FROM {quoted(table)} ORDER BY {fields}"):
        digest.update((json.dumps(tuple(row), ensure_ascii=True, separators=(",", ":"), allow_nan=False,
                                  default=lambda blob: {"blob_hex": blob.hex()}) + "\n").encode())
        count += 1
    return {"columns": names, "rows": count, "sha256": digest.hexdigest()}


def file_hash(path):
    with path.open("rb") as stream:
        return file_digest(stream, "sha256").hexdigest()


def convert(source: Path, destination: Path):
    """Read a snapshot, verify every retained value, and publish a new database atomically."""
    if destination.exists():
        raise FileExistsError(destination)
    if source.resolve() == destination.resolve():
        raise ValueError("Conversion must not overwrite its source")
    source_hash = file_hash(source)
    with TemporaryDirectory(prefix=".phalanx-convert-", dir=destination.parent) as directory:
        before = Path(directory) / "before.sqlite3"
        after = Path(directory) / "after.sqlite3"
        with connect(source, mode="ro") as original, closing(sqlite3.connect(before)) as copy:
            original.backup(copy)
        initialize(after)
        with connect(before, mode="ro") as old, connect(after) as new:
            source_version = old.execute("PRAGMA user_version").fetchone()[0]
            if old.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID or source_version not in (24, 25):
                raise ValueError("Expected the operator's reviewed schema-24/25 Phalanx snapshot")
            check(old)
            source_inventory = {
                row[0]: inventory(old, row[0], columns(old, row[0]))
                for row in old.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
            }
            expected = objects(new)
            tables = [row[0] for row in new.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            source_tables = {row[0] for row in old.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            retired_tables = {"attempt_workers", "external_rechecks"} if source_version == 24 else set()
            extra = source_tables - set(tables) - retired_tables
            missing = set(tables) - source_tables
            if extra or missing != SCOUT_TABLES:
                raise ValueError(f"Unexpected source tables (extra={sorted(extra)}, missing={sorted(missing)}); "
                                 "review the snapshot before conversion")
            retained, retired_columns = {}, {}
            for table in tables:
                if table in SCOUT_TABLES:
                    continue
                target = columns(new, table)
                removed = set(columns(old, table)) - set(target)
                allowed = ({"runner_id", "lease_digest", "lease_expires_at", "blocker"}
                           if table == "attempts" and source_version == 24 else set())
                if removed - allowed or set(target) - set(columns(old, table)):
                    raise ValueError(f"Unexpected columns in {table}")
                if removed:
                    retired_columns[table] = sorted(removed)
                retained[table] = target
            triggers = [row[0] for row in new.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' ORDER BY name")]
            new.execute("PRAGMA foreign_keys=OFF")
            new.execute("ATTACH DATABASE ? AS original", (before.resolve().as_uri() + "?mode=ro",))
            new.execute("BEGIN IMMEDIATE")
            for row in new.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall():
                new.execute(f"DROP TRIGGER {quoted(row[0])}")
            counts = {}
            for table, names in retained.items():
                fields = ", ".join(map(quoted, names))
                new.execute(f"DELETE FROM {quoted(table)}")
                new.executemany(
                    f"INSERT INTO {quoted(table)} ({fields}) VALUES ({', '.join('?' for _ in names)})",
                    old.execute(f"SELECT {fields} FROM {quoted(table)}"),
                )
                # EXCEPT compares values with SQLite's types and NULL semantics, not row ordering.
                for left, right in (("main", "original"), ("original", "main")):
                    if new.execute(
                        f"SELECT {fields} FROM {left}.{quoted(table)} EXCEPT "
                        f"SELECT {fields} FROM {right}.{quoted(table)} LIMIT 1"
                    ).fetchone():
                        raise ValueError(f"Conversion changed values in {table}")
                copied = inventory(new, table, names)
                if copied != inventory(old, table, names):
                    raise ValueError(f"Conversion changed row counts or typed values in {table}")
                counts[table] = copied["rows"]
            for trigger in triggers:
                new.execute(trigger)
            check(new)
            if objects(new) != expected:
                raise ValueError("Converted sqlite_master differs from a fresh store")
            new.execute("COMMIT")
        if file_hash(source) != source_hash:
            raise ValueError("Source changed during conversion; use a quiesced, retained snapshot")
        # A link publishes the complete checked file without replacing an existing destination.
        os.link(after, destination)
    return {"application_id": APPLICATION_ID, "schema_version": SCHEMA_VERSION,
            "sqlite_master_matches": True, "integrity_check": "ok", "foreign_key_check": [],
            "retained_values_match": True, "rows": counts,
            "source_archive": {"path": str(source.resolve()), "sha256": source_hash,
                               "schema_version": source_version, "tables": source_inventory},
            "added_tables": sorted(SCOUT_TABLES), "scouting_enabled": False,
            "retired_metadata": {"tables": sorted(source_tables - set(tables)), "columns": retired_columns}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="retained operator-owned schema-24 or schema-25 snapshot")
    parser.add_argument("destination", type=Path, help="new, nonexistent converted database path")
    args = parser.parse_args()
    print(json.dumps(convert(args.source, args.destination), indent=2))


if __name__ == "__main__":
    main()

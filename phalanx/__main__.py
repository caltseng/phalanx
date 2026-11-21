import argparse
from pathlib import Path
import sqlite3

from .config import database_path
from .store import snapshot


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m phalanx")
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("backup", help="snapshot PHALANX_DB to a new file")
    backup.add_argument("destination", type=Path)
    restore = commands.add_parser("restore", help="restore a snapshot to a new PHALANX_DB path")
    restore.add_argument("source", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "backup":
            snapshot(database_path(), args.destination)
        else:
            snapshot(args.source, database_path())
    except (OSError, ValueError, sqlite3.Error) as error:
        parser.exit(1, f"phalanx: {error}\n")


if __name__ == "__main__":
    main()

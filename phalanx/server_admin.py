"""Server-host-only admin commands. They open the server's database directly."""

from .config import database_path
from .store import snapshot


def run_server_command(args) -> None:
    if args.server_command == "backup":
        snapshot(database_path(), args.destination)
    else:
        snapshot(args.source, database_path())

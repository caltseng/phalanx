"""Server-host-only admin commands. They open the server's database directly."""

from .config import database_path
from .store import snapshot


def serve(*, host, port):
    import uvicorn

    uvicorn.run("phalanx.api:app", host=host, port=port, workers=1)


def run_server_command(args) -> None:
    if args.server_command == "backup":
        snapshot(database_path(), args.destination)
    else:
        snapshot(args.source, database_path())

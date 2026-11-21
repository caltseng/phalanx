import argparse
import json
import os
from pathlib import Path
import sqlite3
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .config import database_path
from .store import snapshot


def api_request(base_url, route, *, body=None, key=None):
    token = os.environ.get("PHALANX_OWNER_TOKEN", "")
    if not token:
        raise ValueError("PHALANX_OWNER_TOKEN is required for this command")
    headers = {"Authorization": f"Bearer {token}"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if key:
        headers["Idempotency-Key"] = key
    request = Request(base_url.rstrip("/") + route, data=data, headers=headers)
    try:
        with urlopen(request, timeout=10) as response:
            return json.load(response)
    except HTTPError as error:
        with error:
            detail = error.read().decode("utf-8", errors="replace")
        raise ValueError(f"HTTP {error.code}: {detail}") from error


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m phalanx")
    parser.add_argument("--api-url", default=os.environ.get("PHALANX_API_URL", "http://127.0.0.1:8000"))
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("backup", help="snapshot PHALANX_DB to a new file")
    backup.add_argument("destination", type=Path)
    restore = commands.add_parser("restore", help="restore a snapshot to a new PHALANX_DB path")
    restore.add_argument("source", type=Path)
    register = commands.add_parser("register", help="register an expected repository remote")
    register.add_argument("repository")
    register.add_argument("remote")
    intake = commands.add_parser("intake", help="file a work item")
    intake.add_argument("repository")
    intake.add_argument("goal")
    intake.add_argument("--target", choices=["report", "ready_for_check_in", "checked_in"], required=True)
    intake.add_argument("--key", help="reuse this key when retrying the same request")
    inspect = commands.add_parser("inspect", help="show a work item and its current execution")
    inspect.add_argument("item_id")
    inbox = commands.add_parser("inbox", help="show work awaiting owner input")
    inbox.add_argument("--limit", type=int, default=50)
    inbox.add_argument("--after-time", default="")
    inbox.add_argument("--after-id", default="")
    reply = commands.add_parser("reply", help="answer an active question")
    reply.add_argument("item_id")
    reply.add_argument("execution_id")
    reply.add_argument("answer")
    reply.add_argument("--version", type=int, required=True)
    reply.add_argument("--key", required=True, help="reuse this key when retrying the same reply")
    args = parser.parse_args()
    try:
        if args.command == "backup":
            snapshot(database_path(), args.destination)
        elif args.command == "restore":
            snapshot(args.source, database_path())
        elif args.command == "register":
            result = api_request(args.api_url, "/v1/repositories",
                                 body={"id": args.repository, "remote": args.remote})
            print(json.dumps(result, indent=2))
        elif args.command == "intake":
            result = api_request(
                args.api_url, "/v1/items", key=args.key,
                body={"repository_id": args.repository, "goal": args.goal, "target": args.target},
            )
            print(json.dumps(result, indent=2))
        elif args.command == "inbox":
            query = {"limit": args.limit, "after_time": args.after_time}
            if args.after_id:
                query["after_id"] = args.after_id
            result = api_request(args.api_url, "/v1/owner/inbox?" + urlencode(query))
            print(json.dumps(result, indent=2))
        elif args.command == "reply":
            item = quote(args.item_id, safe="")
            execution = quote(args.execution_id, safe="")
            result = api_request(
                args.api_url, f"/v1/items/{item}/executions/{execution}/reply", key=args.key,
                body={"expected_version": args.version, "answer": args.answer},
            )
            print(json.dumps(result, indent=2))
        else:
            result = api_request(args.api_url, f"/v1/items/{quote(args.item_id, safe='')}")
            print(json.dumps(result, indent=2))
    except (OSError, ValueError, sqlite3.Error) as error:
        parser.exit(1, f"phalanx: {error}\n")


if __name__ == "__main__":
    main()

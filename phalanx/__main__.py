import argparse
import json
import os
from pathlib import Path
import sqlite3
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .attended import RunnerError, add_attended_command, run_attended
from .attended_recovery_service import add_recovery_command, run_recovery_command
from .captain_loop import add_captain_command, run_captain
from .captain_relay import add_relay_command, run_relay
from .github import GitHubError
from .issue_intake import ingest_issue
from .workspaces import WorkspaceError


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
    server = commands.add_parser(
        "server", help="server-host-only admin; opens PHALANX_DB directly, never through the API")
    admin = server.add_subparsers(dest="server_command", required=True)
    backup = admin.add_parser("backup", help="snapshot PHALANX_DB to a new file")
    backup.add_argument("destination", type=Path)
    restore = admin.add_parser("restore", help="restore a snapshot to a new PHALANX_DB path")
    restore.add_argument("source", type=Path)
    register = commands.add_parser("register", help="register an expected repository remote")
    register.add_argument("repository")
    register.add_argument("remote")
    intake = commands.add_parser("intake", help="file a work item")
    intake.add_argument("repository")
    intake.add_argument("goal")
    intake.add_argument("--target", choices=["report", "ready_for_check_in", "checked_in"], required=True)
    intake.add_argument("--key", help="reuse this key when retrying the same request")
    issue = commands.add_parser("ingest-issue", help="file a registered repository's GitHub issue")
    issue.add_argument("repository", help="registered Phalanx repository ID")
    issue.add_argument("number", type=int, help="GitHub issue number in that repository")
    issue.add_argument("--target", choices=["report", "ready_for_check_in", "checked_in"], required=True)
    issue.add_argument("--key", help="override the stable issue key for an intentional new import")
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
    reply.add_argument("--answered-by", choices=("owner", "captain"), default="owner",
                       help="who answered: the owner, or the Captain triaging a routine question")
    onboard = commands.add_parser(
        "onboard", help="register, clone, map and configure validation for a target repository, "
                        "then report what is still missing")
    onboard.add_argument("repository", help="Phalanx repository ID")
    onboard.add_argument("--config", required=True, type=Path, help="runner config to update")
    onboard.add_argument("--state", required=True, type=Path, help="runner state directory")
    onboard.add_argument("--remote", help="origin URL to register and clone")
    onboard.add_argument("--checkout", help="local clone path; cloned when absent or empty")
    onboard.add_argument("--base-branch", help="default: the existing mapping, then origin/HEAD")
    onboard.add_argument("--project-id", help="worker runtime project for the checkout")
    onboard.add_argument("--host-id", help="worker runtime host; default: the one other mappings share")
    add_attended_command(commands)
    add_recovery_command(commands)
    add_captain_command(commands)
    add_relay_command(commands)
    args = parser.parse_args()
    try:
        if args.command == "server":
            from .server_admin import run_server_command

            run_server_command(args)
        elif args.command == "attended":
            run_attended(args)
        elif args.command == "attended-recover":
            run_recovery_command(args)
        elif args.command == "captain":
            run_captain(args)
        elif args.command == "relay":
            run_relay(args)
        elif args.command == "onboard":
            from .onboarding import onboard as run_onboard

            result = run_onboard(
                args.config, args.state, args.repository, remote=args.remote, checkout=args.checkout,
                base_branch=args.base_branch, project_id=args.project_id, host_id=args.host_id,
                owner_token=os.environ.get("PHALANX_OWNER_TOKEN"),
                runner_token=os.environ.get("PHALANX_RUNNER_TOKEN"),
                github_token=os.environ.get("PHALANX_GITHUB_TOKEN"),
            )
            print(json.dumps(result, indent=2))
            if not result["complete"]:
                raise SystemExit(1)
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
        elif args.command == "ingest-issue":
            result = ingest_issue(api_request, args.api_url, args.repository, args.number,
                                  args.target, key=args.key)
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
                body={"expected_version": args.version, "answer": args.answer,
                      "answered_by": args.answered_by},
            )
            print(json.dumps(result, indent=2))
        else:
            result = api_request(args.api_url, f"/v1/items/{quote(args.item_id, safe='')}")
            print(json.dumps(result, indent=2))
    except (OSError, ValueError, sqlite3.Error, RunnerError, WorkspaceError, GitHubError) as error:
        parser.exit(1, f"phalanx: {error}\n")


if __name__ == "__main__":
    main()

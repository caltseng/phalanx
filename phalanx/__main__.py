import argparse
import json
import os
from pathlib import Path
import sqlite3
from urllib.parse import quote, urlencode

from .api_client import APIError
from .deployment import add_deploy_command, run_deploy
from .github import GitHubError
from .issue_intake import ingest_issue
from .muster import add_muster_command, run_muster
from .reconciler import ReconcilerError, add_reconcile_command, run_reconcile
from .shift import add_shift_command, run_shift
from .workspaces import WorkspaceError
from .work_cli import add_work_command, request, run_work


def api_request(base_url, route, *, body=None, key=None):
    token = os.environ.get("PHALANX_OWNER_TOKEN", "")
    if not token:
        raise ValueError("PHALANX_OWNER_TOKEN is required for this command")
    return request(base_url, token, route, body=body, key=key, timeout=10)


TARGET_HELP = ("checked_in (default) merges the PR once GitHub's required reviews and checks pass, "
               "ready_for_check_in stops at the ready PR; "
               "report delivers a Markdown report")
TARGETS = ("report", "ready_for_check_in", "checked_in")


def prerequisite(value):
    upstream, separator, milestone = value.partition(":")
    if not upstream or (separator and milestone not in TARGETS):
        raise argparse.ArgumentTypeError(
            f"expected ITEM or ITEM:MILESTONE with milestone in {', '.join(TARGETS)}")
    return {"upstream_id": upstream, "milestone": milestone or "checked_in"}


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
    intake.add_argument("--target", choices=TARGETS, default="checked_in",
                        help=TARGET_HELP)
    intake.add_argument("--publication-mode", choices=["ready", "draft"], default="ready",
                        help="direct sessions: stop at a verified draft instead of promoting or merging")
    intake.add_argument("--key", help="reuse this key when retrying the same request")
    intake.add_argument("--label", help="group related items under this label on the work board")
    intake.add_argument("--after", type=prerequisite, action="append", default=[],
                        metavar="ITEM[:MILESTONE]",
                        help="wait for ITEM to reach MILESTONE (default checked_in); repeatable")
    intake.add_argument("--priority", choices=("low", "normal", "urgent"), default="normal")
    intake.add_argument("--context", action="append", default=[],
                        help="context link or note; repeatable")
    intake.add_argument("--accept", action="append", default=[],
                        help="acceptance criterion; repeatable")
    issue = commands.add_parser("ingest-issue", help="file a registered repository's GitHub issue")
    issue.add_argument("repository", help="registered Phalanx repository ID")
    issue.add_argument("number", type=int, help="GitHub issue number in that repository")
    issue.add_argument("--target", choices=TARGETS, default="checked_in",
                       help=TARGET_HELP)
    issue.add_argument("--publication-mode", choices=["ready", "draft"], default="ready")
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
    onboard.add_argument("--config", required=True, type=Path, help="Captain config to update")
    onboard.add_argument("--state", required=True, type=Path, help="workspace preparation scratch directory")
    onboard.add_argument("--remote", help="origin URL to register and clone")
    onboard.add_argument("--checkout", help="local clone path; cloned when absent or empty")
    onboard.add_argument("--base-branch", help="default: the existing mapping, then origin/HEAD")
    onboard.add_argument("--project-id", help="worker runtime project for the checkout")
    onboard.add_argument("--host-id", help="worker runtime host; default: the one other mappings share")
    cancel = commands.add_parser("cancel", help="cancel an item's current execution")
    cancel.add_argument("item_id")
    cancel.add_argument("--reason", required=True)
    add_work_command(commands)
    add_reconcile_command(commands)
    add_shift_command(commands)
    add_muster_command(commands)
    add_deploy_command(commands)
    args = parser.parse_args()
    try:
        if args.command == "deploy":
            if args.deploy_command == "run" and args.service == "server":
                from .server_admin import serve

                code = run_deploy(args, serve=serve)
            else:
                code = run_deploy(args)
            if code:
                parser.exit(code)
        elif args.command == "server":
            from .server_admin import run_server_command

            run_server_command(args)
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
        elif args.command == "work":
            run_work(args)
        elif args.command == "reconcile":
            if run_reconcile(args):
                parser.exit(1)
        elif args.command == "shift":
            run_shift(args)
        elif args.command == "muster":
            run_muster(args)
        elif args.command == "register":
            result = api_request(args.api_url, "/v1/repositories",
                                 body={"id": args.repository, "remote": args.remote})
            print(json.dumps(result, indent=2))
        elif args.command == "intake":
            body = {"repository_id": args.repository, "goal": args.goal, "target": args.target,
                    "context_links": args.context, "acceptance_criteria": args.accept,
                    "priority": args.priority, "publication_mode": args.publication_mode}
            if args.after:
                body["dependencies"] = args.after
            if args.label is not None:
                body["label"] = args.label
            result = api_request(args.api_url, "/v1/items", key=args.key, body=body)
            print(json.dumps(result, indent=2))
        elif args.command == "ingest-issue":
            result = ingest_issue(api_request, args.api_url, args.repository, args.number,
                                  args.target, key=args.key, publication_mode=args.publication_mode)
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
        elif args.command == "cancel":
            item = quote(args.item_id, safe="")
            execution = api_request(args.api_url, f"/v1/items/{item}")["execution"]
            # The key is bound to the version read, so a retried request is idempotent and a
            # retry after the state moved fails visibly on the stale version instead.
            result = api_request(
                args.api_url, f"/v1/items/{item}/executions/{quote(execution['id'], safe='')}/cancel",
                key=f"cancel-{execution['id']}-{execution['version']}",
                body={"expected_version": execution["version"], "reason": args.reason},
            )
            print(json.dumps(result, indent=2))
        else:
            result = api_request(args.api_url, f"/v1/items/{quote(args.item_id, safe='')}")
            print(json.dumps(result, indent=2))
    except (OSError, ValueError, sqlite3.Error, APIError, WorkspaceError, GitHubError,
            ReconcilerError) as error:
        parser.exit(1, f"phalanx: {error}\n")


if __name__ == "__main__":
    main()

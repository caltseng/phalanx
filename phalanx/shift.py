"""`phx shift`: a Captain's commands on the direct path.

A Captain is an agent chat that launches and coordinates native worker sessions through its
runtime's session tools. It keeps no local state. This host holds only the shift configuration,
the charter and each item worktree's writer credential file; everything else is server state.
"""

from argparse import ArgumentParser
from datetime import UTC, datetime
import hashlib
from importlib import resources
import json
import os
from pathlib import Path
import secrets
from string import Template
import subprocess
import tomllib
from urllib.parse import quote, urlencode

from .work_cli import CREDENTIAL, request
from .workspaces import WorkspaceManager

CHARTER_LISTS = ("repositories", "allowed_actions", "escalate", "stop_when")
# Shift bounds (D7). A Captain also ends its shift at its first compaction or a degradation signal.
SHIFT_DEFAULTS = {"max_hours": 4, "max_wakes": 48}
MAX_OPEN_ITEMS = 1000


class ShiftError(ValueError):
    pass


def load_config(path):
    if path is None:
        raise ShiftError("Pass --config or set PHALANX_SHIFT_CONFIG")
    config = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    missing = [key for key in ("base_url", "repositories", "charter", "worktrees", "runtime")
               if not config.get(key)]
    if missing:
        raise ShiftError(f"Shift config {path} needs {', '.join(missing)}")
    return config


def _git_tree(path):
    result = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=path, capture_output=True,
                            text=True, stdin=subprocess.DEVNULL)
    return result.returncode == 0 and result.stdout.strip() == "true"


def load_charter(config):
    """Read and validate the charter. Its revision is the SHA-256 of the file's bytes."""
    path = Path(config["charter"]).resolve()
    data = path.read_bytes()
    guarded = [Path(mapping["checkout"]).resolve() for mapping in config["repositories"].values()]
    guarded.append(Path(config["worktrees"]).resolve())
    if any(path.is_relative_to(root) for root in guarded) or _git_tree(path.parent):
        raise ShiftError(f"The charter {path} must live outside every checkout, worktree and Git repository")
    try:
        charter = tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ShiftError(f"The charter {path} is not valid TOML: {error}") from None
    unknown = set(charter) - {"goal", "notes", "shift", *CHARTER_LISTS}
    if unknown:
        raise ShiftError(f"Unknown charter settings: {', '.join(sorted(unknown))}")
    for key in ("goal", "notes"):
        if (key == "goal" or key in charter) and not (isinstance(charter.get(key), str) and charter[key].strip()):
            raise ShiftError(f"Charter {key} must be non-empty text")
    for key in CHARTER_LISTS:
        value = charter.get(key)
        if not (isinstance(value, list) and value
                and all(isinstance(entry, str) and entry.strip() for entry in value)):
            raise ShiftError(f"Charter {key} must be a non-empty list of text")
    unmapped = sorted(set(charter["repositories"]) - set(config["repositories"]))
    if unmapped:
        raise ShiftError(f"Charter repositories without a checkout mapping: {', '.join(unmapped)}")
    shift = charter.get("shift", {})
    if not isinstance(shift, dict) or set(shift) - set(SHIFT_DEFAULTS) or not all(
            type(value) is int and value > 0 for value in shift.values()):
        raise ShiftError("Charter [shift] takes positive integers max_hours and max_wakes")
    return {**charter, "shift": {**SHIFT_DEFAULTS, **shift}, "revision": hashlib.sha256(data).hexdigest(),
            "path": str(path)}


class API:
    def __init__(self, config):
        self.base_url = config["base_url"]
        self.token = os.environ.get("PHALANX_RUNNER_TOKEN")
        if not self.token:
            raise ShiftError("Set PHALANX_RUNNER_TOKEN for Captain commands")

    def __call__(self, route, **options):
        return request(self.base_url, self.token, route, **options)


def _q(value):
    return quote(value, safe="")


def shift_bounds(captain, charter, now=None):
    age = (now or datetime.now(UTC)) - datetime.fromisoformat(captain["created_at"])
    bounds = charter["shift"]
    due = []
    if age.total_seconds() >= bounds["max_hours"] * 3600:
        due.append(f"shift age reached {bounds['max_hours']} h")
    if captain["wake_count"] >= bounds["max_wakes"]:
        due.append(f"wake count reached {bounds['max_wakes']}")
    return {"age_minutes": int(age.total_seconds() // 60), "wakes": captain["wake_count"], **bounds,
            "handoff_due": due}


def start(api, charter, session_ref, parent_id=None):
    body = {"session_ref": session_ref, "charter_revision": charter["revision"]}
    if parent_id:
        body["parent_id"] = parent_id
    # A lost reply replays the same registration instead of leaving a stale twin.
    key = "captain-" + hashlib.sha256(f"{session_ref}\0{parent_id or ''}".encode()).hexdigest()
    captain = api("/v1/captains", body=body, key=key)
    return {"captain": captain, "charter": charter, "shift": shift_bounds(captain, charter)}


def heartbeat(api, charter, captain_id, *, wake=False, state=None, focus=None, sessions=None,
              handoff_note=None):
    """Post a heartbeat with the charter's current revision. Omitted fields keep their last value."""
    current = api(f"/v1/captains/{_q(captain_id)}")
    body = {
        "charter_revision": charter["revision"],
        "wake_count": current["wake_count"] + int(wake),
        "state": state or current["state"],
        "focus": current["focus"] if focus is None else focus,
        "sessions": current["sessions"] if sessions is None else sessions,
        "handoff_note": current["handoff_note"] if handoff_note is None else handoff_note,
    }
    captain = api(f"/v1/captains/{_q(captain_id)}", body=body, method="PUT")
    return {"captain": captain, "charter": charter, "shift": shift_bounds(captain, charter)}


def _summary(item):
    writer = item["writer"]
    return {
        "item_id": item["item_id"], "execution_id": item["id"], "version": item["version"],
        "repository_id": item["repository_id"], "title": item["title"], "stage": item["stage"],
        "status": item["status"], "wait_reason": item["wait_reason"],
        "blocked_prerequisites": item["blocked_prerequisite_count"], "primary_pr_url": item["primary_pr_url"],
        "writer": writer and {key: writer[key] for key in ("id", "state", "captain_id", "session_id",
                                                             "last_progress_at", "progress_note")},
    }


def status(api, charter):
    """Flags, Captains and the charter's open items: what a wake reconciles."""
    items, cursor = [], None
    while len(items) < MAX_OPEN_ITEMS:
        query = {"kind": "open", "limit": 100, **({"cursor": cursor} if cursor else {})}
        page = api("/v1/operator/executions?" + urlencode(query))
        items.extend(page["items"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    scope = set(charter["repositories"])
    return {
        "flags": api("/v1/flags")["flags"],
        "captains": api("/v1/captains?limit=100")["captains"],
        "items": [_summary(item) for item in items if item["repository_id"] in scope],
        "out_of_scope_open_items": sum(item["repository_id"] not in scope for item in items),
    }


def _fetch(checkout, branch):
    for args in (["check-ref-format", f"refs/heads/{branch}"],
                 ["fetch", "--quiet", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}"]):
        result = subprocess.run(["git", *args], cwd=checkout, capture_output=True, text=True,
                                stdin=subprocess.DEVNULL)
        if result.returncode:
            raise ShiftError(f"git {args[0]} failed for {branch} in {checkout}: {result.stderr.strip()}")


def credential_path(worktree):
    result = subprocess.run(["git", "rev-parse", "--git-path", CREDENTIAL], cwd=worktree,
                            capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if result.returncode:
        raise ShiftError(f"Cannot locate the writer credential in {worktree}: {result.stderr.strip()}")
    return Path(worktree) / result.stdout.strip()


def _write_credential(path, data):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data), encoding="utf-8")
    os.replace(temporary, path)


def kickoff(item, worktree, nonce, work_command):
    criteria = item.get("acceptance_criteria") or []
    template = Template(resources.files("phalanx").joinpath("prompts", "worker.md").read_text(encoding="utf-8"))
    return template.substitute(
        item_id=item["id"], execution_id=item["execution"]["id"], repository=item["repository_id"],
        target=item["target"], goal=item["goal"], worktree=worktree, nonce=nonce, work=work_command,
        criteria="\n".join(f"- {entry}" for entry in criteria) or "- None beyond the goal",
    )


def launch(api, config, charter, captain_id, item_id, *, replaces=None):
    """Prepare the item's worktree and register a writer intent, ready for its session to launch.

    The nonce and token are written to the worktree's credential file before registering, so a
    rerun after a lost reply replays the same registration. Only an ended writer gets a new nonce.
    """
    item = api(f"/v1/items/{_q(item_id)}")
    repository = item["repository_id"]
    if repository not in charter["repositories"]:
        raise ShiftError(f"Repository {repository} is outside the charter")
    mapping = config["repositories"][repository]
    remote = next((entry["remote"] for entry in api("/v1/repositories") if entry["id"] == repository), None)
    if remote is None:
        raise ShiftError(f"Repository {repository} is not registered")
    manager = WorkspaceManager(Path(mapping["checkout"]), remote, Path(config["worktrees"]))
    manager.verify_checkout()
    base = item.get("target_branch") or mapping["base_branch"]
    _fetch(mapping["checkout"], base)
    workspace = manager.prepare(item_id, base=manager.resolve_origin_base(base))
    path = credential_path(workspace.path)
    intent = {"item_id": item_id, "execution_id": item["execution"]["id"], "captain_id": captain_id,
              "replaces": replaces}
    saved = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    route = f"/v1/items/{_q(item_id)}/executions/{_q(intent['execution_id'])}/writer"
    version = item["execution"]["version"]
    for fresh in (False, True):
        if fresh or not saved.get("nonce") or any(saved.get(key) != value for key, value in intent.items()):
            saved = {**intent, "base_url": config["base_url"], "nonce": secrets.token_urlsafe(16),
                     "token": secrets.token_urlsafe(32)}
            _write_credential(path, saved)
        if fresh:
            version = api(f"/v1/items/{_q(item_id)}")["execution"]["version"]
        binding = api(route, body={
            "expected_version": version,
            "captain_id": captain_id, "runtime": config["runtime"], "nonce": saved["nonce"],
            "token_digest": hashlib.sha256(saved["token"].encode()).hexdigest(),
            "replaces": replaces, "replaced_stopped": replaces is not None,
        })
        if binding["writer"]["state"] != "ended":
            break
    else:
        raise ShiftError("The new writer ended at registration; inspect the item")
    nonce = saved["nonce"]
    return {
        "item_id": item_id, "execution_id": binding["id"], "version": binding["version"],
        "stage": binding["state"]["stage"], "status": binding["state"]["status"], "writer": binding["writer"],
        "worktree": str(workspace.path), "repository": {"id": repository, **mapping},
        "session_name": f"phx {item_id[:8]} {nonce}",
        "kickoff": kickoff(item, workspace.path, nonce, config.get("work_command", "python -m phalanx work")),
    }


def _writer_route(args, suffix=""):
    return f"/v1/items/{_q(args.item)}/executions/{_q(args.execution)}/writer{suffix}"


def add_shift_command(commands):
    shared = ArgumentParser(add_help=False)
    shared.add_argument("--config", type=Path, default=os.environ.get("PHALANX_SHIFT_CONFIG"),
                        help="shift config JSON (default: PHALANX_SHIFT_CONFIG)")
    shift = commands.add_parser("shift", help="a Captain's commands on the direct path (runner token)")
    actions = shift.add_subparsers(dest="shift_command", required=True)
    prompt = actions.add_parser("prompt", help="print the Captain prompt")
    prompt.add_argument("name", choices=["captain"])
    actions.add_parser("charter", parents=[shared], help="validate the charter and show its revision")
    begin = actions.add_parser("start", parents=[shared], help="register this chat as a Captain")
    begin.add_argument("--session-ref", required=True, help="this chat's native session ID or link")
    begin.add_argument("--parent", help="the Captain that launched this one")
    beat = actions.add_parser("heartbeat", parents=[shared], help="re-read the charter and heartbeat")
    beat.add_argument("--captain", required=True)
    beat.add_argument("--wake", action="store_true", help="count this heartbeat as a wake")
    beat.add_argument("--state", choices=["active", "ending", "ended"])
    beat.add_argument("--focus", nargs="*", help="item IDs this wake acts on")
    beat.add_argument("--session", nargs="*", dest="sessions", help="native sessions visible to you")
    beat.add_argument("--handoff-note")
    actions.add_parser("status", parents=[shared], help="flags, Captains and the charter's open items")
    start_writer = actions.add_parser("launch", parents=[shared],
                                      help="prepare an item's worktree and register its writer intent")
    start_writer.add_argument("--captain", required=True)
    start_writer.add_argument("--item", required=True)
    start_writer.add_argument("--replaces", metavar="WRITER",
                              help="replace this open writer, attesting that its session is stopped")
    for name, help_text in (("writer", "confirm a launched session or report its state"),
                            ("withdraw", "end a writer whose session is stopped or never launched")):
        action = actions.add_parser(name, parents=[shared], help=help_text)
        for option in ("--captain", "--item", "--execution", "--writer"):
            action.add_argument(option, required=True)
    writer = actions.choices["writer"]
    writer.add_argument("--session-id", help="the launched native session's ID (confirms the launch)")
    writer.add_argument("--session-url")
    writer.add_argument("--state", choices=["busy", "idle", "stopped", "error"])
    writer.add_argument("--abandon", action="store_true",
                        help="hand this unconfirmed intent to any live Captain")
    withdraw = actions.choices["withdraw"]
    withdraw.add_argument("--reason", required=True)
    withdraw.add_argument("--stopped", action="store_true", help="attest that the session is stopped")


def run_shift(args):
    if args.shift_command == "prompt":
        print(resources.files("phalanx").joinpath("prompts", f"{args.name}.md").read_text(encoding="utf-8"))
        return
    config = load_config(args.config)
    charter = load_charter(config)
    if args.shift_command == "charter":
        result = charter
    elif args.shift_command == "start":
        result = start(API(config), charter, args.session_ref, args.parent)
    elif args.shift_command == "heartbeat":
        result = heartbeat(API(config), charter, args.captain, wake=args.wake, state=args.state,
                           focus=args.focus, sessions=args.sessions, handoff_note=args.handoff_note)
    elif args.shift_command == "status":
        result = status(API(config), charter)
    elif args.shift_command == "launch":
        result = launch(API(config), config, charter, args.captain, args.item, replaces=args.replaces)
    elif args.shift_command == "writer":
        body = {"captain_id": args.captain, "writer_id": args.writer}
        for field in ("session_id", "session_url", "state"):
            if getattr(args, field) is not None:
                body[field] = getattr(args, field)
        if args.abandon:
            body["abandon"] = True
        result = API(config)(_writer_route(args), body=body, method="PATCH")
    else:
        result = API(config)(_writer_route(args, "/withdraw"), body={
            "captain_id": args.captain, "writer_id": args.writer, "reason": args.reason,
            "stopped": args.stopped})
    print(json.dumps(result, indent=2))

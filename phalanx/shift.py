"""`phx shift`: a Captain's commands on the direct path.

A Captain is an agent chat that launches and coordinates native worker sessions through its
runtime's session tools. It keeps no local state. This host holds only the shift configuration,
the charter and each item worktree's writer credential file; everything else is server state.
"""

from argparse import SUPPRESS, ArgumentParser
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
from .workspaces import WorkspaceError, WorkspaceManager, _git, _status

CHARTER_LISTS = ("repositories", "allowed_actions", "escalate", "stop_when")
# Shift bounds (D7). A Captain also ends its shift at its first compaction or a degradation signal.
SHIFT_DEFAULTS = {"max_hours": 4, "max_wakes": 48, "max_writers": 4}
# A launch's staged intent. It becomes the live credential once the server accepts its nonce.
PENDING = "phalanx-writer.pending.json"


class ShiftError(ValueError):
    pass


def load_config(path, *, home=None):
    if path is None:
        raise ShiftError("Pass --config or set PHALANX_SHIFT_CONFIG")
    config = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(config, dict):
        raise ShiftError(f"Shift config {path} must be a JSON object")
    if home is not None:
        expected = home / "charter.toml"
        charter = config.get("charter", "charter.toml")
        if not isinstance(charter, str) or not charter.strip():
            raise ShiftError(f"Discovered shift config must use muster charter {expected}")
        configured = Path(charter)
        if not configured.is_absolute():
            configured = home / configured
        if configured.resolve() != expected.resolve():
            raise ShiftError(f"Discovered shift config must use muster charter {expected}")
        config["charter"] = str(expected)
    missing = [key for key in ("base_url", "repositories", "charter", "worktrees", "runtime")
               if not config.get(key)]
    if missing:
        raise ShiftError(f"Shift config {path} needs {', '.join(missing)}")
    for repository, mapping in config["repositories"].items():
        if "checks_environment" in mapping:
            environment = mapping["checks_environment"]
            if not isinstance(environment, str) or not environment.strip():
                raise ShiftError(f"Repository {repository} checks_environment must be non-empty text")
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
        raise ShiftError("Charter [shift] takes positive integers max_hours, max_wakes and max_writers")
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
    if sessions is None:
        sessions = [{"id": session["id"], "name": session["name"]} for session in current["sessions"]]
    body = {
        "charter_revision": charter["revision"],
        "wake_count": current["wake_count"] + int(wake),
        "state": state or current["state"],
        "focus": current["focus"] if focus is None else focus,
        "sessions": sessions,
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


def status(api, charter, captain_id=None):
    """Flags, Captains and the charter's open items: what a wake reconciles."""
    items, cursor = [], None
    while True:
        query = {"kind": "open", "limit": 100, **({"cursor": cursor} if cursor else {})}
        page = api("/v1/operator/executions?" + urlencode(query))
        items.extend(page["items"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    scope = set(charter["repositories"])
    scoped = [_summary(item) for item in items if item["repository_id"] in scope]
    return {
        "flags": api("/v1/flags")["flags"],
        "captains": api("/v1/captains?limit=100")["captains"],
        "items": scoped,
        "active_writers": sum(item["writer"] is not None for item in scoped),
        "max_writers": charter["shift"]["max_writers"],
        "out_of_scope_open_items": sum(item["repository_id"] not in scope for item in items),
        **({"captain": api(f"/v1/captains/{_q(captain_id)}")} if captain_id else {}),
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


def _read_credential(path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def kickoff(item, worktree, nonce, work_command, base, checks_environment=None):
    criteria = item.get("acceptance_criteria") or []
    mode = item["execution"]["state"].get("publication_mode", "ready")
    if mode == "draft":
        publication = (
            f"open one draft PR with an explicit base (`gh pr create --draft --base {base}`). "
            "Reuse the branch's open PR instead of creating another. Disable any existing auto-merge "
            "(`gh pr merge --disable-auto`); if it is ready, convert it to draft (`gh pr ready --undo`). "
            f"Retarget it to `{base}` if needed. Never promote, enable auto-merge or merge this PR, "
            "even for a `checked_in` target. Delivery stops at a verified draft."
        )
    else:
        publication = (
            f"open one ready (not draft) PR with an explicit base (`gh pr create --base {base}`). "
            "Reuse the branch's open PR instead of creating another: mark it ready if it is a draft "
            f"(`gh pr ready`) and retarget it to `{base}` if needed. For a `checked_in` target, "
            "enable GitHub auto-merge with any method the repository allows."
        )
    template = Template(resources.files("phalanx").joinpath("prompts", "worker.md").read_text(encoding="utf-8"))
    return template.substitute(
        item_id=item["id"], execution_id=item["execution"]["id"], repository=item["repository_id"],
        target=item["execution"]["state"]["target"], goal=item["goal"], worktree=worktree, nonce=nonce, work=work_command,
        base=base, publication=publication, publication_mode=mode,
        checks_environment=(f"`{checks_environment}`" if checks_environment is not None
                            else "Not configured. Do not guess an interpreter or command prefix."),
        criteria="\n".join(f"- {entry}" for entry in criteria) or "- None beyond the goal",
    )


def launch(api, config, charter, captain_id, item_id, *, replaces=None):
    """Prepare the item's worktree and register a writer intent, ready for its session to launch.

    The nonce and token are staged in a pending file next to the worktree's credential before
    registering, so a rerun after a lost reply replays the same registration. The pending file
    replaces the live credential only once the server accepts that nonce, so a refused launch never
    disturbs the current writer. Only an ended writer gets a new nonce.
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
    pending = path.with_name(PENDING)
    intent = {"item_id": item_id, "execution_id": item["execution"]["id"], "captain_id": captain_id,
              "replaces": replaces}

    def matches(data):
        return data.get("nonce") and all(data.get(key) == value for key, value in intent.items())

    saved = next((data for data in map(_read_credential, (pending, path)) if matches(data)), None)
    route = f"/v1/items/{_q(item_id)}/executions/{_q(intent['execution_id'])}/writer"
    version = item["execution"]["version"]
    for fresh in (False, True):
        if fresh or saved is None:
            saved = {**intent, "base_url": config["base_url"], "nonce": secrets.token_urlsafe(16),
                     "token": secrets.token_urlsafe(32)}
            _write_credential(pending, saved)
        if fresh:
            version = api(f"/v1/items/{_q(item_id)}")["execution"]["version"]
        binding = api(route, body={
            "expected_version": version,
            "captain_id": captain_id, "runtime": config["runtime"], "nonce": saved["nonce"],
            "token_digest": hashlib.sha256(saved["token"].encode()).hexdigest(),
            "replaces": replaces, "replaced_stopped": replaces is not None,
            "writer_limit": {"max_writers": charter["shift"]["max_writers"],
                             "repositories": charter["repositories"]},
        })
        if binding["writer"]["state"] != "ended":
            break
    else:
        raise ShiftError("The new writer ended at registration; inspect the item")
    nonce = saved["nonce"]
    if _read_credential(pending).get("nonce") == nonce:
        os.replace(pending, path)
    return {
        "item_id": item_id, "execution_id": binding["id"], "version": binding["version"],
        "stage": binding["state"]["stage"], "status": binding["state"]["status"], "writer": binding["writer"],
        "worktree": str(workspace.path), "repository": {"id": repository, **mapping},
        "session_workspace": config.get("session_workspace"),
        "session_name": f"phx {item_id[:8]} {nonce}",
        "kickoff": kickoff(item, workspace.path, nonce, config.get("work_command", "python -m phalanx work"),
                           base, mapping.get("checks_environment")),
    }


def prompt(name):
    return resources.files("phalanx").joinpath("prompts", f"{name}.md").read_text(encoding="utf-8")


def archive_result(api, session_id, outcome, reason, reported_by):
    return api("/v1/session-archives", method="PUT", body={
        "session_id": session_id, "outcome": outcome, "reason": reason, "reported_by": reported_by})


def archive_check(api, config, charter, session_id, reported_by, *, item_id=None, worktree=None,
                  no_worktree=False):
    """Check preservation only; the creator uses its runtime's tools to actually archive."""
    current = api("/v1/session-archives?" + urlencode({"session_id": session_id}))
    record = current["record"]
    if record and record["outcome"] in ("archived", "refused"):
        return {"can_archive": False, **record}
    if current["active"]:
        return {"can_archive": False, "reason": "The writer, Captain handoff or creator cleanup has not ended"}
    try:
        if current["writers"] and (not item_id or any(
                writer["item_id"] != item_id for writer in current["writers"])):
            raise WorkspaceError("Use the registered writer's --item for its archive check")
        if item_id:
            item = api(f"/v1/items/{_q(item_id)}")
            repository = item["repository_id"]
            if repository not in charter["repositories"]:
                raise WorkspaceError(f"Repository {repository} is outside the charter")
            mapping = config["repositories"][repository]
            remote = next(entry["remote"] for entry in api("/v1/repositories") if entry["id"] == repository)
            workspace = WorkspaceManager(mapping["checkout"], remote, config["worktrees"]).inspect(item_id)
            worktree = workspace.path
        elif not worktree and not no_worktree:
            raise WorkspaceError("Identify the session's worktree, or explicitly pass --no-worktree")
        if worktree:
            path = Path(worktree)
            if _status(path):
                raise WorkspaceError(f"Worktree has local changes or untracked files: {path}")
            entries = _git(path, "ls-files", "-v", "-z")
            if any(not entry.startswith("H ") for entry in entries.split("\0") if entry):
                raise WorkspaceError(f"Index entries can hide local changes: {path}")
            git_dir = Path(_git(path, "rev-parse", "--absolute-git-dir").strip())
            if any((git_dir / name).exists() for name in (
                    "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "REBASE_HEAD",
                    "rebase-merge", "rebase-apply", "sequencer", "BISECT_START")):
                raise WorkspaceError(f"Worktree has an in-progress Git operation: {path}")
            branch = _git(path, "symbolic-ref", "--short", "HEAD").strip()
            _git(path, "check-ref-format", f"refs/heads/{branch}")
            if not _git(path, "ls-remote", "--heads", "origin", f"refs/heads/{branch}").strip():
                if not item_id:
                    raise WorkspaceError(f"Origin branch {branch} is absent; use --item to identify its base")
                branch = item.get("target_branch") or mapping["base_branch"]
                _git(path, "check-ref-format", f"refs/heads/{branch}")
            ref = f"refs/remotes/origin/{branch}"
            _git(path, "fetch", "--quiet", "origin", f"+refs/heads/{branch}:{ref}")
            unpushed = _git(path, "--no-replace-objects", "rev-list", "HEAD", "--not", ref).strip()
            if unpushed:
                raise WorkspaceError(f"Worktree has commits not pushed to origin/{branch}: {path}")
    except WorkspaceError as error:
        record = archive_result(api, session_id, "blocked", str(error), reported_by)
        return {"can_archive": False, **record}
    return {"can_archive": True, "session_id": session_id,
            "reason": "Commits are preserved on origin and worktree is clean" if worktree else "No worktree to remove"}


def handoff(api, charter, captain_id, note, command):
    """Start ending this shift and say how to launch, or find, the successor Captain.

    The server names the successor after this Captain, so a rerun after a lost reply finds the chat
    it already launched instead of starting a second one. The kickoff names `command`, the concrete
    `phx shift` command, because a new chat can't know which command printed it.
    """
    ending = heartbeat(api, charter, captain_id, state="ending", handoff_note=note)
    captain = ending["captain"]
    successors = [api(f"/v1/captains/{_q(successor)}") for successor in captain["successors"]]
    return {
        **ending,
        "open_intents": captain["open_intents"],
        "successors": successors,
        "session_name": captain["successor_name"],
        "kickoff": (f"You succeed Phalanx Captain {captain_id}. In this chat, `phx shift` means `{command}`. "
                    f"On your first wake, run "
                    f"`phx shift start --session-ref <your native session ID> --parent {captain_id}`.\n\n"
                    + prompt("captain")),
    }


def _writer_route(args, suffix=""):
    return f"/v1/items/{_q(args.item)}/executions/{_q(args.execution)}/writer{suffix}"


def add_shift_command(commands):
    # --config is accepted before or after the action. The action's copy only overrides when given.
    shared = ArgumentParser(add_help=False)
    shared.add_argument("--config", type=Path, default=SUPPRESS, help="shift config JSON")
    shared.add_argument("--home", default=SUPPRESS, help="existing muster directory")
    shift = commands.add_parser("shift", help="a Captain's commands on the direct path (runner token)")
    shift.add_argument("--config", type=Path, default=os.environ.get("PHALANX_SHIFT_CONFIG"),
                       help="shift config JSON (default: PHALANX_SHIFT_CONFIG)")
    shift.add_argument("--home", help="existing muster directory (otherwise PHALANX_HOME or user locator)")
    actions = shift.add_subparsers(dest="shift_command", required=True)
    show = actions.add_parser("prompt", parents=[shared],
                              help="print the Captain or keeper prompt, or the worker prompt template")
    show.add_argument("name", choices=["captain", "keeper", "worker"])
    actions.add_parser("charter", parents=[shared], help="validate the charter and show its revision")
    begin = actions.add_parser("start", parents=[shared], help="register this chat as a Captain")
    begin.add_argument("--session-ref", required=True, help="this chat's native session ID or link")
    begin.add_argument("--parent", help="the Captain that launched this one")
    beat = actions.add_parser("heartbeat", parents=[shared], help="re-read the charter and heartbeat")
    beat.add_argument("--captain", required=True)
    beat.add_argument("--wake", action="store_true", help="count this heartbeat as a wake")
    beat.add_argument("--state", choices=["active", "ending", "ended"])
    beat.add_argument("--focus", nargs="*", help="item IDs this wake acts on")
    beat.add_argument("--session", nargs=2, action="append", dest="sessions", metavar=("ID", "NAME"),
                      help="a visible native session named `phx ...`; repeat for each")
    beat.add_argument("--no-sessions", action="store_true", help="report that no such session is visible")
    beat.add_argument("--handoff-note")
    show_status = actions.add_parser("status", parents=[shared], help="flags, Captains and the charter's open items")
    show_status.add_argument("--captain", help="also read this Captain, including ended-writer cleanup")
    act = actions.add_parser("action", parents=[shared], help="record an action on the item's timeline")
    act.add_argument("--captain", required=True)
    act.add_argument("--item", required=True)
    act.add_argument("--action", required=True, help="what you did, for example `launched` or `replaced`")
    act.add_argument("--reason", required=True)
    end = actions.add_parser("handoff", parents=[shared],
                             help="start ending this shift and get the successor's name and kickoff")
    end.add_argument("--captain", required=True)
    end.add_argument("--note", required=True, help="what your successor needs to know")
    retire = actions.add_parser("retire-reconciler", parents=[shared],
                                help="forget a decommissioned reconciler's heartbeat")
    retire.add_argument("runner_id")
    check = actions.add_parser("archive-check", parents=[shared],
                               help="check local work before using runtime session tools to archive")
    check.add_argument("--session", required=True)
    check.add_argument("--by", required=True, help="reporting native session ID")
    location = check.add_mutually_exclusive_group(required=True)
    location.add_argument("--item")
    location.add_argument("--worktree", type=Path)
    location.add_argument("--no-worktree", action="store_true")
    archived = actions.add_parser("archive-result", parents=[shared],
                                  help="record a runtime archive result, without invoking the runtime")
    archived.add_argument("--session", required=True)
    archived.add_argument("--by", required=True, help="reporting native session ID")
    archived.add_argument("--outcome", required=True, choices=["archived", "refused", "blocked"])
    archived.add_argument("--reason", required=True)
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
        print(prompt(args.name))
        return
    home = None
    if args.config is None or getattr(args, "home", None) is not None:
        from .muster import direct_config
        home, args.config = direct_config("shift", getattr(args, "home", None))
    config = load_config(args.config, home=home)
    charter = load_charter(config)
    if args.shift_command == "charter":
        result = charter
    elif args.shift_command == "start":
        result = start(API(config), charter, args.session_ref, args.parent)
    elif args.shift_command == "heartbeat":
        if args.no_sessions and args.sessions:
            raise ShiftError("Pass --session or --no-sessions, not both")
        sessions = [] if args.no_sessions else args.sessions and [
            {"id": session_id, "name": name} for session_id, name in args.sessions]
        result = heartbeat(API(config), charter, args.captain, wake=args.wake, state=args.state,
                           focus=args.focus, sessions=sessions, handoff_note=args.handoff_note)
    elif args.shift_command == "status":
        result = status(API(config), charter, args.captain)
    elif args.shift_command == "action":
        result = API(config)(f"/v1/items/{_q(args.item)}/captain-actions", body={
            "captain_id": args.captain, "action": args.action, "reason": args.reason})
    elif args.shift_command == "handoff":
        command = config.get("shift_command") or f"python -m phalanx shift --config {Path(args.config).resolve()}"
        result = handoff(API(config), charter, args.captain, args.note, command)
    elif args.shift_command == "retire-reconciler":
        result = API(config)(f"/v1/reconcilers/{_q(args.runner_id)}", method="DELETE")
    elif args.shift_command == "archive-check":
        result = archive_check(API(config), config, charter, args.session, args.by, item_id=args.item,
                               worktree=args.worktree, no_worktree=args.no_worktree)
    elif args.shift_command == "archive-result":
        result = archive_result(API(config), args.session, args.outcome, args.reason, args.by)
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

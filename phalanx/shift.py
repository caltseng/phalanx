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
import re
import secrets
from string import Template
import tomllib
import unicodedata
from urllib.parse import quote, urlencode

from .api_client import APIClient
from .work_cli import CREDENTIAL
from .git import run_git
from .json_utils import print_json, write_json as _write_credential
from .workspaces import WorkspaceError, WorkspaceManager, _git, _status
from .display_text import reason_text, short_title
from .session_links import session_reference

CHARTER_LISTS = ("repositories", "allowed_actions", "escalate", "stop_when")
# Elapsed-time rotation. Actual degradation remains an independent reason to hand off.
SHIFT_DEFAULTS = {"max_hours": 24}
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
               if key not in config or (key != "repositories" and not config[key])]
    if missing:
        raise ShiftError(f"Shift config {path} needs {', '.join(missing)}")
    if not isinstance(config["repositories"], dict):
        raise ShiftError("Shift config repositories must map repository IDs to checkout settings")
    for repository, mapping in config["repositories"].items():
        if "checks_environment" in mapping:
            environment = mapping["checks_environment"]
            if not isinstance(environment, str) or not environment.strip():
                raise ShiftError(f"Repository {repository} checks_environment must be non-empty text")
    return config


def _git_tree(path):
    result = run_git(path, "rev-parse", "--is-inside-work-tree", check=False, error=ShiftError)
    return result.returncode == 0 and result.stdout.strip() == b"true"


def load_charter(config):
    """Read and validate the charter. Its revision is the SHA-256 of the file's bytes."""
    path = Path(config["charter"]).resolve()
    data = path.read_bytes()
    guarded = [Path(mapping["checkout"]).resolve() for mapping in config["repositories"].values()
               if mapping.get("checkout")]
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
    shift = charter.get("shift", {})
    if not isinstance(shift, dict) or set(shift) - set(SHIFT_DEFAULTS) or not all(
            type(value) is int and value > 0 for value in shift.values()):
        raise ShiftError("Charter [shift] takes a positive integer max_hours")
    return {**charter, "shift": {**SHIFT_DEFAULTS, **shift},
            "revision": hashlib.sha256(data).hexdigest(),
            "path": str(path)}


def _q(value):
    return quote(value, safe="")


def shift_bounds(captain, charter, now=None):
    age = (now or datetime.now(UTC)) - datetime.fromisoformat(captain["created_at"])
    bounds = charter["shift"]
    due = []
    if age.total_seconds() >= bounds["max_hours"] * 3600:
        due.append(f"shift age reached {bounds['max_hours']} h")
    compactions = captain.get("compaction_count")
    return {"age_minutes": int(age.total_seconds() // 60), "wakes": captain["wake_count"], **bounds,
            "compaction_count": compactions, "handoff_due": due}


def pickup_scope(api, charter, config=None):
    repositories = {repository: None for repository in charter["repositories"]}
    if config is not None:
        registered = {entry["id"]: entry["remote"] for entry in api("/v1/repositories")}
        for repository in repositories:
            mapping = config["repositories"].get(repository)
            if not mapping:
                repositories[repository] = f"Repository {repository} has no checkout mapping"
            elif not mapping.get("checkout") or not mapping.get("base_branch"):
                repositories[repository] = f"Repository {repository} mapping needs checkout and base_branch"
            elif repository not in registered:
                repositories[repository] = f"Repository {repository} is not registered"
            elif not config.get("session_workspace"):
                repositories[repository] = "Native session_workspace is not configured"
            else:
                try:
                    WorkspaceManager(Path(mapping["checkout"]), registered[repository],
                                     Path(config["worktrees"])).verify_checkout()
                except WorkspaceError as error:
                    repositories[repository] = f"Checkout blocked: {error}"
    return {"repositories": repositories}


def start(api, charter, session_ref, parent_id=None, *, runtime=None, config=None):
    session_ref = session_reference(session_ref, runtime)
    body = {"session_ref": session_ref, "charter_revision": charter["revision"],
            "pickup": pickup_scope(api, charter, config)}
    if parent_id:
        body["parent_id"] = parent_id
    # A lost reply replays the same registration instead of leaving a stale twin.
    key = "captain-" + hashlib.sha256(f"{session_ref}\0{parent_id or ''}".encode()).hexdigest()
    captain = api("/v1/captains", body=body, key=key)
    return {"captain": captain, "charter": charter, "shift": shift_bounds(captain, charter)}


def heartbeat(api, charter, captain_id, *, wake=False, state=None, focus=None, sessions=None,
              handoff_note=None, compaction_count=None, config=None, repository_blocks=(), repository_ready=()):
    """Post a heartbeat with the charter's current revision. Omitted fields keep their last value."""
    current = api(f"/v1/captains/{_q(captain_id)}")
    if sessions is None:
        sessions = [{key: value for key, value in session.items() if key != "first_seen"}
                    for session in current["sessions"]]
    scope = pickup_scope(api, charter, config)
    body = {
        "charter_revision": charter["revision"],
        "wake_count": current["wake_count"] + int(wake),
        "state": state or current["state"],
        "focus": current["focus"] if focus is None else focus,
        "sessions": sessions,
        "handoff_note": current["handoff_note"] if handoff_note is None else handoff_note,
        "pickup": {**scope, "repositories": dict(scope["repositories"])},
    }
    # Native trust observations persist until the runtime explicitly confirms readiness.
    for repository, reason in current["pickup"]["repositories"].items():
        if reason and reason.startswith("Runtime blocked:") and repository in body["pickup"]["repositories"]:
            body["pickup"]["repositories"][repository] = reason
    for repository in repository_ready:
        if repository not in body["pickup"]["repositories"]:
            raise ShiftError(f"Repository {repository} is outside the charter")
        body["pickup"]["repositories"][repository] = scope["repositories"][repository]
    for repository, reason in repository_blocks:
        if repository not in body["pickup"]["repositories"]:
            raise ShiftError(f"Repository {repository} is outside the charter")
        body["pickup"]["repositories"][repository] = f"Runtime blocked: {reason}"
    if compaction_count is not None:
        body["compaction_count"] = compaction_count
    captain = api(f"/v1/captains/{_q(captain_id)}", body=body, method="PUT")
    return {"captain": captain, "charter": charter, "shift": shift_bounds(captain, charter)}


def _summary(item):
    writer = item["writer"]
    if writer and writer["state"] == "ended":
        writer = None
    return {
        "item_id": item["item_id"], "execution_id": item["id"], "version": item["version"],
        "repository_id": item["repository_id"], "title": item["title"], "stage": item["stage"],
        "status": item["status"], "priority": item["priority"], "wait_reason": item["wait_reason"],
        "resume_trigger": item["resume_trigger"], "wait_age_seconds": item["wait_age_seconds"],
        "pickup": item["pickup"],
        "blocked_prerequisites": item["blocked_prerequisite_count"], "primary_pr_url": item["primary_pr_url"],
        "writer": writer and {key: writer[key] for key in ("id", "state", "captain_id", "session_id",
                                                             "nonce", "created_at", "confirmed_at", "abandoned_at",
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
    capacity = api("/v1/dispatch/settings")
    return {
        "issue_scout": api("/v1/issue-scout"),
        "flags": api("/v1/flags")["flags"],
        "captains": api("/v1/captains?limit=100")["captains"],
        "items": scoped,
        "excluded_items": [_summary(item) for item in items if item["repository_id"] not in scope],
        "pickup_candidates": [item["item_id"] for item in sorted(
                                  scoped, key=lambda item: ("urgent", "normal", "low").index(item["priority"]))
                              if item["pickup"] and item["pickup"]["eligible"]
                              and (not captain_id or captain_id in item["pickup"]["captain_ids"])],
        "active_writers": capacity["active_writers"],
        "max_writers": capacity["max_writers"],
        "building_writers": capacity["building_writers"],
        "max_building_writers": capacity["max_building_writers"],
        "delivering_writers": capacity["delivering_writers"],
        "out_of_scope_open_items": sum(item["repository_id"] not in scope for item in items),
        **({"captain": api(f"/v1/captains/{_q(captain_id)}")} if captain_id else {}),
    }


def credential_path(worktree):
    result = run_git(worktree, "rev-parse", "--git-path", CREDENTIAL, check=False, error=ShiftError)
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ShiftError(f"Cannot locate the writer credential in {worktree}: {detail}")
    return Path(worktree) / result.stdout.decode("utf-8", errors="surrogateescape").strip()


def _read_credential(path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def kickoff(item, worktree, nonce, work_command, base, checks_environment=None):
    criteria = item.get("acceptance_criteria") or []
    mode = item["execution"]["state"]["publication_mode"]
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
            f"(`gh pr ready`) and retarget it to `{base}` if needed. "
            "Publication does not require enabling auto-merge: submit the PR URL and retain "
            "this writer in delivery while gates remain outstanding. Before merging or enabling "
            "auto-merge, satisfy the Merge decision section above."
        )
    template = Template(resources.files("phalanx").joinpath("prompts", "worker.md").read_text(encoding="utf-8"))
    return template.substitute(
        item_id=item["id"], execution_id=item["execution"]["id"], repository=item["repository_id"],
        target=item["execution"]["state"]["target"], goal=item["goal"], worktree=worktree, nonce=nonce, work=work_command,
        base=base, publication=publication, publication_mode=mode,
        checks_environment=(f"`{checks_environment}`" if checks_environment is not None
                            else "Not configured. Do not guess an interpreter or command prefix."),
        criteria="\n".join(f"- {entry}" for entry in criteria) or "- None beyond the goal",
        context="\n".join("    " + line for line in
                          json.dumps(item.get("context_links") or [], ensure_ascii=True, indent=2).splitlines()),
    )


def writer_session_name(goal):
    """Task words first; the suffix identifies Phalanx sessions, never individual writers."""
    text = goal if isinstance(goal, str) else ""
    text = re.sub(r"\S+://\S+", "[redacted]", text)
    text = "".join(" " if unicodedata.category(char).startswith("C") else char for char in text)
    text = reason_text(text, max(len(text), 40))
    text = re.sub(r"(?i)^(?:(?:please|give|provide|newly|created|the|a|an)\s+)+", "", text)
    return short_title(text or "Phalanx task", limit=34) + " [phx]"


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
    execution = item["execution"]
    if replaces is None and execution["state"]["status"] == "queued":
        decision = api(f"/v1/operator/items/{_q(item_id)}")["item"]["pickup"]
        if decision and not decision["eligible"]:
            raise ShiftError(decision["reason"])
        if decision and captain_id not in decision["captain_ids"]:
            raise ShiftError("This Captain's scope or repository readiness excludes pickup")
    mapping = config["repositories"].get(repository)
    if not mapping:
        raise ShiftError(f"Repository {repository} has no checkout mapping")
    remote = next((entry["remote"] for entry in api("/v1/repositories") if entry["id"] == repository), None)
    if remote is None:
        raise ShiftError(f"Repository {repository} is not registered")
    manager = WorkspaceManager(Path(mapping["checkout"]), remote, Path(config["worktrees"]))
    base = item.get("target_branch") or mapping["base_branch"]
    workspace = manager.prepare_from_origin(item_id, branch=base)
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
        "session_name": writer_session_name(item.get("goal")),
        "session_identity": {"item_id": item_id, "writer_id": binding["writer"]["id"], "writer_nonce": nonce},
        "kickoff": kickoff(item, workspace.path, nonce, config.get("work_command", "python -m phalanx work"),
                           base, mapping.get("checks_environment")),
    }


def prompt(name):
    text = resources.files("phalanx").joinpath("prompts", f"{name}.md").read_text(encoding="utf-8")
    if name in {"captain", "keeper"}:
        text += "\n" + prompt("forwarding")
    return text


def wake(api, charter, captain_id, *, config):
    from .issue_scout_client import scout

    workflow = prompt("captain")
    instructions = prompt("wake")
    current = api(f"/v1/captains/{_q(captain_id)}")
    if "reinforcement" not in current:
        raise ShiftError("The API needs the Captain reinforcement deployment; no wake was recorded")
    result = heartbeat(api, charter, captain_id, wake=True, config=config)
    captain = result["captain"]
    result["shift"] = shift_bounds(captain, charter, datetime.fromisoformat(captain["heartbeat_at"]))
    result["instructions"] = instructions
    result["reinforcement"] = {**captain["reinforcement"]}
    if result["reinforcement"]["due"]:
        result["reinforcement"]["workflow"] = workflow
        result["reinforcement"]["acknowledgement_arguments"] = [
            "shift", "ack-reinforcement", "--captain", captain_id,
            "--interval", str(result["reinforcement"]["interval"])]
    result["issue_scout"] = scout(api, captain_id)
    return result


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
        "session_name_identity": "Launch nonce derived from the predecessor, not the new Captain's ID",
        "off_shift_instructions": prompt("forwarding"),
        "kickoff": (f"You succeed predecessor Phalanx Captain {captain_id}; that is not your Captain ID. "
                    f"In this chat, `phx shift` means `{command}`. "
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
                              help="print Captain, keeper, worker or retained-chat forwarding instructions")
    show.add_argument("name", choices=["captain", "keeper", "worker", "forwarding"])
    actions.add_parser("charter", parents=[shared], help="validate the charter and show its revision")
    wake_up = actions.add_parser("wake", parents=[shared],
                                 help="heartbeat and print a briefing, with canonical reinforcement when due")
    wake_up.add_argument("--captain", required=True)
    acknowledge = actions.add_parser("ack-reinforcement", parents=[shared],
                                      help="acknowledge the reinforcement interval actually read")
    acknowledge.add_argument("--captain", required=True)
    acknowledge.add_argument("--interval", required=True, type=int)
    begin = actions.add_parser("start", parents=[shared], help="register this chat as a Captain")
    begin.add_argument("--session-ref", required=True, help="this chat's native session ID or link")
    begin.add_argument("--parent", help="the Captain that launched this one")
    beat = actions.add_parser("heartbeat", parents=[shared], help="re-read the charter and heartbeat")
    beat.add_argument("--captain", required=True)
    beat.add_argument("--wake", action="store_true", help="count this heartbeat as a wake")
    beat.add_argument("--compaction-count", type=int,
                      help="native session's cumulative successful compactions; omit if unavailable")
    beat.add_argument("--state", choices=["active", "ending", "ended"])
    beat.add_argument("--focus", nargs="*", help="item IDs this wake acts on")
    beat.add_argument("--session", nargs=2, action="append", dest="sessions", metavar=("ID", "NAME"),
                      help="a visible Phalanx session; repeat for each")
    beat.add_argument("--session-writer", nargs=2, action="append", metavar=("ID", "NONCE"),
                      help="a reported session's full writer nonce, verified from its kickoff; repeat for each")
    beat.add_argument("--session-availability", nargs=2, action="append", metavar=("ID", "STATE"),
                      help="explicit native lookup result: present, missing or deleted; requires --session")
    beat.add_argument("--no-sessions", action="store_true", help="report that no such session is visible")
    beat.add_argument("--handoff-note")
    beat.add_argument("--repository-blocked", nargs=2, action="append", metavar=("REPOSITORY", "REASON"),
                      help="record a native trust/readiness blocker until explicitly cleared")
    beat.add_argument("--repository-ready", action="append", metavar="REPOSITORY",
                      help="clear a runtime blocker after native verification")
    show_status = actions.add_parser("status", parents=[shared], help="flags, Captains and the charter's open items")
    show_status.add_argument("--captain", help="also read this Captain, including ended-writer cleanup")
    actions.add_parser("current", parents=[shared],
                       help="resolve the unique live active Captain and native session binding")
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
    usage = actions.add_parser("usage", parents=[shared],
                               help="observe a bound writer's usage, including after delivery")
    for option in ("--captain", "--item", "--execution", "--writer"):
        usage.add_argument(option, required=True)
    usage.add_argument("--file", type=Path, required=True, help="allowlisted cumulative snapshot JSON")


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
        print_json(charter)
        return
    token = os.environ.get("PHALANX_RUNNER_TOKEN")
    if not token:
        raise ShiftError("Set PHALANX_RUNNER_TOKEN for Captain commands")
    api = APIClient(config["base_url"], token).call
    if args.shift_command == "wake":
        result = wake(api, charter, args.captain, config=config)
    elif args.shift_command == "ack-reinforcement":
        result = api(f"/v1/captains/{_q(args.captain)}/reinforcement",
                             method="PUT", body={"interval": args.interval})
    elif args.shift_command == "start":
        result = start(api, charter, args.session_ref, args.parent, runtime=config["runtime"], config=config)
    elif args.shift_command == "heartbeat":
        if args.no_sessions and (args.sessions or args.session_writer or args.session_availability):
            raise ShiftError("Pass --session or --no-sessions, not both")
        sessions = [] if args.no_sessions else args.sessions and [
            {"id": session_id, "name": name, "runtime": config["runtime"]} for session_id, name in args.sessions]
        by_id = {session["id"]: session for session in sessions or []}
        for session_id, nonce in args.session_writer or []:
            if session_id not in by_id:
                raise ShiftError("--session-writer requires a matching --session")
            by_id[session_id]["writer_nonce"] = nonce
        for session_id, availability in args.session_availability or []:
            if session_id not in by_id or availability not in ("present", "missing", "deleted"):
                raise ShiftError("--session-availability requires a matching --session and present, missing or deleted")
            by_id[session_id]["availability"] = availability
        result = heartbeat(api, charter, args.captain, wake=args.wake, state=args.state,
                           focus=args.focus, sessions=sessions, handoff_note=args.handoff_note,
                           compaction_count=args.compaction_count, config=config,
                           repository_blocks=args.repository_blocked or (),
                           repository_ready=args.repository_ready or ())
    elif args.shift_command == "status":
        result = {**status(api, charter, args.captain),
                  "dashboard_url": config["base_url"].rstrip("/") + "/operator/"}
    elif args.shift_command == "current":
        result = api("/v1/captains/current")
    elif args.shift_command == "action":
        result = api(f"/v1/items/{_q(args.item)}/captain-actions", body={
            "captain_id": args.captain, "action": args.action, "reason": args.reason})
    elif args.shift_command == "handoff":
        command = config.get("shift_command") or f"python -m phalanx shift --config {Path(args.config).resolve()}"
        result = handoff(api, charter, args.captain, args.note, command)
    elif args.shift_command == "retire-reconciler":
        result = api(f"/v1/reconcilers/{_q(args.runner_id)}", method="DELETE")
    elif args.shift_command == "archive-check":
        result = archive_check(api, config, charter, args.session, args.by, item_id=args.item,
                               worktree=args.worktree, no_worktree=args.no_worktree)
    elif args.shift_command == "archive-result":
        result = archive_result(api, args.session, args.outcome, args.reason, args.by)
    elif args.shift_command == "launch":
        result = launch(api, config, charter, args.captain, args.item, replaces=args.replaces)
    elif args.shift_command == "usage":
        from .usage_models import Snapshot
        snapshot = Snapshot.model_validate_json(args.file.read_text(encoding="utf-8"))
        route = (f"/v1/items/{_q(args.item)}/executions/{_q(args.execution)}"
                 f"/writers/{_q(args.writer)}/usage")
        result = api(route, body={
            "captain_id": args.captain, "snapshot": snapshot.model_dump(mode="json")})
    elif args.shift_command == "writer":
        body = {"captain_id": args.captain, "writer_id": args.writer}
        for field in ("session_id", "session_url", "state"):
            if getattr(args, field) is not None:
                body[field] = getattr(args, field)
        if args.abandon:
            body["abandon"] = True
        result = api(_writer_route(args), body=body, method="PATCH")
    else:
        result = api(_writer_route(args, "/withdraw"), body={
            "captain_id": args.captain, "writer_id": args.writer, "reason": args.reason,
            "stopped": args.stopped})
    print_json(result)

"""Explicit installation and foreground service entrypoints; never discover live processes."""

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys

from .api_client import validate_api_url
from .json_utils import unique_object
from .muster import read_environment, resolve_home
from .shift import SHIFT_DEFAULTS
from .workspaces import any_file_lock

DEFAULT_HOME = ".phalanx_muster"


def initialize(home=None, *, api_url=None, port=8000):
    """Create reviewable configuration only, in a previously nonexistent directory."""
    selected = home if home is not None else os.environ.get("PHALANX_HOME")
    root = Path(selected).expanduser() if selected is not None else Path.home() / DEFAULT_HOME
    if not root.is_absolute():
        raise ValueError("Installation --home must be absolute")
    root = root.resolve()
    if not 1 <= port <= 65535:
        raise ValueError("Server port must be between 1 and 65535")
    api_url = api_url if api_url is not None else f"http://127.0.0.1:{port}"
    validate_api_url(api_url)
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    files = {
        "deployment.json": {
            "environment_file": str(root / "private.env"),
            "shift_config": str(root / "shift.json"),
            "server": {"host": "127.0.0.1", "port": port},
            "reconciler_config": str(root / "reconciler.json"),
            "reconciler_state": str(root / "reconciler"),
        },
        "shift.json": {
            "base_url": api_url, "runtime": "SET_RUNTIME",
            "charter": str(root / "charter.toml"),
            "worktrees": str(root / "worktrees"),
            "session_workspace": "SET_NATIVE_WORKSPACE",
            "repositories": {},
        },
        "reconciler.json": {
            "base_url": api_url, "reconciler_id": "SET_STABLE_RECONCILER_ID", "repositories": {},
        },
    }
    for name, value in files.items():
        (root / name).write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    (root / "private.env.example").write_text(
        "# Copy to private.env with private OS permissions; supply credentials privately.\n"
        "# Never regenerate credentials for an existing deployment.\n"
        f"PHALANX_HOME={root}\nPHALANX_DB={root / 'db' / 'phalanx.sqlite3'}\n"
        f"PHALANX_API_URL={api_url}\nPHALANX_SHIFT_CONFIG={root / 'shift.json'}\n"
        "# PHALANX_OWNER_TOKEN=\n# PHALANX_RUNNER_TOKEN=\n# PHALANX_GITHUB_TOKEN=\n"
        "PHALANX_LOCAL_BOARD=0\n", encoding="utf-8")
    (root / "charter.toml").write_text(
        '# Normal owner intake in these repositories is eligible without listing item IDs.\n'
        '# Notes may order or constrain work; they are not an intake allow-list.\n'
        '# PR intake defaults to checked_in (merged on the owner\'s behalf).\n'
        '# Preserve explicit ready_for_check_in/report targets and draft ceilings.\n'
        'goal = "SET_GOAL"\nrepositories = []\n'
        'notes = """\n'
        'Core Captain responsibility: progress and outcome accountability.\n'
        'On every ordinary scheduled wake and material work event ask: Is work progressing as expected?\n'
        'What is stalled or at risk? What can I unblock within existing authority?\n'
        'Take the next useful authorized action and verify its effect against actual stage/progress,\n'
        'dependency, validation, CI/review, integration and merge evidence and stage expectations.\n'
        'Busy sessions, fully occupied capacity, recorded holds, PR publication and a polling heartbeat\n'
        'are not by themselves delivery progress. Protect healthy work and legitimate waits; no churn\n'
        'or arbitrary age SLA. Resolve routine questions/validation and stale holds/reconciliation.\n'
        'Refill genuinely available eligible Building and Total capacity with waiting-fix preference;\n'
        'retain the same writer and Building handoff discipline through review/CI/integration/check-in.\n'
        'Record unresolved blockers with concrete reason, responsible actor and next action or resume trigger.\n'
        'Escalate only missing authority/access, safety issues or owner decisions with an actionable question.\n'
        'Preserve current-execution targets, explicit report/ready/draft ceilings, trust, privacy/source\n'
        'permissions, both capacity limits, sole-writer ownership and creator-owned cleanup; no new access\n'
        'or bypass. For checked_in, keep auto-merge off until independent final exact-head approval and\n'
        'applicable required gates; verify required CI, actual-main integration where applicable and merge.\n'
        'Any qualified reviewer independent of the author may provide final exact-head review; reuse\n'
        'valid exact-head evidence, preserving genuine GitHub required reviewers and protections.\n'
        'No native session or person is a universal review dependency. Ordinary performance targets\n'
        'have a two-round optimization budget: retain measurements, remaining miss and concrete follow-up.\n'
        'Explicit safety/SLO/resource-contract gates stay hard; use supported versioned controls for\n'
        'owner-approved classification, never silently waive acceptance criteria or claim a missed metric met.\n'
        'On material native-session, stage/result, Building release, PR merge and release/deployment events,\n'
        'run bounded reconciliation/refill now, preserving atomic Building/Total and sole-writer fencing;\n'
        'no new scheduler, service, polling framework, quota or blanket serialization.\n'
        'Routine compatible reviewed/checks-green upgrades use standing owner deployment authority with\n'
        'fresh validated backup, complete migration rehearsal, exact scoped API service ownership,\n'
        'continuity checks and recoverable rollback per docs/deployment.md. Destructive/data-loss/schema-\n'
        'downgrade/new-service/credential-scope changes remain explicit owner decisions.\n'
        'Source merge is not a deployed result; record installed revision and retained-session receipts.\n'
        'Use existing five-minute reconciliation, 90-minute reinforcement and 24-hour handoff.\n'
        'Queued wake prompts do not refresh liveness during long turns; use existing non-wake\n'
        'heartbeats between bounded coordination batches before staleness, not another scheduler.\n'
        '"""\n'
        'allowed_actions = ["Deliver scoped work through its current target, including merging '
        'checked_in PRs under repository rules", "Execute routine compatible reviewed/checks-green '
        'upgrades under standing owner deployment authority and docs/deployment.md safeguards"]\n'
        'escalate = ["Missing authority or credentials", "Destructive/data-loss/schema-downgrade/'
        'new-service/credential-scope changes"]\nstop_when = ["Owner stops the muster"]\n'
        '[shift]\n' + "".join(f"{key} = {value}\n" for key, value in SHIFT_DEFAULTS.items()),
        encoding="utf-8")
    return root


def configuration(path):
    """Read config and environment into a child environment, leaving this process unchanged."""
    try:
        config = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=unique_object)
    except (ValueError, UnicodeError):
        raise ValueError("Invalid deployment configuration JSON") from None
    expected = {"environment_file", "server", "shift_config", "reconciler_config", "reconciler_state"}
    if not isinstance(config, dict) or set(config) != expected:
        raise ValueError("Deployment config needs environment_file, server, shift_config, "
                         "reconciler_config and reconciler_state")
    for name in expected - {"server"}:
        if not isinstance(config[name], str) or not Path(config[name]).is_absolute():
            raise ValueError(f"Deployment {name} must be an absolute path")
    server = config["server"]
    if (not isinstance(server, dict) or set(server) != {"host", "port"}
            or server["host"] not in ("127.0.0.1", "::1", "localhost")
            or type(server["port"]) is not int or not 1 <= server["port"] <= 65535):
        raise ValueError("Deployment server needs a loopback host and a port between 1 and 65535")
    env = {**read_environment(Path(config["environment_file"])), **os.environ}
    env.setdefault("PHALANX_SHIFT_CONFIG", config["shift_config"])
    return config, env


@contextmanager
def service_environment(env):
    previous = dict(os.environ)
    os.environ.clear()
    os.environ.update(env)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(previous)


def run_service(config, env, service, *, initialize_database=False, once=False, serve=None):
    if service == "reconciler":
        command = [sys.executable, "-P", "-m", "phalanx", "reconcile",
                   "--config", config["reconciler_config"], "--state", config["reconciler_state"]]
        if once:
            command.append("--once")
        return subprocess.call(command, env=env)
    database = env.get("PHALANX_DB", "")
    if not database or not Path(database).is_absolute():
        raise ValueError("Server requires an explicit absolute PHALANX_DB")
    path = Path(database).resolve()
    if not path.is_file() and not initialize_database:
        raise ValueError("Recorded database is missing; refuse to create a replacement. "
                         "For a new install only, pass --initialize-database")
    if path.exists() and initialize_database:
        raise ValueError("--initialize-database is only for a new database")
    if serve is None:
        raise ValueError("Server launch requires the server-host entrypoint")
    path.parent.mkdir(parents=True, exist_ok=True)
    with any_file_lock([path.with_name(path.name + ".service.lock")]) as locked:
        if locked is None:
            raise ValueError("A deployment server already owns this database")
        if path.exists() and initialize_database:
            raise ValueError("--initialize-database is only for a new database")
        if not initialize_database and not path.is_file():
            raise ValueError("Recorded database disappeared before startup; refusing to create a replacement")
        with service_environment(env):
            serve(host=config["server"]["host"], port=config["server"]["port"])
        return 0


def add_deploy_command(commands):
    parser = commands.add_parser("deploy", help="explicit setup and foreground service entrypoints")
    actions = parser.add_subparsers(dest="deploy_command", required=True)
    init = actions.add_parser("init", help="create templates only; never overwrite an existing home")
    init.add_argument("--home", type=Path, help="new absolute directory (default: ~/.phalanx_muster)")
    init.add_argument("--api-url", help="client API URL (default: loopback at --port)")
    init.add_argument("--port", type=int, default=8000)
    for name in ("check", "run", "cli"):
        action = actions.add_parser(name)
        action.add_argument("--config", type=Path, help="explicit deployment.json, including legacy layouts")
        action.add_argument("--home", help="existing home; otherwise PHALANX_HOME or locator")
        if name == "run":
            action.add_argument("service", choices=("server", "reconciler"))
            action.add_argument("--initialize-database", action="store_true")
            action.add_argument("--once", action="store_true")
        elif name == "cli":
            action.add_argument("arguments", nargs=argparse.REMAINDER)


def run_deploy(args, *, serve=None):
    if args.deploy_command == "init":
        print(f"Templates created in {initialize(args.home, api_url=args.api_url, port=args.port)}; "
              "configure credentials, mappings, runtime and charter before starting anything.")
        return 0
    if args.config is not None and args.home is not None:
        raise ValueError("Use either --config or --home")
    path = args.config if args.config is not None else resolve_home(args.home) / "deployment.json"
    config, env = configuration(path)
    if args.deploy_command == "check":
        database = env.get("PHALANX_DB", "")
        if not database or not Path(database).is_absolute() or not Path(database).is_file():
            raise ValueError("Recorded absolute PHALANX_DB must exist; check never initializes it")
        for name in ("shift_config", "reconciler_config"):
            if not Path(config[name]).is_file():
                raise ValueError(f"Missing deployment {name}")
        if not all(env.get(key) for key in
                   ("PHALANX_OWNER_TOKEN", "PHALANX_RUNNER_TOKEN", "PHALANX_GITHUB_TOKEN")):
            raise ValueError("Configure owner, runner and GitHub credentials privately")
        print("Deployment paths and credentials present; no services started or state changed.")
        return 0
    if args.deploy_command == "cli":
        arguments = args.arguments
        if arguments[:1] == ["--"]:
            arguments = arguments[1:]
        return subprocess.call([sys.executable, "-P", "-m", "phalanx", *arguments], env=env)
    if args.initialize_database and args.service != "server":
        raise ValueError("--initialize-database applies only to the server")
    if args.once and args.service != "reconciler":
        raise ValueError("--once applies only to the reconciler")
    return run_service(config, env, args.service,
                       initialize_database=args.initialize_database, once=args.once, serve=serve)

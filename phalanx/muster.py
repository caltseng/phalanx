"""Find this user's muster directory, start its missing services, and report its status.

A muster directory holds one runner's existing configuration and state:

    runner.json          runner config (runner_id, base_url, capacity, repositories, ...)
    runner/              private runner state: slot journals, relay queue, captain-loop.lock
    private/env.json     private environment: tokens and other settings, as {"NAME": "value"}
    db/phalanx.sqlite3   the API database, unless PHALANX_DB in private/env.json names another
    logs/                server.log and captain-loop.log written by `muster start`

Muster commands never create, migrate, move or delete that state.
"""

from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from time import monotonic, sleep, time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, build_opener

from .attended import NoRedirect
from .captain_relay import RELAY_STALL_SECONDS
from .workspaces import _lock, _unlock


LOOPBACK = {"localhost", "127.0.0.1", "::1"}
BLOCKERS = {"unmatched_attempts", "relay_stalled", "renewal_interval_too_long"}
RELAY_NOTE = ("muster start never restores the Captain relay. The relay serves only while the runtime's "
              "standing relay chat runs `relay next`; start or wake that chat (README: Standing relay serve)")
# Windows creation flags, spelled out so the branch is testable on every OS.
DETACHED_PROCESS, CREATE_NEW_PROCESS_GROUP, CREATE_BREAKAWAY_FROM_JOB = 0x8, 0x200, 0x01000000
_OPENER = build_opener(NoRedirect())


class MusterError(ValueError):
    pass


def locator_path() -> Path:
    return Path.home() / ".phalanx" / "muster.json"


def locate(home: Path | None = None, environ=os.environ, locator: Path | None = None):
    """Return (muster directory, source): --home, then PHALANX_HOME, then the per-user locator file."""
    if home is not None:
        return Path(home).expanduser().resolve(), "--home"
    if environ.get("PHALANX_HOME"):
        return _absolute(environ["PHALANX_HOME"], "PHALANX_HOME"), "PHALANX_HOME"
    locator = locator or locator_path()
    try:
        raw = locator.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        raise MusterError(f"No muster located: pass --home DIR, set PHALANX_HOME, or run "
                          f"`phalanx muster use DIR` once to write {locator}") from None
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if not isinstance(data, dict) or set(data) != {"home"} or not isinstance(data["home"], str):
        raise MusterError(f"Muster locator {locator} is invalid; it must be exactly "
                          '{"home": "<absolute muster directory>"}. Rewrite it with `phalanx muster use DIR`')
    return _absolute(data["home"], str(locator)), str(locator)


def _absolute(value: str, source: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise MusterError(f"The muster directory from {source} must be an absolute path, not {value!r}")
    return path


def _json_object(path: Path, label: str) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise MusterError(f"The muster's {label} {path} is missing") from None
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise MusterError(f"The muster's {label} {path} is not a JSON object")
    return data


@dataclass(frozen=True)
class Muster:
    home: Path
    source: str
    config: dict
    env: dict = field(repr=False)
    database: Path | None

    @property
    def config_path(self) -> Path:
        return self.home / "runner.json"

    @property
    def state(self) -> Path:
        return self.home / "runner"

    @property
    def logs(self) -> Path:
        return self.home / "logs"

    @property
    def base_url(self) -> str:
        return self.config["base_url"].rstrip("/")

    @property
    def local(self) -> bool:
        """Whether `muster start` may run the API here: plain HTTP on loopback."""
        url = urlsplit(self.base_url)
        return url.scheme == "http" and url.hostname in LOOPBACK


def load(home: Path, source: str) -> Muster:
    """Validate an existing muster directory without writing to it."""
    if not home.is_dir():
        raise MusterError(f"Muster directory {home} (from {source}) does not exist. Fix the location, or "
                          "point the locator at the muster that has its state with `phalanx muster use DIR`")
    home = home.resolve()
    config = _json_object(home / "runner.json", "runner config")
    if not isinstance(config.get("runner_id"), str) or not config["runner_id"] \
            or not isinstance(config.get("base_url"), str) or not urlsplit(config["base_url"]).hostname:
        raise MusterError(f"The muster's runner config {home / 'runner.json'} needs a nonempty runner_id "
                          "and an API base_url")
    if not (home / "runner").is_dir():
        raise MusterError(f"The muster's runner state directory {home / 'runner'} is missing. Muster commands "
                          "never create state; restore it, or point the locator at the muster that has it")
    env_path = home / "private" / "env.json"
    env = _json_object(env_path, "private environment")
    if not all(isinstance(value, str) for value in env.values()):
        raise MusterError(f"The muster's private environment {env_path} must map variable names to strings")
    muster = Muster(home, source, config, env, None)
    required = ["PHALANX_RUNNER_TOKEN"] + (["PHALANX_OWNER_TOKEN"] if muster.local else [])
    missing = [name for name in required if not env.get(name)]
    if missing:
        raise MusterError(f"The muster's private environment {env_path} has no {' or '.join(missing)}")
    url = env.get("PHALANX_API_URL")
    if url is not None and url.rstrip("/") != muster.base_url:
        raise MusterError(f"Ambiguous API: PHALANX_API_URL in {env_path} is {url}, but base_url in "
                          f"{muster.config_path} is {muster.base_url}. Make them match")
    if not muster.local:
        return muster
    database = Path(env.get("PHALANX_DB") or "db/phalanx.sqlite3").expanduser()
    database = (database if database.is_absolute() else home / database).resolve()
    if not database.is_file():
        raise MusterError(f"The muster's database {database} is missing. Muster commands never create a "
                          f"database; restore it, or set PHALANX_DB in {env_path} to the existing one")
    return Muster(home, source, config, env, database)


def store_identity(path: Path) -> str:
    """The server's `store` value for a database file (`GET /v1/runner/status`)."""
    return hashlib.sha256(os.path.normcase(str(Path(path).resolve())).encode("utf-8")).hexdigest()[:16]


def _get(url: str, token: str | None = None, timeout: float = 10):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    with _OPENER.open(Request(url, headers=headers), timeout=timeout) as response:
        return json.load(response)


def probe_api(muster: Muster) -> dict:
    """Classify whatever answers at base_url: absent, this muster's API (verified), or unverified."""
    base = muster.base_url
    try:
        body = _get(base + "/v1/runner/status", muster.env["PHALANX_RUNNER_TOKEN"])
    except HTTPError as error:
        error.close()
        return {"state": "unverified", "reason": f"{base} answered HTTP {error.code} to this muster's runner "
                "token, so it is not this muster's API"}
    except URLError as error:
        if isinstance(error.reason, ConnectionRefusedError):
            return {"state": "absent", "reason": f"Nothing is listening at {base}"}
        return {"state": "unverified", "reason": f"{base} did not answer as a Phalanx API: {error.reason}"}
    except (OSError, ValueError) as error:
        return {"state": "unverified", "reason": f"{base} did not answer as a Phalanx API: {type(error).__name__}"}
    if not isinstance(body, dict) or body.get("role") != "runner":
        return {"state": "unverified", "reason": f"{base} did not answer as a Phalanx API"}
    if muster.database is not None and "store" not in body:
        return {"state": "unverified", "reason": f"{base} accepts this muster's token but predates store "
                "identity, so it cannot be verified; restart it from this muster"}
    if muster.database is not None and body["store"] != store_identity(muster.database):
        return {"state": "unverified", "reason": f"{base} accepts this muster's token but does not report "
                f"serving {muster.database}; stop that server, or restart it from this muster"}
    try:
        _get(base + "/health")
        healthy = {"healthy": True}
    except (OSError, ValueError) as error:
        healthy = {"healthy": False, "health_reason": f"/health failed: {error}"}
    return {"state": "verified", "reason": f"{base} is this muster's API", **healthy}


def loop_running(state: Path) -> bool:
    """Whether a Captain loop holds this state directory's process lock."""
    path = state / "captain-loop.lock"
    if not path.is_file():
        return False
    with path.open("r+b") as handle:
        try:
            _lock(handle)
        except OSError:
            return True
        _unlock(handle)
    return False


def detach_options(os_name: str = os.name) -> dict:
    """Popen options that let a service outlive the shell or chat tool that started it."""
    if os_name == "nt":
        return {"creationflags": DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB}
    return {"start_new_session": True}


def spawn(argv, *, env, cwd, log):
    with log.open("ab") as output:
        options = {"stdin": subprocess.DEVNULL, "stdout": output, "stderr": subprocess.STDOUT,
                   "cwd": cwd, "env": env, "close_fds": True}
        detach = detach_options()
        try:
            return subprocess.Popen(argv, **options, **detach)
        except OSError:
            if "creationflags" not in detach:
                raise
            # A job object that forbids breakaway refuses the flag; start inside the job instead.
            return subprocess.Popen(argv, **options,
                                    creationflags=detach["creationflags"] & ~CREATE_BREAKAWAY_FROM_JOB)


def _environment(muster: Muster, *, drop=(), **extra) -> dict:
    """The muster's private settings over the shell's, with no stray PHALANX_ values from the shell."""
    env = {name: value for name, value in os.environ.items() if not name.upper().startswith("PHALANX_")}
    env.update(muster.env, PYTHONUNBUFFERED="1", **extra)
    for name in drop:
        env.pop(name, None)
    return env


def _wait_for(check, process, *, what: str, log: Path, timeout: float, poll: float):
    deadline = monotonic() + timeout
    while True:
        value = check()
        if value:
            return value
        if process.poll() is not None:
            raise MusterError(f"The {what} exited with code {process.returncode} before it was ready; see {log}")
        if monotonic() >= deadline:
            raise MusterError(f"The {what} (pid {process.pid}) was not ready within {timeout:g}s; see {log}. "
                              "Run `phalanx muster status` to check whether it came up")
        sleep(poll)


@contextmanager
def _start_lock(state: Path):
    with (state / "muster-start.lock").open("a+b") as handle:
        try:
            _lock(handle)
        except OSError:
            raise MusterError("Another `phalanx muster start` is running for this muster; let it finish, "
                              "then run `phalanx muster status`") from None
        yield


def _log(path: Path, message: str):
    with path.open("a", encoding="utf-8") as output:
        output.write(f"{datetime.now(timezone.utc).isoformat()} {message}\n")


def start(muster: Muster, *, launch=spawn, timeout: float = 120, poll: float = 0.5) -> dict:
    """Start this muster's API and Captain loop only where absent; repeating it changes nothing."""
    result = {"home": str(muster.home), "located_by": muster.source}
    muster.logs.mkdir(exist_ok=True)
    with _start_lock(muster.state):
        api = probe_api(muster)
        if api["state"] == "unverified":
            raise MusterError(f"Not starting anything: {api['reason']}")
        if api["state"] == "verified":
            result["api"] = {"status": "already_running", "url": muster.base_url}
        elif not muster.local:
            raise MusterError(f"The API at {muster.base_url} is unreachable. muster start runs only a loopback "
                              "http API; start the hosted API, then retry")
        else:
            url = urlsplit(muster.base_url)
            log = muster.logs / "server.log"
            _log(log, "muster start: starting the API")
            process = launch(
                [sys.executable, "-P", "-m", "uvicorn", "phalanx.api:app",
                 "--app-dir", str(Path(__file__).resolve().parents[1]),
                 "--host", url.hostname, "--port", str(url.port or 80)],
                env=_environment(muster, drop=("PHALANX_GITHUB_TOKEN",), PHALANX_DB=str(muster.database)),
                cwd=muster.home, log=log)
            api = _wait_for(lambda: (probe := probe_api(muster))["state"] != "absent" and probe,
                            process, what="API", log=log, timeout=timeout, poll=poll)
            if api["state"] != "verified":
                raise MusterError(f"The API started but is not verified: {api['reason']}; see {log}")
            result["api"] = {"status": "started", "url": muster.base_url, "pid": process.pid}
        if loop_running(muster.state):
            result["captain_loop"] = {"status": "already_running"}
        else:
            log = muster.logs / "captain-loop.log"
            _log(log, "muster start: starting the Captain loop")
            process = launch(
                [sys.executable, "-P", "-m", "phalanx", "captain",
                 "--config", str(muster.config_path), "--state", str(muster.state)],
                env=_environment(muster, drop=("PHALANX_OWNER_TOKEN",)), cwd=muster.home, log=log)
            _wait_for(lambda: loop_running(muster.state), process, what="Captain loop", log=log,
                      timeout=timeout, poll=poll)
            result["captain_loop"] = {"status": "started", "pid": process.pid}
    result["relay"] = RELAY_NOTE
    return result


def _read_only(path: Path):
    return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=30)


def _has_table(db, name: str) -> bool:
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _at(seconds: float | None) -> str | None:
    return None if seconds is None else datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def relay_status(state: Path, now: float | None = None) -> dict:
    """Relay serving as observed from the relay queue, never inferred from the API or loop."""
    path = state / "captain-relay.sqlite3"
    seen = since = None
    if path.is_file():
        try:
            with closing(_read_only(path)) as db:
                if _has_table(db, "server") and _has_table(db, "request"):
                    row = db.execute("SELECT value FROM server WHERE key='seen'").fetchone()
                    seen = row[0] if row else None
                    # The same clock as the loop's stall check: the first request since the last poll.
                    since = db.execute("SELECT MIN(created_at) FROM request WHERE created_at > ?",
                                       (seen or 0,)).fetchone()[0]
        except sqlite3.Error as error:
            return {"state": "unreadable", "reason": f"Relay queue {path}: {error}"}
    now = time() if now is None else now
    result = {"last_polled_at": _at(seen), "waiting_since": _at(since)}
    if since is not None and now - since >= RELAY_STALL_SECONDS:
        return {"state": "stalled", **result,
                "reason": "No relay server has taken a request since it arrived; start or wake the standing "
                          "relay chat"}
    if seen is not None and now - seen < RELAY_STALL_SECONDS:
        return {"state": "serving", **result, "reason": "A relay server polled or answered recently"}
    return {"state": "not_observed", **result,
            "reason": f"No relay poll in the last {RELAY_STALL_SECONDS} s and nothing has waited that long; "
                      "serving needs the runtime's standing relay chat"}


def loop_statuses(state: Path) -> list[dict]:
    """The Captain loop's last recorded statuses, as `attended status --all` shows them."""
    path = state / "attended.sqlite3"
    if not path.is_file():
        return []
    try:
        with closing(_read_only(path)) as db:
            if not _has_table(db, "captain"):
                return []
            return [json.loads(value) for (value,) in db.execute(
                "SELECT value FROM captain WHERE key LIKE 'status-%' ORDER BY key")]
    except sqlite3.Error as error:
        return [{"slot": "journal", "status": "journal_unreadable", "reason": f"{path}: {error}"}]


def status(muster: Muster, *, now: float | None = None) -> dict:
    api = probe_api(muster)
    statuses = loop_statuses(muster.state)
    local = muster.config.get("capacity", 1)
    report = {
        "home": str(muster.home), "located_by": muster.source,
        "paths": {"config": str(muster.config_path), "state": str(muster.state),
                  "database": None if muster.database is None else str(muster.database),
                  "logs": str(muster.logs)},
        "api": {"url": muster.base_url, **api},
        "captain_loop": {"running": loop_running(muster.state)},
        "relay": {**relay_status(muster.state, now), "note": RELAY_NOTE},
        "capacity": {"local": local},
        "dispatch": {},
        "blockers": [event for event in statuses
                     if event.get("status") in BLOCKERS or str(event.get("status", "")).endswith("_blocked")
                     or event.get("status") == "journal_unreadable"],
        "loop_status": statuses,
    }
    if api["state"] != "verified":
        report["capacity"]["reason"] = report["dispatch"]["reason"] = "API not verified"
        return report
    try:
        view = _get(f"{muster.base_url}/v1/dispatch/runners/{quote(muster.config['runner_id'], safe='')}",
                    muster.env["PHALANX_RUNNER_TOKEN"])
        report["capacity"].update(server=view["capacity"], reserved=view["reserved"],
                                  effective=min(local, view["capacity"]) if isinstance(local, int) else None)
        report["dispatch"] = {"paused": view["paused"], "aging_seconds": view["aging_seconds"]}
    except (OSError, ValueError, KeyError, TypeError) as error:
        report["capacity"]["reason"] = report["dispatch"]["reason"] = f"Dispatch view unavailable: {error}"
    return report


def use(directory: Path, locator: Path | None = None) -> dict:
    """Validate a muster directory and record it as this user's muster."""
    muster = load(Path(directory).expanduser().resolve(), "muster use")
    locator = locator or locator_path()
    locator.parent.mkdir(parents=True, exist_ok=True)
    temporary = locator.with_name(locator.name + ".tmp")
    temporary.write_text(json.dumps({"home": str(muster.home)}, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, locator)
    return {"locator": str(locator), "home": str(muster.home)}


def add_muster_command(commands):
    parser = commands.add_parser("muster", help="find, start and inspect this user's muster")
    actions = parser.add_subparsers(dest="muster_command", required=True)
    for name, text in (("start", "start the muster's API and Captain loop where they are not running"),
                       ("status", "report the muster's API, Captain loop, relay, capacity and blockers")):
        action = actions.add_parser(name, help=text)
        action.add_argument("--home", type=Path,
                            help="muster directory; overrides PHALANX_HOME and the per-user locator file")
    actions.add_parser("use", help="record a muster directory in the per-user locator file").add_argument(
        "directory", type=Path)


def run_muster(args):
    if args.muster_command == "use":
        result = use(args.directory)
    else:
        muster = load(*locate(args.home))
        result = start(muster) if args.muster_command == "start" else status(muster)
    print(json.dumps(result, indent=2))

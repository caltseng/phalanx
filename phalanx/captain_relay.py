"""Production bridge: a Captain chat relays queued requests to its worker runtime.

The Captain chat holds the runtime's session tools. The loop queues each
bridge request in the runner state directory and waits. The Captain chat takes
one request at a time with `relay next`, runs it with its runtime adapter, and records
the reply with `relay respond`. Requests use the same JSON contract as the
`--adapter-command` subprocess bridge.
"""

from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from time import sleep, time
from uuid import uuid4

from .captain_adapter import BRIDGE_CONTRACT_VERSION, LaunchNotDelivered, RequestRetracted
from .handoff import _unique_object
from .runner_errors import RunnerError


RESOLVE_CACHE_SECONDS = 600
# An unverified project, such as an untrusted repository, costs the Captain one answer a minute.
RESOLVE_FAILURE_CACHE_SECONDS = 60
RETAIN_SECONDS = 86400


@contextmanager
def _queue(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(root / "captain-relay.sqlite3", timeout=30)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("""CREATE TABLE IF NOT EXISTS request (
            id TEXT PRIMARY KEY, key TEXT NOT NULL, operation TEXT NOT NULL, body TEXT NOT NULL,
            status TEXT NOT NULL, response TEXT, error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL)""")
        # When a relay server last polled or answered, so the loop can tell a stalled relay from a quiet one.
        db.execute("CREATE TABLE IF NOT EXISTS server (key TEXT PRIMARY KEY, value REAL NOT NULL)")
        yield db


def _seen(db) -> None:
    db.execute("INSERT OR REPLACE INTO server VALUES ('seen', ?)", (time(),))


class RelayTransport:
    """Transport for BridgeAdapter backed by the Captain chat relay queue."""

    def __init__(self, root: Path, *, timeout: float = 300, poll: float = 0.2):
        self.root, self.timeout, self.poll = Path(root).resolve(), timeout, poll
        self.resolved: dict[str, tuple[float, dict]] = {}
        self.unresolved: dict[str, tuple[float, str]] = {}
        # Waiters from a previous loop are gone; never let their requests launch late.
        with _queue(self.root) as db:
            db.execute("UPDATE request SET status='expired', updated_at=? WHERE status='pending' AND updated_at < ?",
                       (time(), time() - timeout))

    def __call__(self, operation: str, arguments: dict) -> dict:
        body = json.dumps({"contract_version": BRIDGE_CONTRACT_VERSION, "operation": operation, "arguments": arguments},
                          sort_keys=True)
        key = hashlib.sha256(body.encode("utf-8")).hexdigest()
        cached = self.resolved.get(key)
        if cached and time() - cached[0] < RESOLVE_CACHE_SECONDS:
            return cached[1]
        failed = self.unresolved.get(key)
        if failed and time() - failed[0] < RESOLVE_FAILURE_CACHE_SECONDS:
            raise RunnerError(failed[1])
        with _queue(self.root) as db:
            # Stamp inside the lock, so no request is created before a poll that could not see it.
            now = time()
            db.execute("DELETE FROM request WHERE status != 'pending' AND updated_at < ?", (now - RETAIN_SECONDS,))
            # Identical requests waiting for the Captain share one action.
            row = db.execute("SELECT id FROM request WHERE key=? AND status='pending'", (key,)).fetchone()
            request_id = row[0] if row else str(uuid4())
            if not row:
                db.execute("INSERT INTO request VALUES (?, ?, ?, ?, 'pending', NULL, NULL, ?, ?)",
                           (request_id, key, operation, body, now, now))
        while True:
            with _queue(self.root) as db:
                status, response, error, updated = db.execute(
                    "SELECT status, response, error, updated_at FROM request WHERE id=?", (request_id,),
                ).fetchone()
                if status in {"pending", "taken"} and time() >= updated + self.timeout:
                    # Settle it so an identical retry queues afresh for a live Captain.
                    status = "expired" if status == "pending" else "lost"
                    db.execute("UPDATE request SET status=?, updated_at=? WHERE id=?", (status, time(), request_id))
            if status == "answered":
                if error is not None:
                    message = f"Captain relay {operation} failed: {error}"
                    if operation == "resolve_project":
                        self.unresolved[key] = (time(), message)
                    raise RunnerError(message)
                reply = json.loads(response)
                if operation == "resolve_project":
                    self.resolved[key] = (time(), reply)
                return reply
            if status == "expired":
                raise LaunchNotDelivered(f"Captain relay never took {operation}; start or resume the Captain chat relay")
            if status == "retracted":
                raise RequestRetracted(f"Captain relay {operation} was retracted before the Captain took it")
            if status == "lost":
                # The Captain may have run the tool. The journal never relaunches; discovery reconciles it.
                raise RunnerError(f"Captain relay took {operation} but never answered; "
                                  "a launch is reconciled by native discovery")
            sleep(self.poll)

    def stall(self, threshold: float) -> str | None:
        """Name a stall when no relay server has polled since a request arrived more than threshold ago."""
        with _queue(self.root) as db:
            row = db.execute("SELECT value FROM server WHERE key='seen'").fetchone()
            # Requeued and retracted requests count too, so expiry never resets the stall clock.
            since = db.execute("SELECT MIN(created_at) FROM request WHERE created_at > ?",
                               (row[0] if row else 0,)).fetchone()[0]
        if since is None or time() - since < threshold:
            return None
        at = datetime.fromtimestamp(since, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return (f"Captain relay has taken no request since {at}; start or wake the relay server. "
                "Waiting work resumes by itself once it takes requests")

    def retract_observations(self, observing: set[str]) -> None:
        """Withdraw untaken observations that name any marker whose slot no longer observes."""
        with _queue(self.root) as db:
            rows = db.execute("SELECT id, body FROM request WHERE status='pending' AND operation='observe'").fetchall()
            stale = [request_id for request_id, body in rows
                     if not {ref["marker"] for ref in json.loads(body)["arguments"]["references"]} <= observing]
            db.executemany("UPDATE request SET status='retracted', updated_at=? WHERE id=?",
                           [(time(), request_id) for request_id in stale])


def read_request(root: Path, request_id: str) -> dict:
    """Return a request taken with `relay next`, so a helper can build its reply."""
    with _queue(Path(root).resolve()) as db:
        row = db.execute("SELECT status, body FROM request WHERE id=?", (request_id,)).fetchone()
    if row is None or row[0] != "taken":
        raise RunnerError("Read only a request taken with `relay next`; it is unknown or settled")
    return {"id": request_id, **json.loads(row[1])}


def take_next(root: Path, wait: float = 30, poll: float = 0.2) -> dict:
    """Hand out the oldest pending request exactly once, even across Captain restarts."""
    deadline = time() + wait
    while True:
        with _queue(Path(root).resolve()) as db:
            _seen(db)
            row = db.execute("SELECT id, body FROM request WHERE status='pending' "
                             "ORDER BY created_at LIMIT 1").fetchone()
            if row:
                db.execute("UPDATE request SET status='taken', updated_at=? WHERE id=?", (time(), row[0]))
                return {"id": row[0], **json.loads(row[1])}
        if time() >= deadline:
            return {"status": "idle"}
        sleep(poll)


def respond(root: Path, request_id: str, *, response: dict | None = None, error: str | None = None) -> dict:
    if (response is None) == (error is None):
        raise RunnerError("Supply exactly one JSON response or error")
    if response is not None and not isinstance(response, dict):
        raise RunnerError("A relay response must be one JSON object")
    with _queue(Path(root).resolve()) as db:
        row = db.execute("SELECT status FROM request WHERE id=?", (request_id,)).fetchone()
        if row is None or row[0] != "taken":
            raise RunnerError("Respond only to a request taken with `relay next`; it is unknown or settled")
        _seen(db)
        db.execute("UPDATE request SET status='answered', response=?, error=?, updated_at=? WHERE id=?",
                   (None if response is None else json.dumps(response), error, time(), request_id))
    return {"status": "recorded", "id": request_id}


def add_relay_command(commands):
    parser = commands.add_parser("relay", help="run Captain chat relay requests for the captain loop")
    parser.add_argument("--state", required=True, type=Path)
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("next", help="take the next request").add_argument("--wait", type=float, default=30)
    answer = actions.add_parser("respond", help="record the runtime's reply to a taken request")
    answer.add_argument("id")
    source = answer.add_mutually_exclusive_group()
    source.add_argument("--file", type=Path, help="JSON reply file; defaults to stdin")
    source.add_argument("--error", help="the runtime action failed with this message")
    observe = actions.add_parser("observe", help="write an observe reply from worker session transcripts")
    observe.add_argument("id")
    observe.add_argument("--runtime", required=True, help="transcript reader, such as copilot")
    observe.add_argument("--host", required=True, help="host ID the listed sessions run on")
    observe.add_argument("--session", nargs=3, action="append", default=[],
                         metavar=("SESSION_ID", "STATE", "PROJECT_ID"),
                         help="a listed worker session and its active, stopped or unknown state")
    observe.add_argument("--complete", action="store_true", help="every worker session on the host was listed")
    observe.add_argument("--out", type=Path, help="reply file for `relay respond --file`; defaults to stdout")


def _observe_reply(args) -> dict:
    from .observe import build_reply, reader

    read = reader(args.runtime)
    sessions = [{"thread_id": session_id, "state": state, "project_id": project_id, **read(session_id)}
                for session_id, state, project_id in args.session]
    return build_reply(read_request(args.state, args.id), sessions, host_id=args.host, complete=args.complete)


def run_relay(args):
    if args.action == "observe":
        reply = json.dumps(_observe_reply(args), indent=2, ensure_ascii=False)
        if args.out is None:
            print(reply)
            return
        args.out.write_text(reply + "\n", encoding="utf-8")
        result = {"status": "written", "id": args.id, "file": str(args.out)}
    elif args.action == "next":
        result = take_next(args.state, args.wait)
    elif args.error is not None:
        result = respond(args.state, args.id, error=args.error)
    else:
        raw = args.file.read_text(encoding="utf-8-sig") if args.file else sys.stdin.read()
        try:
            response = json.loads(raw, object_pairs_hook=_unique_object)
        except ValueError as error:
            raise RunnerError("The relay response is not valid JSON") from error
        result = respond(args.state, args.id, response=response)
    # ASCII escapes survive a Windows pipe in any code page.
    print(json.dumps(result))

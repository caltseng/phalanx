"""Disposable adapter around immutable Git source, never the installed/live API."""

import argparse
from datetime import UTC, datetime
from hashlib import sha256
import itertools
import inspect
import json
import os
from pathlib import Path
import socket
import sys
from threading import Thread
from uuid import UUID


STAMP = datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=UTC)
OWNER = "conformance-owner-only-0000000000000000"
RUNNER = "conformance-runner-only-0000000000000000"
WRITER = "conformance-writer-only-0000000000000000"


def load(source, id_start=0):
    sys.path.insert(0, str(source))
    from phalanx import intake

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return STAMP if tz else STAMP.replace(tzinfo=None)

    intake.datetime = Clock
    ids = itertools.count(id_start + 1)
    intake.uuid4 = lambda: UUID(int=next(ids))
    return intake


def seed(source, database):
    intake = load(source)
    from phalanx.intake_models import Intake
    from phalanx.store import initialize, write_transaction

    initialize(database)
    with write_transaction(database) as db:
        intake.register(db, "fixture", "https://github.com/example/fixture.git")
        intake.uuid4 = iter((UUID(int=0x1000), UUID(int=0x1001))).__next__
        item = intake.create(db, Intake(repository_id="fixture", goal="Retained Unicode: \u96ea",
                                       target="report"))
        now = STAMP.isoformat(timespec="microseconds")
        db.execute("""INSERT INTO captains
            (id, session_ref, charter_revision, state, created_at, heartbeat_at)
            VALUES ('fixture-captain', 'synthetic-session', 'synthetic-charter', 'active', ?, ?)""",
                   (now, now))
        db.execute("""INSERT INTO writers
            (id, execution_id, item_id, captain_id, runtime, nonce, token_digest, state, created_at)
            VALUES ('fixture-writer', ?, ?, 'fixture-captain', 'synthetic-runtime',
                    'synthetic-nonce', ?, 'idle', ?)""",
                   (item["execution"]["id"], item["id"], sha256(WRITER.encode()).hexdigest(), now))
        # Saved nondefault operator settings and opaque rows must survive.
        db.execute("UPDATE dispatch_settings SET paused=1, max_writers=3, max_building_writers=2")
        db.execute("INSERT INTO request_receipts VALUES ('other', 'retained', ?, ?)",
                   ("d" * 64, '{"opaque":"\\u96ea","zero":0,"null":null}'))
        ids = itertools.count(0x1100)
        intake.uuid4 = lambda: UUID(int=next(ids))
        payload = Intake(repository_id="fixture", goal="Historical receipt \u96ea")
        material = payload.model_dump(mode="json")
        for key, compatible in (("old-null", True), ("current-model", False)):
            result = intake.create(db, payload)
            fingerprint = dict(material, runner_affinity=None) if compatible else material
            db.execute("INSERT INTO request_receipts VALUES ('intake', ?, ?, ?)",
                       (key, intake.digest(intake.encode(fingerprint)), intake.encode(result)))
            db.execute("UPDATE work_items SET request_json=json_set(request_json,'$.goal','Steered after receipt') WHERE id=?",
                       (result["id"],))
        db.execute("INSERT INTO request_receipts VALUES ('intake', 'unproved', ?, '{}')",
                   ("0" * 64,))


def serve(source, database, id_start, captain_clock=False):
    intake = load(source, id_start)
    if captain_clock:
        from phalanx import captains
        captains.datetime = intake.datetime
        captains.uuid4 = intake.uuid4
    for key in list(os.environ):
        if key.startswith("PHALANX_"):
            del os.environ[key]
    os.environ.update(PHALANX_DB=str(database), PHALANX_OWNER_TOKEN=OWNER,
                      PHALANX_RUNNER_TOKEN=RUNNER, PHALANX_LOCAL_BOARD="0",
                      PHALANX_HOME=str(database.parent))
    os.environ["USERPROFILE"] = os.environ["HOME"] = str(database.parent)
    from phalanx.api import create_app
    from phalanx.workspaces import _lock
    import uvicorn

    with Path(str(database.resolve()) + ".service.lock").open("a+b") as handle:
        _lock(handle)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        print(f"http://127.0.0.1:{listener.getsockname()[1]}", flush=True)
        server = uvicorn.Server(uvicorn.Config(create_app(), log_level="error", access_log=False,
                                              proxy_headers=False))
        def stop_on_input():
            sys.stdin.readline()
            server.should_exit = True
        Thread(target=stop_on_input, daemon=True).start()
        server.run(sockets=[listener])


def client(source, url, output):
    load(source)
    from phalanx import api_client
    APIClient, APIError = api_client.APIClient, api_client.APIError

    owner = APIClient(url, OWNER)
    current = "key" in inspect.signature(APIClient.call).parameters
    def call(route, body=None, key=None):
        if current:
            return owner.call(route, body, key=key)
        if key is not None:
            return api_client.request(url, OWNER, route, body=body, key=key)
        return owner.call(route.removeprefix("/v1"), body)

    assert call("/v1/owner/status") == {"role": "owner"}
    runner = APIClient(url, RUNNER)
    assert runner.call("/v1/runner/status" if current else "/runner/status") == {"role": "runner"}
    request = {"repository_id": "fixture", "goal": "Unchanged Python client \u96ea"}
    first = call("/v1/items", request, key="python-client")
    assert call("/v1/items", request, key="python-client") == first
    assert call("/v1/items/" + first["id"]) == first
    try:
        call("/v1/owner/status/")
    except APIError as error:
        assert "307" in str(error)
    else:
        raise AssertionError("Unchanged client followed a redirect")
    output.write_text(json.dumps(first, sort_keys=True), encoding="utf-8")


def lock_probe(source, database, hold):
    load(source)
    from phalanx.workspaces import _lock
    with Path(str(database.resolve()) + ".service.lock").open("a+b") as handle:
        try:
            _lock(handle)
        except OSError:
            print("excluded", flush=True)
            return
        print("acquired", flush=True)
        if hold:
            sys.stdin.readline()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("initialize", "seed", "serve", "client", "lock", "hold-lock"))
    parser.add_argument("source", type=Path)
    parser.add_argument("target")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--id-start", type=int, default=0)
    parser.add_argument("--captain-clock", action="store_true")
    args = parser.parse_args()
    if args.action == "initialize":
        load(args.source)
        from phalanx.store import initialize
        initialize(Path(args.target))
    elif args.action == "seed":
        seed(args.source, Path(args.target))
    elif args.action == "serve":
        serve(args.source, Path(args.target), args.id_start, args.captain_clock)
    elif args.action == "client":
        client(args.source, args.target, args.output)
    else:
        lock_probe(args.source, Path(args.target), args.action == "hold-lock")


if __name__ == "__main__":
    main()

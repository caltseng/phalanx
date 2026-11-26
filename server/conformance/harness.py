"""Real-loopback differential tests on new synthetic, independently owned files."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
import hashlib
import http.client
import importlib.metadata
import io
import json
import os
from pathlib import Path
import platform
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from urllib.parse import urlsplit
import zipfile


ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
PINS = {
    "P0": "9af29396b37f86816fd274e01632164a47718a57",
    "D0": "ce0a32cd7c83c77d50d19b7a4601cd48cdacf8ae",
    "retained": "7b86cc994916dc8c393e6217303374b1a5c2c9d0",
}
RUNTIME = {"fastapi": "0.165.0", "pydantic": "2.14.4", "starlette": "1.10.0",
           "uvicorn": "0.54.0", "anyio": "4.16.0"}
OWNER = "conformance-owner-only-0000000000000000"
RUNNER = "conformance-runner-only-0000000000000000"
WRITER = "conformance-writer-only-0000000000000000"
SEED = f"{0x1000:032x}"
SEED_EXECUTION = f"{0x1001:032x}"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def run(*args, **kwargs):
    return subprocess.run(args, cwd=ROOT, check=True, timeout=120, **kwargs)


def invoke(action, source, target, *args, **kwargs):
    return run(sys.executable, "-P", str(HERE / "oracle.py"), action, str(source),
               str(target), *map(str, args), **kwargs)


def export(label, root):
    archive = root / f"{label}.zip"
    run("git", "-c", "core.autocrlf=false", "--no-pager", "archive", PINS[label], "--format=zip",
        "-o", str(archive), "phalanx", "requirements.lock")
    destination = root / label
    with zipfile.ZipFile(archive) as source:
        source.extractall(destination)
    manifest = json.loads((HERE / "manifest.json").read_text(encoding="utf-8"))
    for name, expected in manifest["files"][label].items():
        if name.startswith("phalanx/") or name == "requirements.lock":
            if digest((destination / name).read_bytes()) != expected:
                raise AssertionError(f"Immutable exported oracle bytes changed: {label}/{name}")
    return destination


def source_manifest():
    result = {"pins": PINS, "runtime": RUNTIME, "python": "3.14.10", "sqlite": "3.53.1",
              "files": {}, "safety_modules": ["test_intake", "test_contention", "test_store",
                                            "test_dispatch", "test_writers", "test_transitions",
                                            "test_bootstrap"]}
    for label, revision in PINS.items():
        data = subprocess.check_output(
            ["git", "-c", "core.autocrlf=false", "--no-pager", "archive", revision, "--format=zip",
             "phalanx", "requirements.lock", "tests"], cwd=ROOT, timeout=60)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            result["files"][label] = {
                name: digest(archive.read(name)) for name in archive.namelist()
                if name.endswith(".py") or name == "requirements.lock"
            }
    return result


def verify_manifest():
    frozen = json.loads((HERE / "manifest.json").read_text(encoding="utf-8"))
    if frozen != source_manifest():
        raise AssertionError("Immutable source/test manifest changed")
    if platform.python_version() != frozen["python"] or sqlite3.sqlite_version != frozen["sqlite"]:
        raise AssertionError("Use the pinned isolated Python/SQLite checks environment")
    if {name: importlib.metadata.version(name) for name in RUNTIME} != RUNTIME:
        raise AssertionError("Pinned Python oracle dependencies differ")


def vector_manifest():
    paths = sorted((ROOT / "server").rglob("*.go")) + sorted(HERE.glob("*.py"))
    paths += [ROOT / "server" / "go.mod", ROOT / "server" / "go.sum",
              HERE / "golden-fresh.json", HERE / "golden-retained.json"]
    tests = []
    for path in paths:
        if path.name.endswith("_test.go"):
            tests += [str(path.relative_to(ROOT)).replace(os.sep, "/") + ":" + name
                      for name in re.findall(r"^func (Test\w+)\(", path.read_text(encoding="utf-8"), re.M)]
    return {"request_ids": [case["id"] for case in cases()], "request_count": len(cases()),
            "implementations": ["P0", "D0", "Go"], "schema_shapes": ["fresh", "retained"],
            "go_tests": tests, "go_test_count": len(tests),
            "files": {str(path.relative_to(ROOT)).replace(os.sep, "/"): digest(path.read_bytes())
                      for path in paths}}


def verify_vectors():
    expected = json.loads((HERE / "vectors-manifest.json").read_text(encoding="utf-8"))
    if expected != vector_manifest():
        raise AssertionError("Exact source/test/vector manifest changed; do not regenerate to hide a mismatch")


def snapshot(path):
    # Only paths freshly allocated by this harness are passed here.
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        ddl = db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()
        tables = {}
        for name, in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
            quoted = '"' + name.replace('"', '""') + '"'
            cursor = db.execute(f"SELECT * FROM {quoted}")
            columns = [entry[0] for entry in cursor.description]
            rows = [[{"type": type(value).__name__, "value": value.hex() if isinstance(value, bytes) else value}
                     for value in row] for row in cursor]
            rows.sort(key=lambda row: json.dumps(row, sort_keys=True))
            tables[name] = {"columns": columns, "rows": rows}
        return {"application_id": db.execute("PRAGMA application_id").fetchone()[0],
                "version": db.execute("PRAGMA user_version").fetchone()[0],
                "journal": db.execute("PRAGMA journal_mode").fetchone()[0],
                "ddl": ddl, "tables": tables}


def fresh(directory, sources, retained=False):
    directory.mkdir()
    (directory / "DISPOSABLE").write_bytes(b"phalanx-go-conformance-v1\n")
    database = directory / "fixture.sqlite3"
    if retained:
        invoke("initialize", sources["retained"], database)
        invoke("initialize", sources["D0"], database)
    invoke("seed", sources["P0"], database)
    if retained:
        with closing(sqlite3.connect(database)) as db:
            # Operator-provided public recipe's DDL pin; not a live-file read.
            ddl = db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()
            expected = "4186600d653716a1688bfb4a425bc55bea4fdef7aa0d756d7fdf07a730e93950"
            actual = digest(json.dumps(ddl, separators=(",", ":"), ensure_ascii=True).encode())
            if actual != expected:
                raise AssertionError(f"Retained D0 schema pin mismatch: {actual}")
    return database


@contextmanager
def server(command, directory, expose=False):
    errors = (directory / "server-errors.txt").open("w", encoding="utf-8")
    process = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=errors,
                               stdin=subprocess.PIPE, text=True, encoding="utf-8")
    try:
        with ThreadPoolExecutor(max_workers=1) as reader:
            future = reader.submit(process.stdout.readline)
            try:
                url = future.result(timeout=20).strip()
            except TimeoutError:
                process.kill()
                raise AssertionError("Server did not publish a loopback listener")
        if not url.startswith("http://127.0.0.1:"):
            errors.flush()
            raise AssertionError(f"Server failed: {url}; " +
                                 (directory / "server-errors.txt").read_text(encoding="utf-8"))
        until = time.monotonic() + 20
        while True:
            try:
                response = wire(url, "GET", "/health", [], "")
                if response["status"] == 200:
                    break
            except (OSError, http.client.HTTPException):
                if process.poll() is not None:
                    errors.flush()
                    raise AssertionError("Server exited before readiness: " +
                                         (directory / "server-errors.txt").read_text(encoding="utf-8"))
            if time.monotonic() >= until:
                raise AssertionError("Health readiness deadline exceeded")
            time.sleep(0.02)
        yield (url, process) if expose else url
    finally:
        if process.poll() is None:
            process.stdin.write("stop\n")
            process.stdin.flush()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
            raise AssertionError("Owned conformance server did not drain/exit")
        process.stdin.close()
        process.stdout.close()
        errors.close()


def wire(url, method, path, headers, body):
    target = urlsplit(url)
    connection = http.client.HTTPConnection(target.hostname, target.port, timeout=15)
    data = body.encode("utf-8")
    try:
        connection.putrequest(method, path, skip_host=any(k.lower() == "host" for k, _ in headers))
        for name, value in headers:
            connection.putheader(name, value.replace("$ORIGIN", url))
        connection.putheader("Content-Length", str(len(data)))
        connection.endheaders(data)
        response = connection.getresponse()
        raw = response.read()
        fields = [(name.lower(), value.replace(url, "$ORIGIN")) for name, value in response.getheaders()]
        parsed = json.loads(raw) if raw and "application/json" in response.getheader("Content-Type", "") else None
        return {"status": response.status, "headers": fields, "body_hex": raw.hex(),
                "json": parsed, "text": raw.decode("utf-8")}
    finally:
        connection.close()


def comparable(response):
    # Representation-only JSON escaping/order/whitespace and transport-generated
    # Date/Server/length do not change types, nulls, arrays, or field presence.
    headers = sorted((name, value) for name, value in response["headers"]
                     if name not in {"date", "server", "content-length", "connection", "transfer-encoding"})
    body = typed_json(response["json"]) if response["json"] is not None else ("text", response["text"])
    return response["status"], headers, body


def typed_json(value):
    if isinstance(value, dict):
        return ("object", tuple((key, typed_json(entry)) for key, entry in sorted(value.items())))
    if isinstance(value, list):
        return ("array", tuple(typed_json(entry) for entry in value))
    # Python equality alone considers True == 1 == 1.0. Wire conformance must not.
    return (type(value).__name__, repr(value))


def verify_comparator():
    if typed_json({"n": True}) == typed_json({"n": 1}) or typed_json([1]) == typed_json([1.0]):
        raise AssertionError("Comparator discarded numeric/bool types")
    if typed_json({}) == typed_json({"value": None}) or typed_json([1, 2]) == typed_json([2, 1]):
        raise AssertionError("Comparator discarded field presence or array order")
    if typed_json({"a": 1, "b": None}) != typed_json({"b": None, "a": 1}):
        raise AssertionError("Comparator treated JSON object presentation as semantic")

def cases():
    result = []

    def add(name, method="GET", path="/health", value=None, token=OWNER, headers=(), raw=None):
        fields = [] if token is None else [("Authorization", "Bearer " + token)]
        if (value is not None or raw is not None) and not any(name.lower() == "content-type" for name, _ in headers):
            fields.append(("Content-Type", "application/json"))
        fields.extend(headers)
        body = raw if raw is not None else "" if value is None else json.dumps(value, ensure_ascii=True, separators=(",", ":"))
        result.append({"id": name, "method": method, "path": path, "headers": fields, "body": body})

    add("anonymous-health", token=None)
    for role, token in (("owner", OWNER), ("runner", RUNNER), ("writer", WRITER), ("invalid", "not-valid"), ("anonymous", None)):
        for route in ("owner", "runner"):
            add(f"{role}-{route}-probe", path=f"/v1/{route}/status", token=token)
        add(f"{role}-item-read", path="/v1/items/" + SEED, token=token)
    add("writer-other-item", path="/v1/items/another", token=WRITER)
    add("writer-repositories", path="/v1/repositories", token=WRITER)
    add("repositories", path="/v1/repositories")
    add("missing-item", path="/v1/items/missing")
    add("bad-item-id", path="/v1/items/invalid%21")
    add("escaped-slash", path="/v1/items/a%2Fb")
    add("health-head", method="HEAD")
    add("wrong-method", method="DELETE", path="/v1/items/" + SEED)
    add("no-follow-redirect", path="/v1/owner/status/")
    add("unknown-route", path="/not-in-the-slice")
    add("register", "POST", "/v1/repositories", {"id": "new", "remote": "  https://github.com/example/new.git  "})
    add("register-replay", "POST", "/v1/repositories", {"id": "new", "remote": "https://github.com/example/new.git"})
    add("register-conflict", "POST", "/v1/repositories", {"id": "new", "remote": "different"})
    base = {"repository_id": "fixture", "goal": "  Go \u96ea \U0001f680 <>& \u2028 \x1c  "}
    for name, payload in (
        ("defaults", base), ("explicit-nulls", dict(base, target_branch=None, label=None)),
        ("explicit-defaults", dict(base, target="checked_in", priority="normal", publication_mode="ready",
                                  context_links=[], acceptance_criteria=[], dependencies=[])),
    ):
        add(name, "POST", "/v1/items", payload, headers=[("Idempotency-Key", "defaults")])
    add("changed-receipt", "POST", "/v1/items", dict(base, goal="Changed"), headers=[("Idempotency-Key", "defaults")])
    add("empty-body-and-key", "POST", "/v1/items", raw="", headers=[("Idempotency-Key", "")])
    historical = {"repository_id": "fixture", "goal": "Historical receipt \u96ea"}
    for key in ("old-null", "current-model", "unproved"):
        add("historical-" + key, "POST", "/v1/items", historical, headers=[("Idempotency-Key", key)])
        add("historical-changed-" + key, "POST", "/v1/items", dict(historical, goal="Changed"),
            headers=[("Idempotency-Key", key)])
    for name, value in (
        ("empty-object", {}), ("array-model", []), ("string-model", "text"), ("null-model", None),
        ("coercion", {"repository_id": False, "goal": 42}),
        ("float-coercion", {"repository_id": "fixture", "goal": 1.25}),
        ("big-int-coercion", {"repository_id": "fixture", "goal": 9007199254740993}),
        ("null-required", {"repository_id": None, "goal": None}),
        ("blank", dict(base, goal="\u00a0\x1c")),
        ("bad-id", dict(base, repository_id="a!")),
        ("target-null", dict(base, target=None)),
        ("branch-empty", dict(base, target_branch=" ")),
        ("lists-null", dict(base, context_links=None, dependencies=None)),
        ("lists-scalar", dict(base, acceptance_criteria="text")),
        ("list-coercion", dict(base, context_links=[False, None, 1])),
        ("bad-label", dict(base, label="a!")),
        ("label-null", dict(base, label=None)),
        ("retired-null", dict(base, runner_affinity=None)),
        ("retired-value", dict(base, runner_affinity="runner")),
        ("extra", dict(base, future=None, arbitrary=1)),
        ("draft-report", dict(base, target="report", publication_mode="draft")),
        ("bad-enums", dict(base, target=1, priority=True, publication_mode="invalid")),
        ("dependency-errors", dict(base, dependencies=[{}, {"upstream_id":None, "milestone":None}, "text"])),
        ("dependency-extra", dict(base, dependencies=[{"upstream_id":SEED, "milestone":"report", "old":None}])),
        ("unregistered", dict(base, repository_id="absent")),
        ("missing-prerequisite", dict(base, dependencies=[{"upstream_id":"missing"}])),
        ("type-prerequisite", dict(base, dependencies=[{"upstream_id":SEED}])),
        ("duplicate-prerequisite", dict(base, dependencies=[{"upstream_id":SEED, "milestone":"report"}]*2)),
        ("valid-prerequisite", dict(base, dependencies=[{"upstream_id":SEED, "milestone":"report"}])),
        ("list-bound", dict(base, context_links=["x"]*51)),
        ("dependency-bound", dict(base, dependencies=[{"upstream_id":SEED}]*101)),
        ("goal-bound", dict(base, goal="x"*16001)),
        ("identifier-bound", dict(base, repository_id="x"*101)),
    ):
        add(name, "POST", "/v1/items", raw=json.dumps(value, separators=(",", ":")))
    for name, raw in (
        ("empty-body", ""), ("invalid-json", "{"), ("trailing-comma", '{"a":1,}'),
        ("extra-json", '{} {}'), ("duplicate-key", '{"repository_id":"fixture","goal":"first","goal":"last"}'),
        ("nested-extra-order", '{"repository_id":"fixture","goal":"ok","dependencies":[{"upstream_id":"a","z":1,"a":2}]}'),
        ("surrogate-high", '{"repository_id":"fixture","goal":"\\ud800"}'),
        ("surrogate-low", '{"repository_id":"fixture","goal":"\\udc00"}'),
        ("nan-input", '{"repository_id":"fixture","goal":NaN}'),
        ("inf-input", '{"repository_id":"fixture","goal":Infinity}'),
        ("negative-inf-input", '{"repository_id":"fixture","goal":-Infinity}'),
        ("overflow-float", '{"repository_id":"fixture","goal":1e309}'),
        ("unterminated-string", '{"goal":"x}'),
        ("invalid-escape", '{"goal":"\\x"}'),
        ("invalid-unicode-escape", '{"goal":"\\uZZZZ"}'),
        ("unicode-error-position", '{"goal":"雪" x}'),
        ("missing-colon", '{"goal" "x"}'),
        ("unterminated-object", '{"goal":1'),
        ("trailing-array", '[1,]'),
        ("nan-bad-key", '{NaN:1}'),
        ("utf8-bom", '\ufeff{"repository_id":"fixture","goal":"BOM input"}'),
    ):
        add(name, "POST", "/v1/items", raw=raw)
    for name, fields in (
        ("same-origin", [("Origin", "$ORIGIN"), ("Sec-Fetch-Site", "same-origin")]),
        ("origin-only", [("Origin", "$ORIGIN")]),
        ("duplicate-origin", [("Origin", "$ORIGIN"), ("Origin", "https://other.invalid")]),
        ("forwarded-origin", [("Origin", "https://other.invalid"), ("Forwarded", "proto=https;host=other.invalid")]),
        ("forwarded-bearer", [("X-Forwarded-For", "203.0.113.1")]),
        ("cross-origin", [("Origin", "https://other.invalid")]),
        ("null-origin", [("Origin", "null")]),
        ("site-without-origin", [("Sec-Fetch-Site", "same-origin")]),
        ("cross-site", [("Origin", "$ORIGIN"), ("Sec-Fetch-Site", "cross-site")]),
        ("empty-key", [("Idempotency-Key", "")]),
        ("long-key", [("Idempotency-Key", "x"*201)]),
        ("key-null-string", [("Idempotency-Key", "null")]),
        ("latin1-header-key", [("Idempotency-Key", "\u00e9")]),
    ):
        add(name, "POST", "/v1/items", base, headers=fields)
    for token in (None, RUNNER, WRITER):
        add("intake-role-" + str(token), "POST", "/v1/items", base, token=token)
    add("application-json-suffix", "POST", "/v1/items", base,
        headers=[("Content-Type", "application/vnd.example+json")])
    add("text-json-suffix", "POST", "/v1/items", base,
        headers=[("Content-Type", "text/example+json")])
    return result


def sequence(url, database):
    observations = []
    tokens = {}
    for case in cases():
        before = snapshot(database)
        response = wire(url, case["method"], case["path"], case["headers"], case["body"])
        after = snapshot(database)
        if response["status"] >= 400 and before != after:
            raise AssertionError(f"Refusal had durable side effects: {case['id']}")
        token = after["tables"]["operator_change"]["rows"][0][0]["value"]
        if len(token) != 32 or any(char not in "0123456789ABCDEF" for char in token):
            raise AssertionError("Malformed generated SQLite change token")
        tokens.setdefault(token, len(tokens))
        observations.append({"request": case, "response": response, "durable": after,
                             "change_token_ordinal": tokens[token]})
    return observations


def comparable_durable(observation):
    result = json.loads(json.dumps(observation["durable"]))
    # One bijection per implementation and sequence. Preserve all original
    # randomblob tokens in evidence; only their relational identity is mapped.
    result["tables"]["operator_change"]["rows"][0][0]["value"] = (
        f"$CHANGE_TOKEN_{observation['change_token_ordinal']}")
    return result


def pack(observations):
    snapshots = {}
    schema = None
    rows = {}
    packed = []
    for observation in observations:
        durable = observation["durable"]
        common = {key: value for key, value in durable.items() if key != "tables"}
        common["columns"] = {name: table["columns"] for name, table in durable["tables"].items()}
        if schema is None:
            schema = common
        elif schema != common:
            raise AssertionError("Conformance sequence changed schema")
        snapshot_rows = {}
        for name, table in durable["tables"].items():
            key = digest(json.dumps(table["rows"], ensure_ascii=True, separators=(",", ":")).encode())
            rows.setdefault(key, table["rows"])
            snapshot_rows[name] = key
        key = digest(json.dumps(snapshot_rows, sort_keys=True).encode())
        snapshots.setdefault(key, snapshot_rows)
        packed.append({**{k: v for k, v in observation.items() if k != "durable"}, "snapshot": key})
    return {"schema": schema, "table_rows": rows, "snapshots": snapshots, "observations": packed}


def unpack(packed):
    observations = []
    for observation in packed["observations"]:
        durable = {key: value for key, value in packed["schema"].items() if key != "columns"}
        durable["tables"] = {
            name: {"columns": packed["schema"]["columns"][name], "rows": packed["table_rows"][key]}
            for name, key in packed["snapshots"][observation["snapshot"]].items()
        }
        observations.append({**{k: v for k, v in observation.items() if k != "snapshot"}, "durable": durable})
    return observations


def lock_checks(binary, root, sources):
    directory = root / "locks"
    database = fresh(directory, sources, True)
    for pin in ("P0", "D0"):
        holder = subprocess.Popen([sys.executable, "-P", str(HERE / "oracle.py"), "hold-lock",
                                   str(sources[pin]), str(database)],
                                  cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            if holder.stdout.readline().strip() != "acquired":
                raise AssertionError("Python could not acquire disposable lock")
            contender = subprocess.run([str(binary), "--fixture", str(database)],
                                       cwd=ROOT, capture_output=True, timeout=10, text=True)
            if contender.returncode == 0:
                raise AssertionError("Go bypassed Python's exact OS lock")
        finally:
            holder.communicate("\n", timeout=10)
    with server([str(binary), "--fixture", str(database)], directory):
        for pin in ("P0", "D0"):
            result = invoke("lock", sources[pin], database, capture_output=True, text=True)
            if result.stdout.strip() != "excluded":
                raise AssertionError("Python bypassed Go's exact OS lock")
    for pin in ("P0", "D0"):
        result = invoke("lock", sources[pin], database, capture_output=True, text=True)
        if result.stdout.strip() != "acquired":
            raise AssertionError("Go lock was not released on exit")
    print("Bidirectional Python-Go sole-instance lock exclusion and exit release PASS")


def mutation(url, body, key):
    return wire(url, "POST", "/v1/items",
                [("Authorization", "Bearer " + OWNER), ("Content-Type", "application/json"),
                 ("Idempotency-Key", key)], json.dumps(body, separators=(",", ":")))


def switch_and_fault_checks(binary, root, sources, output):
    directory = root / "package-switch"
    database = fresh(directory, sources, True)
    initial = snapshot(database)
    body = {"repository_id": "fixture", "goal": "Package switch \u96ea \U0001f680"}
    commands = {
        "P0": [sys.executable, "-P", str(HERE / "oracle.py"), "serve", str(sources["P0"]), str(database)],
        "Go": [str(binary), "--fixture", str(database), "--id-start", "8192"],
        "D0": [sys.executable, "-P", str(HERE / "oracle.py"), "serve", str(sources["D0"]), str(database),
               "--id-start", "12288"],
    }
    receipts = {}
    records = []
    for implementation, key in (("P0", "before-switch"), ("Go", "go-created"), ("D0", "rollback-created")):
        with server(commands[implementation], directory) as url:
            for prior, expected in receipts.items():
                before = snapshot(database)
                response = mutation(url, body, prior)
                if response["status"] != 201 or response["json"] != expected:
                    raise AssertionError(f"{implementation} cannot replay {prior} after package switch")
                if snapshot(database) != before:
                    raise AssertionError("Package-switch replay mutated current data")
            response = mutation(url, body, key)
            if response["status"] != 201:
                raise AssertionError(f"{implementation} cannot mutate evolved current data")
            receipts[key] = response["json"]
            records.append({"implementation": implementation, "key": key, "response": response,
                            "durable": snapshot(database)})
    with server(commands["Go"][:-1] + ["16384"], directory, expose=True) as (url, process):
        for key, expected in receipts.items():
            if mutation(url, body, key)["json"] != expected:
                raise AssertionError("Go restart did not preserve switched-package receipts")
        # Abort at receipt insertion: all earlier item/execution/history writes
        # and operator-change triggers must roll back, with a visible 500.
        with closing(sqlite3.connect(database)) as db:
            db.execute("""CREATE TRIGGER injected_receipt_failure BEFORE INSERT ON request_receipts
                WHEN NEW.idempotency_key='abort-receipt'
                BEGIN SELECT RAISE(ABORT, 'injected receipt failure'); END""")
            db.commit()
        before = snapshot(database)
        response = mutation(url, body, "abort-receipt")
        if response["status"] != 500 or snapshot(database) != before:
            raise AssertionError("Receipt failure leaked side effects or was mislabeled")
        with closing(sqlite3.connect(database)) as db:
            db.execute("DROP TRIGGER injected_receipt_failure")
            db.commit()
        if mutation(url, body, "abort-receipt")["status"] != 201:
            raise AssertionError("Receipt failure did not recover in the same process")
        # The complete request is dispatched while SQLite is externally locked;
        # the caller then loses the socket before any response is read.
        with closing(sqlite3.connect(database, isolation_level=None)) as blocker:
            blocker.execute("BEGIN EXCLUSIVE")
            target = urlsplit(url)
            call = http.client.HTTPConnection(target.hostname, target.port, timeout=10)
            payload = json.dumps(body).encode()
            call.request("POST", "/v1/items", body=payload,
                         headers={"Authorization": "Bearer " + OWNER,
                                  "Content-Type": "application/json", "Idempotency-Key": "lost-response"})
            time.sleep(0.15)
            call.close()
            blocker.execute("ROLLBACK")
        until = time.monotonic() + 8
        stored = None
        while stored is None:
            with closing(sqlite3.connect(database.as_uri()+"?mode=ro", uri=True, timeout=2)) as db:
                row = db.execute("SELECT response_json FROM request_receipts WHERE scope='intake' AND idempotency_key='lost-response'").fetchone()
                if row:
                    stored = json.loads(row[0])
            if stored is None:
                if time.monotonic() >= until:
                    raise AssertionError("Dispatched disconnected mutation did not complete")
                time.sleep(0.02)
        # Owned crash after commit/before receipt delivery, never an old snapshot
        # restoration. The current file must retain the accepted receipt.
        durable = snapshot(database)
        process.kill()
        process.wait(timeout=10)
    with server(commands["D0"], directory) as url:
        before = snapshot(database)
        response = mutation(url, body, "lost-response")
        if response["status"] != 201 or response["json"] != stored or snapshot(database) != before:
            raise AssertionError("D0 rollback/retry lost or duplicated a committed disconnected write")
        own_read = wire(url, "GET", "/v1/items/" + SEED,
                        [("Authorization", "Bearer " + WRITER)], "")
        if own_read["status"] != 200:
            raise AssertionError("Retained writer binding was lost across package switch/crash")
        final = snapshot(database)
        if (initial["ddl"] != final["ddl"]
                or initial["tables"]["writers"] != final["tables"]["writers"]
                or initial["tables"]["dispatch_settings"] != final["tables"]["dispatch_settings"]):
            raise AssertionError("Switch/crash changed retained schema, binding, or settings")
        for table in ("work_items", "executions"):
            if len(final["tables"][table]["rows"]) != len(initial["tables"][table]["rows"]) + 5:
                raise AssertionError("Lost-response/abort replay duplicated work")
    records.append({"fault": "lost-response/owned-crash/D0-retry", "response": response,
                    "committed": durable, "durable": final})
    (output / "package-switch-faults.json").write_text(json.dumps(records, ensure_ascii=True), encoding="utf-8")
    print("P0 -> Go -> D0 -> Go/crash -> D0 current-data replay, refusal rollback, retained binding PASS")


def busy_checks(binary, root, sources, output):
    records = []
    expected = None
    for label in ("P0", "D0", "Go"):
        directory = root / ("busy-" + label)
        database = fresh(directory, sources)
        command = [str(binary), "--fixture", str(database)] if label == "Go" else [
            sys.executable, "-P", str(HERE / "oracle.py"), "serve", str(sources[label]), str(database)]
        with server(command, directory) as url:
            before = snapshot(database)
            with closing(sqlite3.connect(database, isolation_level=None)) as blocker:
                blocker.execute("BEGIN EXCLUSIVE")
                start = time.monotonic()
                response = mutation(url, {"repository_id": "fixture", "goal": "Busy refusal"}, "busy-key")
                elapsed = time.monotonic() - start
                blocker.execute("ROLLBACK")
            # End-to-end HTTP elapsed time includes dispatch/host scheduling;
            # it is recorded, not confused with SQLite's per-call busy bound.
            if (response["status"] != 503 or ("retry-after", "1") not in response["headers"]
                    or snapshot(database) != before):
                raise AssertionError(f"{label}: busy refusal/bound/side effects differ ({elapsed:.3f}s)")
            if expected is None:
                expected = comparable(response)
            elif comparable(response) != expected:
                raise AssertionError(f"{label}: SQLite busy wire detail differs")
            if mutation(url, {"repository_id": "fixture", "goal": "Busy refusal"}, "busy-key")["status"] != 201:
                raise AssertionError("Busy refusal did not recover in the same process")
            records.append({"implementation": label, "http_seconds": elapsed, "response": response,
                            "refusal_durable": before, "recovery_durable": snapshot(database)})
    (output / "busy-faults.json").write_text(json.dumps(records, ensure_ascii=True), encoding="utf-8")
    print("P0/D0/Go exact 503+Retry-After/detail, zero refusal effects, same-process recovery PASS")


def differential(binary, output, freeze=False):
    verify_comparator()
    verify_manifest()
    if not freeze:
        verify_vectors()
    output.mkdir(parents=True, exist_ok=True)
    with disposable_root() as root:
        sources = {label: export(label, root) for label in PINS}
        for retained in (False, True):
            shape = "retained" if retained else "fresh"
            frozen_path = HERE / f"golden-{shape}.json"
            expected = None
            for label in ("P0", "D0", "Go"):
                directory = root / f"{shape}-{label}"
                database = fresh(directory, sources, retained)
                command = [str(binary), "--fixture", str(database)] if label == "Go" else [
                    sys.executable, "-P", str(HERE / "oracle.py"), "serve", str(sources[label]), str(database)]
                with server(command, directory) as url:
                    actual = sequence(url, database)
                    client_output = directory / "client.json"
                    invoke("client", sources["P0" if label == "Go" else label], url,
                           "--output", client_output)
                    for revision in ("P0", "D0"):
                        # Existing clients both read/write/retry against the slice.
                        invoke("client", sources[revision], url, "--output", client_output)
                (output / f"{shape}-{label}.json").write_text(json.dumps(actual, ensure_ascii=True), encoding="utf-8")
                if expected is None:
                    expected = actual
                    if freeze:
                        frozen_path.write_text(json.dumps(pack(actual), ensure_ascii=True), encoding="utf-8")
                    else:
                        frozen = unpack(json.loads(frozen_path.read_text(encoding="utf-8")))
                        for old, new in zip(frozen, actual, strict=True):
                            if (old["request"] != json.loads(json.dumps(new["request"]))
                                    or comparable(old["response"]) != comparable(new["response"])
                                    or comparable_durable(old) != comparable_durable(new)):
                                raise AssertionError(f"Immutable golden oracle drift: {shape}/{new['request']['id']}")
                else:
                    for first, second in zip(expected, actual, strict=True):
                        name = first["request"]["id"]
                        if comparable(first["response"]) != comparable(second["response"]):
                            raise AssertionError(f"{shape}/{label}/{name}: wire mismatch\n"
                                                 f"{comparable(first['response'])!r}\n{comparable(second['response'])!r}")
                        if comparable_durable(first) != comparable_durable(second):
                            raise AssertionError(f"{shape}/{label}/{name}: durable mismatch")
            print(f"{shape}: {len(expected)} exact request/durable vectors x P0,D0,Go PASS")
        lock_checks(binary, root, sources)
        switch_and_fault_checks(binary, root, sources, output)
        busy_checks(binary, root, sources, output)
    if freeze:
        (HERE / "vectors-manifest.json").write_text(json.dumps(vector_manifest(), indent=2), encoding="utf-8")


@contextmanager
def disposable_root():
    temporary = TemporaryDirectory(prefix="phalanx-go-conformance-")
    try:
        yield Path(temporary.name)
    finally:
        deadline = time.monotonic() + 5
        while True:
            try:
                temporary.cleanup()
                break
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--freeze", action="store_true")
    parser.add_argument("--freeze-manifest", action="store_true")
    args = parser.parse_args()
    if args.freeze_manifest:
        (HERE / "manifest.json").write_text(json.dumps(source_manifest(), indent=2), encoding="utf-8")
        return
    if args.binary is None or args.output is None:
        parser.error("--binary and --output are required")
    differential(args.binary.resolve(), args.output.resolve(), args.freeze)


if __name__ == "__main__":
    main()

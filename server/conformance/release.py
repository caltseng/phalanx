"""Real packaged entrypoint, current-data rollback/admin and matched synthetic load."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
import json
import math
import os
from pathlib import Path
import platform
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import zipfile
from urllib.parse import urlsplit

import harness as h


def environment(database):
    return {**{key: value for key, value in os.environ.items() if not key.startswith("PHALANX_")},
            "PHALANX_DB": str(database), "PHALANX_OWNER_TOKEN": h.OWNER,
            "PHALANX_RUNNER_TOKEN": h.RUNNER, "PHALANX_LOCAL_BOARD": "0"}


def parameters(binary, database):
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    return [str(binary), "--host", "127.0.0.1", "--port", str(port),
            "--assets", str(binary.parent / "assets")], f"http://127.0.0.1:{port}"


@contextmanager
def production(binary, database, directory, python_source=None):
    command, url = parameters(binary, database)
    if python_source is not None:
        command = [sys.executable, "-P", "-c",
                   "import json,os,signal,sys; from threading import Thread; "
                   "sys.path.insert(0,sys.argv.pop(1)); "
                   "from phalanx.deployment import run_service; from phalanx.server_admin import serve; "
                   "Thread(target=lambda:(sys.stdin.buffer.read(),signal.raise_signal(signal.SIGINT)),daemon=True).start(); "
                   "sys.exit(run_service(json.loads(sys.argv[1]),dict(os.environ),'server',serve=serve))",
                   str(python_source), json.dumps({"server": {"host": "127.0.0.1", "port": urlsplit(url).port}})]
    with (directory / "server.log").open("ab") as log:
        log_offset = log.tell()
        process = subprocess.Popen(command, env=environment(database), cwd=directory,
                                   stdin=subprocess.PIPE if python_source is not None else subprocess.DEVNULL,
                                   stdout=log, stderr=log,
                                   creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0)
        try:
            deadline = time.monotonic() + 15
            while True:
                if process.poll() is not None:
                    raise AssertionError("Packaged server exited before authenticated readiness")
                own_listener = ("address=" + url.removeprefix("http://") if python_source is None
                                else "Uvicorn running on " + url).encode()
                if own_listener not in (directory / "server.log").read_bytes()[log_offset:]:
                    if time.monotonic() >= deadline:
                        raise AssertionError("Owned server did not confirm its listener before client probes")
                    time.sleep(.05)
                    continue
                try:
                    response = h.wire(url, "GET", "/v1/owner/status", [("Authorization", "Bearer " + h.OWNER)], "")
                    if response["status"] == 200 and response["json"] == {"role": "owner"}:
                        break
                except OSError:
                    pass
                if time.monotonic() >= deadline:
                    raise AssertionError("Packaged server failed authenticated startup")
                time.sleep(.05)
            yield url, process
        finally:
            if process.poll() is None:
                if python_source is not None:
                    process.stdin.close()
                else:
                    process.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGTERM)
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
                    raise AssertionError("Owned synthetic server did not gracefully drain; forced cleanup is not PASS")
            if process.returncode != 0:
                raise AssertionError(f"Packaged server exit was not clean: {process.returncode}")


def requests():
    result = []
    for index in range(120):
        if index % 4 == 0:
            result.append({"id": index, "method": "POST", "path": "/v1/items",
                           "body": {"repository_id": "fixture", "goal": f"Matched synthetic work {index} \u96ea"}})
        else:
            result.append({"id": index, "method": "GET",
                           "path": "/v1/items/" + h.SEED if index % 4 == 1 else "/v1/dispatch/settings",
                           "body": None})
    return result


def delayed_header(url):
    target = urlsplit(url)
    with socket.create_connection((target.hostname, target.port), timeout=10) as client:
        client.sendall((f"GET /v1/owner/status HTTP/1.1\r\nHost: {target.netloc}\r\n"
                        f"Authorization: Bearer {h.OWNER}").encode())
        time.sleep(5.25)
        client.sendall(b"\r\nConnection: close\r\n\r\n")
        parts = []
        while block := client.recv(65536):
            parts.append(block)
    raw = b"".join(parts)
    header, separator, body = raw.partition(b"\r\n\r\n")
    if not separator or not header.startswith(b"HTTP/1.1 200 ") or json.loads(body) != {"role": "owner"}:
        raise AssertionError("Production launcher imposed an unapproved request-header deadline")
    return {"delay_seconds": 5.25, "raw_response": raw.decode("latin1")}


def role_request(url, close=False):
    return (f"GET /v1/owner/status HTTP/1.1\r\nHost: {urlsplit(url).netloc}\r\n"
            f"Authorization: Bearer {h.OWNER}\r\n" +
            ("Connection: close\r\n" if close else "") + "\r\n").encode()


def role_response(client):
    raw = b""
    while b"\r\n\r\n" not in raw:
        block = client.recv(65536)
        if not block:
            raise AssertionError("Role response closed before headers: " + raw.hex())
        raw += block
    header, _, body = raw.partition(b"\r\n\r\n")
    lengths = [line.split(b":", 1)[1].strip() for line in header.split(b"\r\n")[1:]
               if line.lower().startswith(b"content-length:")]
    if len(lengths) != 1:
        raise AssertionError("Expected a complete fixed-length role response")
    while len(body) < int(lengths[0]):
        block = client.recv(65536)
        if not block:
            raise AssertionError("Role response closed before body: " + raw.hex())
        raw += block
        body += block
    if (not header.startswith(b"HTTP/1.1 200 ") or b"content-type: application/json" not in header.lower()
            or json.loads(body) != {"role": "owner"}):
        raise AssertionError("Fragmented request changed role response: " + raw.hex())
    return raw.decode("latin1")


def transport_checks(url, database):
    records = {"initial_header": delayed_header(url)}
    target = urlsplit(url)
    with socket.create_connection((target.hostname, target.port), timeout=10) as client:
        first = role_request(url)
        client.sendall(first)
        first_response = role_response(client)
        time.sleep(2)
        second = role_request(url, close=True)
        client.sendall(second[:-2])
        time.sleep(5.25)
        client.sendall(second[-2:])
        records["keepalive_fragment"] = {
            "prefix_before_idle_seconds": 2, "fragment_delay_seconds": 5.25,
            "first_request": first.decode(), "first_response": first_response,
            "second_request": second.decode(), "second_response": role_response(client)}
    with socket.create_connection((target.hostname, target.port), timeout=8) as client:
        request = role_request(url)
        client.sendall(request)
        response = role_response(client)
        start = time.monotonic()
        closed = client.recv(1)
        elapsed = time.monotonic() - start
        if closed or not 4 <= elapsed <= 6.5:
            raise AssertionError("Genuine idle expiry changed: " + str(elapsed))
        records["idle_expiry"] = {"seconds": elapsed, "request": request.decode(), "response": response}
    body = json.dumps({"repository_id": "fixture", "goal": "Real fragmented body \u96ea"}).encode()
    def prefix(key):
        return (f"POST /v1/items HTTP/1.1\r\nHost: {target.netloc}\r\n"
                f"Authorization: Bearer {h.OWNER}\r\nContent-Type: application/json\r\n"
                f"Idempotency-Key: {key}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode()
    with socket.create_connection((target.hostname, target.port), timeout=10) as client:
        request = prefix("fragmented-body")
        client.sendall(request + body[:len(body)//2])
        time.sleep(5.25)
        client.sendall(body[len(body)//2:])
        raw = b""
        while block := client.recv(65536):
            raw += block
        if not raw.startswith(b"HTTP/1.1 201 "):
            raise AssertionError("Request body acquired an unapproved deadline: " + raw.hex())
        records["body_fragment"] = {"delay_seconds": 5.25, "request": (request+body).decode(), "response": raw.decode("latin1")}
    before = h.snapshot(database)
    with socket.create_connection((target.hostname, target.port), timeout=10) as client:
        partial = prefix("incomplete-disconnect") + body[:len(body)//2]
        client.sendall(partial)
    time.sleep(.25)
    if h.snapshot(database) != before:
        raise AssertionError("Incomplete disconnected body caused durable effects")
    records["incomplete_disconnect"] = {"sent": partial.decode(), "durable_unchanged": True}
    with closing(sqlite3.connect(database, isolation_level=None)) as blocker:
        blocker.execute("BEGIN EXCLUSIVE")
        with socket.create_connection((target.hostname, target.port), timeout=10) as client:
            complete = prefix("complete-disconnect") + body
            client.sendall(complete)
            time.sleep(.15)
        blocker.execute("ROLLBACK")
    deadline = time.monotonic() + 10
    while True:
        with closing(sqlite3.connect(database)) as db:
            row = db.execute("SELECT response_json FROM request_receipts WHERE scope='intake' AND idempotency_key='complete-disconnect'").fetchone()
        if row is not None:
            break
        if time.monotonic() >= deadline:
            raise AssertionError("Dispatched complete body was cancelled by caller loss")
        time.sleep(.05)
    records["complete_disconnect"] = {"sent": complete.decode(), "committed_receipt": json.loads(row[0])}
    return records


def drain_check(url, process, database, sources, python=False):
    with closing(sqlite3.connect(database, isolation_level=None)) as blocker:
        blocker.execute("BEGIN EXCLUSIVE")
        with ThreadPoolExecutor(max_workers=1) as caller:
            pending = caller.submit(h.mutation, url, {"repository_id": "fixture", "goal": "Real shutdown drain"}, "final-drain")
            time.sleep(.15)
            if python:
                process.stdin.close()
            else:
                process.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGTERM)
            probe = h.invoke("lock", sources["P0"], database, capture_output=True, text=True)
            if process.poll() is not None or probe.stdout.strip() != "excluded":
                raise AssertionError("Shutdown released sole ownership before its active mutation drained")
            blocker.execute("ROLLBACK")
            response = pending.result(timeout=10)
            if response["status"] != 201:
                raise AssertionError("Shutdown cancelled an accepted/dispatched operation")
            process.wait(timeout=15)
    return {"response": response, "durable": h.snapshot(database), "clean_exit": process.returncode == 0}


def measure(url, run):
    def request(case):
        headers = [("Authorization", "Bearer " + h.OWNER), ("Content-Type", "application/json")]
        if case["body"] is not None:
            headers.append(("Idempotency-Key", f"load-{run}-{case['id']}"))
        start = time.perf_counter()
        response = h.wire(url, case["method"], case["path"], headers,
                          json.dumps(case["body"]) if case["body"] is not None else "")
        elapsed = time.perf_counter() - start
        expected = 201 if case["body"] is not None else 200
        if response["status"] != expected:
            raise AssertionError(f"Load request {case['id']} failed: {response['status']}")
        return {"id": case["id"], "seconds": elapsed, "status": response["status"]}
    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=8) as pool:
        records = list(pool.map(request, requests()))
    elapsed = time.perf_counter() - start
    ordered = sorted(record["seconds"] for record in records)
    return {"run": run, "count": len(records), "accepted_writes": 30, "successful_reads": 90,
            "elapsed_seconds": elapsed, "requests_per_second": len(records) / elapsed,
            "p50_seconds": ordered[math.ceil(.50 * len(ordered)) - 1],
            "p95_seconds": ordered[math.ceil(.95 * len(ordered)) - 1],
            "p99_seconds": ordered[math.ceil(.99 * len(ordered)) - 1],
            "max_seconds": ordered[-1], "records": records}


def release(package, output):
    h.verify_manifest()
    output.mkdir(parents=True, exist_ok=True)
    package_manifest = None
    with h.disposable_root() as root:
        package_root = root / "package"
        with zipfile.ZipFile(package) as archive:
            for member in archive.namelist():
                if Path(member).is_absolute() or ".." in Path(member).parts:
                    raise AssertionError("Unsafe package path")
            archive.extractall(package_root)
        package_manifest = json.loads((package_root / "manifest.json").read_text())
        for name, expected in package_manifest["files"].items():
            if h.digest((package_root / name).read_bytes()) != expected:
                raise AssertionError("Package bytes do not match versioned manifest")
        binary = package_root / ("phalanx-server.exe" if os.name == "nt" else "phalanx-server")
        if os.name != "nt":
            binary.chmod(0o755)
        sources = {label: h.export(label, root) for label in h.PINS}
        for retained in (False, True):
            directory = root / ("release-retained" if retained else "release-fresh")
            database = h.fresh(directory, sources, retained)
            initial = h.snapshot(database)
            receipts = {}
            body = {"repository_id": "fixture", "goal": "Real release current data \u96ea"}
            for reference in ("P0", "D0"):
                with production(binary, database, directory) as (url, process):
                    for pin in ("P0", "D0"):
                        h.invoke("client", sources[pin], url, "--output", output / f"client-{retained}-{reference}-{pin}.json")
                        probe = h.invoke("lock", sources[pin], database, capture_output=True, text=True)
                        if probe.stdout.strip() != "excluded":
                            raise AssertionError("Python acquired the real Go server's lock")
                    response = h.mutation(url, body, "real-release-" + reference)
                    if response["status"] != 201:
                        raise AssertionError("Real release did not accept new writes")
                    receipts["real-release-" + reference] = response
                    backup = directory / ("backup-" + reference + ".sqlite3")
                    subprocess.run([sys.executable, "-P", "-c",
                                    "import sys; sys.path.insert(0,sys.argv.pop(1)); "
                                    "from phalanx.__main__ import main; main()",
                                    str(sources[reference]), "server", "backup", str(backup)],
                                   cwd=h.ROOT, env=environment(database), check=True, timeout=15)
                    if h.snapshot(backup) != h.snapshot(database):
                        raise AssertionError("Existing server admin backup lost typed rows/schema")
                    command, _ = parameters(binary, database)
                    contender = subprocess.run(command, env=environment(database), capture_output=True, timeout=10)
                    if contender.returncode == 0 or b"Cannot acquire sole server ownership" not in contender.stderr:
                        raise AssertionError("Two real Go servers acquired authority")
                    (output / f"go-{retained}-{reference}.json").write_text(
                        json.dumps({"response": response, "durable": h.snapshot(database), "pid": process.pid}), encoding="utf-8")
                    with closing(sqlite3.connect(database, isolation_level=None)) as blocker:
                        blocker.execute("BEGIN EXCLUSIVE")
                        with ThreadPoolExecutor(max_workers=1) as caller:
                            pending = caller.submit(h.mutation, url, body, "drain-" + reference)
                            time.sleep(.15)
                            process.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGTERM)
                            if process.poll() is not None:
                                raise AssertionError("Shutdown released sole ownership before dispatched write drained")
                            probe = h.invoke("lock", sources[reference], database, capture_output=True, text=True)
                            if probe.stdout.strip() != "excluded":
                                raise AssertionError("Shutdown released the shared OS lock during admission wait")
                            blocker.execute("ROLLBACK")
                            response = pending.result(timeout=10)
                            if response["status"] != 201:
                                raise AssertionError("Graceful shutdown cancelled a dispatched mutation")
                            receipts["drain-" + reference] = response
                            process.wait(timeout=15)
                command = [sys.executable, "-P", str(h.HERE / "oracle.py"), "serve", str(sources[reference]),
                           str(database), "--id-start", "1048576" if reference == "P0" else "2097152"]
                with h.server(command, directory) as url:
                    for key, original in receipts.items():
                        before = h.snapshot(database)
                        replay = h.mutation(url, body, key)
                        if h.comparable(replay) != h.comparable(original) or h.snapshot(database) != before:
                            raise AssertionError("Previous Python could not replay newly accepted Go writes unchanged")
                    command, _ = parameters(binary, database)
                    contender = subprocess.run(command, env=environment(database), capture_output=True, timeout=10)
                    if contender.returncode == 0 or b"Cannot acquire sole server ownership" not in contender.stderr:
                        raise AssertionError("Real Go server bypassed Python sole ownership")
                    if h.mutation(url, body, "previous-python-" + reference)["status"] != 201:
                        raise AssertionError("Previous Python could not write evolved Go data")
                if h.snapshot(database)["ddl"] != initial["ddl"]:
                    raise AssertionError("Package-only switch changed schema")
            missing = directory / "missing.sqlite3"
            command, _ = parameters(binary, missing)
            refused = subprocess.run(command, env=environment(missing), capture_output=True, timeout=10)
            if refused.returncode == 0 or missing.exists():
                raise AssertionError("Missing database startup created/replaced a store")
            print(f"Real package/{retained}: clients, online backup, locks, new-write previous-Python rollback PASS", flush=True)

        loads = []
        for retained in (False, True):
            for label in ("P0", "D0", "Go"):
                directory = root / f"load-{retained}-{label}"
                database = h.fresh(directory, sources, retained)
                baseline = h.snapshot(database)
                manager = production(binary, database, directory,
                                     python_source=sources[label] if label != "Go" else None)
                start = time.perf_counter()
                with manager as running:
                    startup_seconds = time.perf_counter() - start
                    url = running[0]
                    transport_proof = transport_checks(url, database) if not retained else None
                    baseline = h.snapshot(database)
                    for _ in range(12):
                        response = h.wire(url, "GET", "/v1/dispatch/settings", [("Authorization", "Bearer " + h.OWNER)], "")
                        if response["status"] != 200:
                            raise AssertionError("Warmup failed")
                    runs = []
                    for index in range(3):
                        result = measure(url, index)
                        durable = h.snapshot(database)
                        for table in ("work_items", "request_receipts"):
                            if len(durable["tables"][table]["rows"]) != len(baseline["tables"][table]["rows"]) + 30 * (index + 1):
                                raise AssertionError("Matched load lost or duplicated accepted durable effects")
                        if durable["ddl"] != baseline["ddl"]:
                            raise AssertionError("Matched load changed schema")
                        data = json.dumps(durable, sort_keys=True)
                        result["durable_sha256"] = h.digest(data.encode())
                        (output / f"load-{label}-{retained}-{index}-durable.json").write_text(data, encoding="utf-8")
                        runs.append(result)
                    drain_proof = drain_check(url, running[1], database, sources, python=label != "Go") if not retained else None
                loads.append({"implementation": label, "retained": retained,
                              "startup_seconds": startup_seconds, "transport": transport_proof,
                              "drain": drain_proof, "baseline_items": len(baseline["tables"]["work_items"]["rows"]), "runs": runs})
                print(f"{label}/{retained}: three matched load runs PASS", flush=True)
        (output / "runtime-load.json").write_text(json.dumps({
            "build": package_manifest["build"], "package_sha256": h.digest(package.read_bytes()),
            "package_bytes": package.stat().st_size, "binary_bytes": binary.stat().st_size,
            "host": {"platform": platform.platform(), "cpu_count": os.cpu_count(), "python": platform.python_version(),
                     "shared_host": True, "other_users_load": "uncontrolled/not measured",
                     "controller": "sequential P0/D0/Go workloads; other host activity unmeasured"},
            "manifest": requests(), "manifest_sha256": h.digest(json.dumps(requests(), sort_keys=True).encode()),
            "concurrency": 8, "warmup_reads": 12, "runs_per_implementation_shape": 3,
            "metadata_policy": "Real UUIDs and wall clock for all production entrypoints; no fixture clock/ID injection",
            "performance_classification": "observations only; proposed goals not approved or claimed",
            "resource_metrics": "CPU/RSS/GC/goroutine/connection/30-minute-soak unmeasured; not passed gates",
            "results": loads,
        }, indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    release(args.package.resolve(), args.output.resolve())


if __name__ == "__main__":
    main()

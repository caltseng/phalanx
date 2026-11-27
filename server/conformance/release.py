"""Real packaged entrypoint, current-data rollback/admin and matched synthetic load."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
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
def production(binary, database, directory):
    command, url = parameters(binary, database)
    with (directory / "server.log").open("ab") as log:
        process = subprocess.Popen(command, env=environment(database), cwd=directory,
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                   creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0)
        try:
            deadline = time.monotonic() + 15
            while True:
                if process.poll() is not None:
                    raise AssertionError("Packaged server exited before authenticated readiness")
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
                    subprocess.run([sys.executable, "-P", "-m", "phalanx", "server", "backup", str(backup)],
                                   cwd=h.ROOT, env=environment(database), check=True, timeout=15)
                    if h.snapshot(backup) != h.snapshot(database):
                        raise AssertionError("Existing server admin backup lost typed rows/schema")
                    command, _ = parameters(binary, database)
                    contender = subprocess.run(command, env=environment(database), capture_output=True, timeout=10)
                    if contender.returncode == 0 or b"Cannot acquire sole server ownership" not in contender.stderr:
                        raise AssertionError("Two real Go servers acquired authority")
                    (output / f"go-{retained}-{reference}.json").write_text(
                        json.dumps({"response": response, "durable": h.snapshot(database), "pid": process.pid}), encoding="utf-8")
                    with sqlite3.connect(database, isolation_level=None) as blocker:
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
                if label == "Go":
                    manager = production(binary, database, directory)
                else:
                    manager = h.server([sys.executable, "-P", str(h.HERE / "oracle.py"), "serve",
                                        str(sources[label]), str(database)], directory)
                start = time.perf_counter()
                with manager as running:
                    startup_seconds = time.perf_counter() - start
                    url = running[0] if label == "Go" else running
                    for _ in range(12):
                        response = h.wire(url, "GET", "/v1/dispatch/settings", [("Authorization", "Bearer " + h.OWNER)], "")
                        if response["status"] != 200:
                            raise AssertionError("Warmup failed")
                    runs = [measure(url, index) for index in range(3)]
                loads.append({"implementation": label, "retained": retained,
                              "startup_seconds": startup_seconds, "runs": runs})
                print(f"{label}/{retained}: three matched load runs PASS", flush=True)
        (output / "runtime-load.json").write_text(json.dumps({
            "build": package_manifest["build"], "package_sha256": h.digest(package.read_bytes()),
            "package_bytes": package.stat().st_size, "binary_bytes": binary.stat().st_size,
            "host": {"platform": platform.platform(), "cpu_count": os.cpu_count(), "python": platform.python_version(),
                     "shared_host": True, "other_users_load": "uncontrolled/not measured",
                     "controller_background_checks": "operator must record; no isolation or SLO claim"},
            "manifest": requests(), "manifest_sha256": h.digest(json.dumps(requests(), sort_keys=True).encode()),
            "concurrency": 8, "warmup_reads": 12, "runs_per_implementation_shape": 3,
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

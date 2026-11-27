"""Disposable Windows proof. Task Scheduler operations require an isolated CI host."""

import argparse
from contextlib import ExitStack, closing
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time
import traceback
import uuid
from unittest.mock import patch
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "server" / "conformance"))

import harness as h
from phalanx import deployment
from phalanx import windows_supervisor as w
from phalanx.workspaces import any_file_lock


def wait_for(predicate, description):
    deadline = time.monotonic() + 30
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise AssertionError("Synthetic readiness gap: " + description)
        time.sleep(.05)


def fixture_supervise(args):
    config, env = deployment.configuration(args.config)
    original_launch = w.Job.launch

    def cmd_launch(job, command, environment, cwd):
        batch = args.record.with_suffix(".cmd")
        # This is deliberately not the production launcher: reproduce a cmd root
        # that exits while the actual deploy/Python/Go descendants keep serving.
        batch.write_text('@echo off\nstart "" /b ' + subprocess.list2cmdline(command)
                         + '\nexit /b 0\n', encoding="utf-8")
        return original_launch(job, [os.environ["ComSpec"], "/d", "/c", str(batch)], environment, cwd)

    with patch.object(w.Job, "launch", cmd_launch):
        return w.supervise(args.config, config, env, task=args.task, record=args.record,
                           engine="go", go_binary=args.binary)


def powershell(script, payload):
    executable = Path(os.environ["SystemRoot"]) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    result = subprocess.run([str(executable), "-NoProfile", "-NonInteractive", "-Command", script],
                            input=json.dumps(payload), text=True, capture_output=True, check=False)
    if result.returncode:
        raise AssertionError("Isolated synthetic Task Scheduler operation failed: " + result.stderr)
    return result.stdout.strip()


TASK_FIXTURE = r"""
$ErrorActionPreference='Stop'
$request=[Console]::In.ReadToEnd()|ConvertFrom-Json
if (-not $request.name.StartsWith('\PhalanxSynthetic-')) { throw 'Synthetic name required' }
$service=New-Object -ComObject 'Schedule.Service'
$service.Connect()
$folder=$service.GetFolder('\')
if ($request.operation -eq 'register') {
    $null=$folder.RegisterTask($request.name,$request.xml,2,$null,$null,3,$null)
} elseif ($request.operation -eq 'run') {
    $null=$folder.GetTask($request.name).Run($null)
} elseif ($request.operation -eq 'remove') {
    $task=$folder.GetTask($request.name)
    if ($task.GetInstances(0).Count -ne 0) { $task.Stop(0) }
    $folder.DeleteTask($request.name,0)
} elseif ($request.operation -eq 'status') {
    $task=$folder.GetTask($request.name)
    @{state=[int]$task.State;last_result=[long]$task.LastTaskResult;
      instances=@($task.GetInstances(0)|ForEach-Object{
          @{instance=[string]$_.InstanceGuid;engine_pid=[long]$_.EnginePID}
      })}|ConvertTo-Json -Depth 5 -Compress
} else { throw 'Unsupported fixture operation' }
"""


def xml_action(command, cwd):
    sid = powershell("[Security.Principal.WindowsIdentity]::GetCurrent().User.Value", {})
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
<RegistrationInfo><Description>Owned disposable Phalanx Windows CI fixture only</Description></RegistrationInfo>
<Principals><Principal id="Fixture"><UserId>{escape(sid)}</UserId><LogonType>InteractiveToken</LogonType>
<RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
<Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
<Priority>5</Priority></Settings>
<Actions Context="Fixture"><Exec><Command>{escape(command[0])}</Command>
<Arguments>{escape(subprocess.list2cmdline(command[1:]))}</Arguments>
<WorkingDirectory>{escape(str(cwd))}</WorkingDirectory></Exec></Actions></Task>"""


def mutation(url, key, goal):
    body = json.dumps({"repository_id": "fixture", "goal": goal})
    headers = [("Authorization", "Bearer " + h.OWNER), ("Idempotency-Key", key),
               ("Content-Type", "application/json")]
    return h.wire(url, "POST", "/v1/items", headers, body), body, headers


def receipt_rows(database):
    with closing(sqlite3.connect(database)) as store:
        return store.execute("SELECT * FROM request_receipts ORDER BY scope,idempotency_key").fetchall()


def durable_identities(database):
    snapshot = h.snapshot(database)
    result = {}
    for table, content in snapshot["tables"].items():
        if "id" in content["columns"]:
            index = content["columns"].index("id")
            result[table] = sorted(json.dumps(row[index], sort_keys=True) for row in content["rows"])
    return result


def task_rehearsal(args):
    if (os.name != "nt" or os.environ.get("PHALANX_WINDOWS_TASK_ISOLATION") != "1"
            or os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("RUNNER_OS") != "Windows"):
        raise SystemExit("REQUIRED ENVIRONMENT GAP: use the existing isolated Windows CI host; "
                         "Task Scheduler proof must not run or be counted as skipped on a shared host")
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "attempt.json").write_text(json.dumps({
        "status": "in_progress", "scope": "owned disposable isolated Windows TaskScheduler proof",
        "source": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "binary_sha256": h.digest(args.binary.read_bytes()), "passed": False,
    }, indent=2), encoding="utf-8")
    with h.disposable_root() as root, ExitStack() as stack:
        sources = {label: h.export(label, root) for label in h.PINS}
        database = h.fresh(root / "owned", sources)
        lock_path = database.with_name(database.name + ".service.lock")
        lock_path.touch()
        home = deployment.initialize(root / "supervised")
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        config_path = home / "deployment.json"
        config = json.loads(config_path.read_text())
        config["server"]["port"] = port
        config_path.write_text(json.dumps(config), encoding="utf-8")
        (home / "private.env").write_text(
            f"PHALANX_DB={database}\nPHALANX_OWNER_TOKEN={h.OWNER}\nPHALANX_RUNNER_TOKEN={h.RUNNER}\n"
            "PHALANX_LOCAL_BOARD=0\n", encoding="utf-8")
        # The task receives only synthetic settings; inherited PHALANX_* settings
        # cannot replace the fixture's database or credentials.
        fixture_env = {key: value for key, value in os.environ.items() if not key.startswith("PHALANX_")}
        fixture_env["PYTHONPATH"] = str(ROOT)
        name = "\\PhalanxSynthetic-" + uuid.uuid4().hex
        record = home / "ownership.json"
        command = [sys.executable, "-P", str(Path(__file__).resolve()), "--fixture-supervise",
                   "--config", str(config_path), "--record", str(record),
                   "--task", name, "--binary", str(args.binary)]
        task_xml = xml_action(command, ROOT)
        powershell(TASK_FIXTURE, {"operation": "register", "name": name, "xml": task_xml})
        stack.callback(powershell, TASK_FIXTURE, {"operation": "remove", "name": name})
        # A separate unrelated task must remain running throughout both stops.
        unrelated_name = "\\PhalanxSynthetic-" + uuid.uuid4().hex
        unrelated_xml = xml_action(
            [sys.executable, "-I", "-c", "import threading; threading.Event().wait()"], root)
        powershell(TASK_FIXTURE, {"operation": "register", "name": unrelated_name, "xml": unrelated_xml})
        stack.callback(powershell, TASK_FIXTURE, {"operation": "remove", "name": unrelated_name})
        powershell(TASK_FIXTURE, {"operation": "run", "name": unrelated_name})
        unrelated = w.task_control("inspect", task=unrelated_name)["task"]

        # Task Scheduler doesn't inherit the launching process environment. The
        # fixture worker explicitly sets only its source location below.
        url = f"http://127.0.0.1:{port}"

        def healthy():
            try:
                return h.wire(url, "GET", "/v1/owner/status",
                              [("Authorization", "Bearer " + h.OWNER)], "")["status"] == 200
            except OSError:
                return False

        def start():
            powershell(TASK_FIXTURE, {"operation": "run", "name": name})
            failure = record.with_suffix(".failure.json")

            def recorded():
                if failure.is_file():
                    details = json.loads(failure.read_text())
                    (args.output / "worker-failure.json").write_text(json.dumps(details, indent=2))
                    raise AssertionError("Owned fixture supervisor failed: " + details["error"])
                if record.is_file():
                    try:
                        return json.loads(record.read_text())
                    except json.JSONDecodeError:
                        return False
                return False

            try:
                wait_for(recorded, "ownership record")
            except BaseException:
                status = powershell(TASK_FIXTURE, {"operation": "status", "name": name})
                (args.output / "task-status-at-failure.json").write_text(status, encoding="utf-8")
                raise
            wait_for(healthy, "owned Go readiness")
            owned = json.loads(record.read_text())
            native = w._native()
            supervisor = w.Original.capture(native, owned["supervisor"]["pid"], w._query,
                                            expected=owned["supervisor"], duplicate=True)
            try:
                root_handle = native.duplicate(supervisor.handle, owned["root_handle"])
                try:
                    native.wait(root_handle)
                    assert native.exited(root_handle), "cmd root did not exit"
                finally:
                    native.close(root_handle)
            finally:
                supervisor.close()
            with any_file_lock([lock_path]) as acquired:
                assert acquired is None, "API did not retain the shared lock"
            return owned

        with patch.dict(os.environ, fixture_env, clear=True):
            config, env = deployment.configuration(config_path)
        first = start()
        accepted, accepted_body, accepted_headers = mutation(url, "accepted-before-stop", "Accepted synthetic work")
        assert accepted["status"] == 201, accepted
        unknown_body = json.dumps({"repository_id": "fixture", "goal": "Unknown caller outcome"})
        unknown_headers = [("Authorization", "Bearer " + h.OWNER), ("Idempotency-Key", "unknown-before-stop"),
                           ("Content-Type", "application/json")]
        before_unknown = receipt_rows(database)
        with socket.create_connection(("127.0.0.1", port), timeout=10) as client:
            wire = (f"POST /v1/items HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                    + "".join(f"{key}: {value}\r\n" for key, value in unknown_headers)
                    + f"Content-Length: {len(unknown_body.encode())}\r\nConnection: close\r\n\r\n"
                    + unknown_body)
            client.sendall(wire.encode())
            # Intentionally receive no HTTP response: caller outcome remains unknown.
            wait_for(lambda: len(receipt_rows(database)) > len(before_unknown), "unknown request durability witness")
        identities = durable_identities(database)
        receipts = receipt_rows(database)
        first_receipt = w.stop(config_path, config, env, task=name, record=record,
                               receipt=args.output / "first-stop.json")
        assert first_receipt["stopped"] and first_receipt["root_exit_before_child"], first_receipt
        assert w.task_control("inspect", task=unrelated_name)["task"] == unrelated
        assert not healthy(), "Original API still serving"
        assert receipt_rows(database) == receipts, "Stopping changed durable receipts"
        record.rename(args.output / "first-ownership.json")
        second = start()
        assert first["containment_id"] != second["containment_id"], "Restart reused containment"
        replay = h.wire(url, "POST", "/v1/items", accepted_headers, accepted_body)
        assert h.comparable(replay) == h.comparable(accepted), "Accepted original-key replay changed"
        reconciled = h.wire(url, "POST", "/v1/items", unknown_headers, unknown_body)
        assert reconciled["status"] == 201, reconciled
        assert receipt_rows(database) == receipts, "Original-key reconciliation duplicated a write"
        assert durable_identities(database) == identities, "Restart/replay changed durable identities"
        second_receipt = w.stop(config_path, config, env, task=name, record=record,
                                receipt=args.output / "second-stop.json")
        assert second_receipt["stopped"]
        assert w.task_control("inspect", task=unrelated_name)["task"] == unrelated
        assert receipt_rows(database) == receipts
        with any_file_lock([lock_path]) as acquired:
            assert acquired is not None, "Shared lock was not released"
        record.rename(args.output / "second-ownership.json")
        with closing(sqlite3.connect(database)) as store:
            assert store.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        (args.output / "proof.json").write_text(json.dumps({
            "passed": True, "scope": "owned disposable cmd/deploy/Go task; no production adoption",
            "source": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
            "binary_sha256": h.digest(args.binary.read_bytes()),
            "accepted_original_key_replay": True, "transport_unknown_original_key_reconciliation": True,
            "durable_identities_unchanged": True, "receipt_count": len(receipts),
            "unrelated_task_preserved": True, "database_rollback": False,
            "historical_mismatch_cause": "UNKNOWN",
        }, indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--fixture-supervise", action="store_true")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--record", type=Path)
    parser.add_argument("--task")
    args = parser.parse_args()
    if args.fixture_supervise:
        # Scheduler actions do not inherit their caller's process environment.
        # Only the already-gated parent registers this disposable worker action.
        marker = args.config.parent.parent / "owned" / "DISPOSABLE"
        if (os.name != "nt" or not args.task.startswith("\\PhalanxSynthetic-")
                or not marker.is_file() or marker.read_bytes() != b"phalanx-go-conformance-v1\n"):
            raise SystemExit("Owned disposable task fixture required")
        os.environ["PYTHONPATH"] = str(ROOT)
        for key in list(os.environ):
            if key.startswith("PHALANX_") and key != "PHALANX_WINDOWS_TASK_ISOLATION":
                del os.environ[key]
        try:
            return fixture_supervise(args)
        except BaseException as error:
            details = {"error": str(error), "type": type(error).__name__,
                       "traceback": traceback.format_exc(), "historical_cause": "UNKNOWN"}
            args.record.with_suffix(".failure.json").write_text(
                json.dumps(details, indent=2), encoding="utf-8")
            raise
    task_rehearsal(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Opt-in foreground Windows API containment, never adoption of a running tree."""

from contextlib import ExitStack
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

from .json_utils import unique_object
from .workspaces import any_file_lock


class OwnershipError(ValueError):
    """Ownership or exclusion could not be established; do not start a replacement."""

    def __init__(self, message, *, diagnostic=None):
        super().__init__(message)
        self.diagnostic = diagnostic


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _native():
    if os.name != "nt":
        raise OwnershipError("Windows API supervision requires Windows")
    return Native()


class _Startup(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD), ("reserved", wintypes.LPWSTR),
        ("desktop", wintypes.LPWSTR), ("title", wintypes.LPWSTR),
        ("x", wintypes.DWORD), ("y", wintypes.DWORD),
        ("x_size", wintypes.DWORD), ("y_size", wintypes.DWORD),
        ("x_chars", wintypes.DWORD), ("y_chars", wintypes.DWORD),
        ("fill", wintypes.DWORD), ("flags", wintypes.DWORD),
        ("show", wintypes.WORD), ("reserved_size", wintypes.WORD),
        ("reserved_bytes", ctypes.POINTER(ctypes.c_ubyte)),
        ("stdin", wintypes.HANDLE), ("stdout", wintypes.HANDLE),
        ("stderr", wintypes.HANDLE),
    ]


class _ProcessInfo(ctypes.Structure):
    _fields_ = [("process", wintypes.HANDLE), ("thread", wintypes.HANDLE),
                ("pid", wintypes.DWORD), ("tid", wintypes.DWORD)]


class _BasicLimits(ctypes.Structure):
    _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                ("flags", wintypes.DWORD), ("minimum", ctypes.c_size_t),
                ("maximum", ctypes.c_size_t), ("active", wintypes.DWORD),
                ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                ("scheduling", wintypes.DWORD)]


class _IO(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in
                ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]


class _Limits(ctypes.Structure):
    _fields_ = [("basic", _BasicLimits), ("io", _IO),
                ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]


class _Completion(ctypes.Structure):
    _fields_ = [("key", ctypes.c_void_p), ("port", wintypes.HANDLE)]


class Native:
    """Small handle-only Win32 boundary; no termination operation accepts a PID."""

    def __init__(self):
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        definitions = {
            "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            "GetCurrentProcess": ([], wintypes.HANDLE),
            "GetProcessId": ([wintypes.HANDLE], wintypes.DWORD),
            "GetExitCodeProcess": ([wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
            "QueryFullProcessImageNameW": ([wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                            ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
            "DuplicateHandle": ([wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE,
                                  ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD,
                                  wintypes.BOOL, wintypes.DWORD], wintypes.BOOL),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
            "WaitForSingleObject": ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
            "GetProcessTimes": ([wintypes.HANDLE] + [ctypes.POINTER(ctypes.c_uint64)] * 4, wintypes.BOOL),
            "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            "SetInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD],
                                       wintypes.BOOL),
            "QueryInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                           wintypes.DWORD, ctypes.c_void_p], wintypes.BOOL),
            "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            "IsProcessInJob": ([wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)],
                               wintypes.BOOL),
            "TerminateJobObject": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            "TerminateProcess": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            "ResumeThread": ([wintypes.HANDLE], wintypes.DWORD),
            "CreateProcessW": ([wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
                                wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
                                ctypes.POINTER(_Startup), ctypes.POINTER(_ProcessInfo)], wintypes.BOOL),
            "CreateIoCompletionPort": ([wintypes.HANDLE, wintypes.HANDLE, ctypes.c_size_t, wintypes.DWORD],
                                       wintypes.HANDLE),
            "GetQueuedCompletionStatus": ([wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD),
                                           ctypes.POINTER(ctypes.c_size_t),
                                           ctypes.POINTER(ctypes.c_void_p), wintypes.DWORD], wintypes.BOOL),
        }
        for name, (arguments, result) in definitions.items():
            function = getattr(self.api, name)
            function.argtypes, function.restype = arguments, result

    def check(self, result, operation):
        if not result:
            raise OwnershipError(f"{operation} failed (Windows error {ctypes.get_last_error()})")
        return result

    def close(self, handle):
        self.check(self.api.CloseHandle(handle), "Close owned handle")

    def open(self, pid, *, duplicate=False):
        return self.check(self.api.OpenProcess(0x100000 | 0x1000 | (0x40 if duplicate else 0), False, pid),
                          "Retain original process")

    def duplicate(self, source_process, source_handle):
        handle = wintypes.HANDLE()
        self.check(self.api.DuplicateHandle(source_process, source_handle,
                   self.api.GetCurrentProcess(), ctypes.byref(handle), 0, False, 2),
                   "Duplicate original supervisor-owned handle")
        return handle.value

    def created(self, handle):
        values = [ctypes.c_uint64() for _ in range(4)]
        self.check(self.api.GetProcessTimes(handle, *(ctypes.byref(v) for v in values)),
                   "Read native process creation FILETIME")
        return values[0].value

    def image(self, handle):
        size = wintypes.DWORD(32768)
        path = ctypes.create_unicode_buffer(size.value)
        self.check(self.api.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)),
                   "Read original native executable")
        return digest(os.path.normcase(os.path.abspath(path.value)))

    def exited(self, handle):
        result = self.api.WaitForSingleObject(handle, 0)
        if result not in (0, 258):
            raise OwnershipError("Original process handle wait failed")
        return result == 0

    def wait(self, handle):
        # No API/task stop deadline has been approved. Never import Git's cleanup budget.
        if self.api.WaitForSingleObject(handle, 0xFFFFFFFF) != 0:
            raise OwnershipError("Wait for original process exit failed")

    def exit_code(self, handle):
        code = wintypes.DWORD()
        self.check(self.api.GetExitCodeProcess(handle, ctypes.byref(code)), "Read original root exit code")
        return code.value

    def members(self, job):
        size = 1024
        while True:
            buffer = ctypes.create_string_buffer(size)
            if self.api.QueryInformationJobObject(job, 3, buffer, size, None):
                counts = (wintypes.DWORD * 2).from_buffer(buffer)
                if counts[0] != counts[1]:
                    raise OwnershipError("Incomplete owned Job member enumeration")
                return list((ctypes.c_size_t * counts[1]).from_buffer(buffer, 8))
            if ctypes.get_last_error() != 234:
                self.check(False, "Enumerate owned Job")
            size *= 2

    def member(self, job, handle):
        result = wintypes.BOOL()
        self.check(self.api.IsProcessInJob(handle, job, ctypes.byref(result)), "Verify owned Job membership")
        return bool(result.value)


@dataclass
class Original:
    native: Native
    pid: int
    handle: int
    created: int
    expected: dict

    @classmethod
    def capture(cls, native, pid, query, *, expected=None, duplicate=False):
        if type(pid) is not int or not 1 <= pid <= 0xFFFFFFFF:
            raise OwnershipError("Invalid original process identity")
        handle = native.open(pid, duplicate=duplicate)
        created, rows = None, []
        try:
            created = native.created(handle)
            rows = query([pid])
            if len(rows) != 1:
                raise OwnershipError("Capture identity unavailable or ambiguous")
            row = rows[0]
            if row.get("pid") != pid:
                raise OwnershipError("Capture returned a different process")
            baseline = dict(row) if expected is None else dict(expected)
            if baseline.get("created") != created:
                # Persist native precision, never round the retained FILETIME to CIM precision.
                if expected is not None or not _same_filetime(created, baseline.get("created")):
                    raise OwnershipError("Capture creation identity changed")
            baseline["created"] = created
            result = cls(native, pid, handle, created, baseline)
            receipt = result.compare(rows)
            if receipt["classification"] != "active-match":
                raise OwnershipError("Capture identity is not an active complete match")
            return result
        except BaseException as error:
            if isinstance(error, OwnershipError) and created is not None:
                baseline = expected if expected is not None else {"pid": pid, "created": created}
                error.diagnostic = cls(native, pid, handle, created, baseline).compare(rows)
            native.close(handle)
            raise

    @classmethod
    def retained(cls, native, source, handle, expected):
        copied = native.duplicate(source, handle)
        try:
            if (native.api.GetProcessId(copied) != expected["pid"]
                    or native.created(copied) != expected["created"]):
                raise OwnershipError("Duplicated original process identity changed")
            return cls(native, expected["pid"], copied, expected["created"], dict(expected))
        except BaseException:
            native.close(copied)
            raise

    def compare(self, rows, *, query_error=None):
        expected_native = _safe_field("created", self.expected.get("created"))
        fields = [{"field": "native_created_filetime", "expected": expected_native,
                   "observed": self.created,
                   "status": "match" if expected_native == self.created else "changed"}]
        observed = rows[0] if len(rows) == 1 else {}
        for name in ("pid", "parent", "created", "executable_hash", "arguments_hash"):
            expected, actual = (_safe_field(name, source.get(name)) for source in (self.expected, observed))
            present = expected is not None and actual is not None
            same = (_same_filetime(expected, actual) if name == "created"
                    else expected == actual)
            fields.append({"field": name, "expected": expected, "observed": actual,
                           "status": "unavailable" if not present else "match" if same else "changed"})
        exited = self.native.exited(self.handle)
        if exited:
            classification = "original-exit-proven"
        elif query_error:
            classification = "query-error"
        elif len(rows) > 1:
            classification = "ambiguous"
        elif any(field["status"] == "changed" for field in fields):
            classification = "changed"
        elif all(field["status"] == "match" for field in fields):
            classification = "active-match"
        else:
            classification = "unavailable"
        return {"pid": self.pid, "native_created_filetime": self.created,
                "original_handle_signaled": exited, "classification": classification,
                "query_error_hash": digest(query_error) if query_error else None, "fields": fields}

    def read(self, query):
        try:
            rows = query([self.pid])
        except OwnershipError as error:
            return self.compare([], query_error=type(error).__name__)
        return self.compare(rows)

    def close(self):
        self.native.close(self.handle)


def _same_filetime(native, cim):
    # Win32_Process CreationDate loses at most one microsecond (10 FILETIME ticks).
    return (type(native) is int and type(cim) is int and native > 0 and cim > 0
            and abs(native - cim) <= 10)


def _safe_field(name, value):
    if name.endswith("_hash"):
        return value if isinstance(value, str) and re.fullmatch("[0-9a-f]{64}", value) else None
    return value if type(value) is int and value >= (0 if name == "parent" else 1) else None


class Job:
    def __init__(self, native):
        self.native = native
        self.identity = uuid.uuid4().hex
        self.handle = native.check(native.api.CreateJobObjectW(None, None), "Create owned Job")
        self.root = None
        self.root_identity = None
        self.port = None
        try:
            limits = _Limits()
            limits.basic.flags = 0x2000  # KILL_ON_JOB_CLOSE, no breakaway flags.
            native.check(native.api.SetInformationJobObject(
                self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)), "Contain future children")
            self.port = native.check(native.api.CreateIoCompletionPort(
                wintypes.HANDLE(-1), None, 0, 1), "Create owned Job completion port")
            completion = _Completion(1, self.port)
            native.check(native.api.SetInformationJobObject(
                self.handle, 7, ctypes.byref(completion), ctypes.sizeof(completion)),
                "Associate owned Job completion")
        except BaseException:
            self.close()
            raise

    def launch(self, command, env, cwd):
        startup, process = _Startup(), _ProcessInfo()
        startup.cb = ctypes.sizeof(startup)
        environment = ctypes.create_unicode_buffer(
            "\0".join(f"{key}={value}" for key, value in sorted(env.items(), key=lambda p: p[0].upper()))
            + "\0\0")
        line = ctypes.create_unicode_buffer(subprocess.list2cmdline(command))
        self.native.check(self.native.api.CreateProcessW(
            command[0], line, None, None, False, 0x4 | 0x400, environment,
            str(cwd), ctypes.byref(startup), ctypes.byref(process)), "Create suspended API root")
        try:
            self.native.check(self.native.api.AssignProcessToJobObject(self.handle, process.process),
                              "Assign suspended API root")
            executable_hash = digest(os.path.normcase(os.path.abspath(command[0])))
            if self.native.image(process.process) != executable_hash:
                raise OwnershipError("Suspended root executable identity changed")
            # CIM command lines need the user-mode loader to run. The native
            # CreateProcess result binds our exact command before any thread runs.
            self.root_identity = {
                "pid": process.pid, "parent": os.getpid(),
                "created": self.native.created(process.process),
                "executable_hash": executable_hash,
                "arguments_hash": digest("\0".join(command[1:])),
            }
            if self.native.api.ResumeThread(process.thread) == 0xFFFFFFFF:
                self.native.check(False, "Resume contained API root")
            self.root = process.process
            return process.pid
        except BaseException:
            # This exact native handle is our still-suspended child, never a PID lookup.
            self.native.check(self.native.api.TerminateProcess(process.process, 1), "Clean up suspended root")
            self.native.wait(process.process)
            self.native.close(process.process)
            raise
        finally:
            self.native.close(process.thread)

    def wait_empty(self):
        while self.native.members(self.handle):
            message, key, process = wintypes.DWORD(), ctypes.c_size_t(), ctypes.c_void_p()
            self.native.check(self.native.api.GetQueuedCompletionStatus(
                self.port, ctypes.byref(message), ctypes.byref(key), ctypes.byref(process), 0xFFFFFFFF),
                "Wait for owned Job completion")
        if self.root is not None:
            self.native.wait(self.root)
            return self.native.exit_code(self.root)
        return 0

    def close(self):
        errors = []
        if self.handle is not None:
            try:
                self.native.check(self.native.api.TerminateJobObject(self.handle, 1),
                                  "Clean up original owned Job")
                self.wait_empty()
            except OwnershipError as error:
                errors.append(str(error))
        for name in ("handle", "root", "port"):
            handle = getattr(self, name, None)
            if handle is not None:
                try:
                    self.native.close(handle)
                except OwnershipError as error:
                    errors.append(str(error))
                setattr(self, name, None)
        if errors:
            raise OwnershipError("; ".join(errors))


def task_control(operation, *, task, pids=(), expected=None, port=None):
    request = {"operation": operation, "task": task, "pids": list(pids),
               "expected": expected, "port": port}
    system = Path(os.environ["SystemRoot"]) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    result = subprocess.run(
        [str(system), "-NoProfile", "-NonInteractive", "-File",
         str(Path(__file__).with_name("windows_task.ps1"))],
        input=json.dumps(request), encoding="utf-8", capture_output=True, check=False)
    if result.returncode:
        raise OwnershipError("Windows ownership query/control failed; error output SHA256="
                             + digest(result.stderr))
    try:
        return json.loads(result.stdout, object_pairs_hook=unique_object)
    except (ValueError, UnicodeError):
        raise OwnershipError("Invalid Windows ownership query/control receipt") from None


def _query(pids):
    return task_control("processes", task=None, pids=pids)["processes"]


def _write_new(path, value):
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2)
        handle.write("\n")


def supervise(config_path, config, env, *, task, record, engine, go_binary):
    native = _native()
    if not record.is_absolute() or record.exists():
        raise OwnershipError("Use a new absolute ownership record in an operator-private directory")
    database = Path(env.get("PHALANX_DB", ""))
    if not database.is_absolute() or not database.is_file():
        raise OwnershipError("Supervision requires the existing absolute database")
    # No automatic task registration, action update, settings change or live adoption.
    initial = task_control("inspect", task=task)
    proof = initial["task"]
    with ExitStack() as stack:
        supervisor = Original.capture(native, os.getpid(), _query)
        stack.callback(supervisor.close)
        lineage = []
        current = supervisor
        while True:
            lineage.append({"identity": current.expected, "handle": current.handle})
            if (current.expected["executable_hash"] == proof["action_executable_hash"]
                    and current.expected["arguments_hash"] == proof["action_arguments_hash"]):
                break
            parent = current.expected["parent"]
            if not parent or parent == proof["engine_pid"] or len(lineage) >= 16:
                raise OwnershipError("Sole task action is not the supervisor's proven ancestor")
            current = Original.capture(native, parent, _query)
            stack.callback(current.close)
        # EnginePID is only an association check, never a termination target or ownership grant.
        if proof["engine_pid"] not in (current.pid, current.expected["parent"]):
            raise OwnershipError("Task engine/action lineage is not proven")
        task_engine = current
        if proof["engine_pid"] != current.pid:
            task_engine = Original.capture(native, proof["engine_pid"], _query)
            stack.callback(task_engine.close)
            if task_engine.created > current.created:
                raise OwnershipError("Task engine PID was reused after action creation")
        if task_control("inspect", task=task)["task"] != proof:
            raise OwnershipError("Task changed before contained launch")
        preflight = task_control("excluded", task=task, port=config["server"]["port"])
        if preflight["listeners"]:
            raise OwnershipError("Configured listener is already in use; no API launched")
        lock_path = database.with_name(database.name + ".service.lock")
        if not lock_path.is_file():
            raise OwnershipError("Existing shared service lock must be present before supervision")
        with any_file_lock([lock_path]) as acquired:
            if acquired is None:
                raise OwnershipError("Existing API still owns the shared service lock; no API launched")
        job = Job(native)
        stack.callback(job.close)
        command = [sys.executable, "-P", "-m", "phalanx", "deploy", "run",
                   "--config", str(config_path), "server", "--engine", engine]
        if go_binary is not None:
            command.extend(["--go-binary", str(go_binary)])
        root_pid = job.launch(command, env, Path.cwd())
        _write_new(record, {"format": 1, "task_path": task, "task": proof,
                            "supervisor": supervisor.expected, "lineage": lineage,
                            "engine": {"identity": task_engine.expected, "handle": task_engine.handle},
                            "job_handle": job.handle, "containment_id": job.identity,
                            "root_handle": job.root, "root": job.root_identity,
                            "root_pid": root_pid,
                            "config_hash": digest(str(config_path)),
                            "database_path_hash": digest(str(database.resolve())),
                            "port": config["server"]["port"], "stop_authorized": False,
                            "scope": "owned-from-suspended-launch; not uninstrumented adoption"})
        returncode = job.wait_empty()
    return returncode


def stop(config_path, config, env, *, task, record, receipt):
    native = _native()
    if not receipt.is_absolute() or receipt.exists():
        raise OwnershipError("Use a new absolute stop receipt path")
    result = {"format": 1, "stopped": False, "request_outcomes": "transport-unknown",
              "recovery": "Reconcile current data and original request keys; never restore an old database",
              "nodes": [], "task_stopped": False, "job_empty": False,
              "listener_excluded": False, "shared_lock_released": False}
    try:
        try:
            owned = json.loads(record.read_text(encoding="utf-8"), object_pairs_hook=unique_object)
        except (OSError, ValueError, UnicodeError):
            raise OwnershipError("No valid original containment record; uninstrumented task unsupported") from None
        database = Path(env.get("PHALANX_DB", "")).resolve()
        if (owned.get("format") != 1 or owned.get("task_path") != task
                or owned.get("config_hash") != digest(str(config_path))
                or owned.get("database_path_hash") != digest(str(database))
                or owned.get("port") != config["server"]["port"]):
            raise OwnershipError("Ownership record does not match the explicit deployment")
        proof = task_control("inspect", task=task)["task"]
        if proof != owned["task"]:
            raise OwnershipError("Original task definition or instance changed")
        result["task"] = proof
        with ExitStack() as stack:
            originals = []
            supervisor = Original.capture(native, owned["supervisor"]["pid"], _query,
                                          expected=owned["supervisor"], duplicate=True)
            originals.append(supervisor)
            stack.callback(supervisor.close)
            if not owned["lineage"] or owned["lineage"][0]["identity"] != owned["supervisor"]:
                raise OwnershipError("Original supervisor lineage unavailable")
            for entry in owned["lineage"][1:]:
                original = Original.retained(native, supervisor.handle, entry["handle"], entry["identity"])
                originals.append(original)
                stack.callback(original.close)
            engine_entry = owned["engine"]
            engine = Original.retained(native, supervisor.handle, engine_entry["handle"],
                                       engine_entry["identity"])
            stack.callback(engine.close)
            result["engine"] = engine.read(_query)
            if result["engine"]["classification"] != "active-match":
                raise OwnershipError("Original task engine association is stale or unavailable")
            # Duplicate from the verified original supervisor, not a name or new PID.
            job = native.duplicate(supervisor.handle, owned["job_handle"])
            stack.callback(native.close, job)
            root = Original.retained(native, supervisor.handle, owned["root_handle"], owned["root"])
            stack.callback(root.close)
            originals.append(root)
            if not native.exited(root.handle) and not native.member(job, root.handle):
                raise OwnershipError("Active original root is not in the original Job")
            for pid in native.members(job):
                if pid == root.pid and not native.exited(root.handle):
                    continue
                original = Original.capture(native, pid, _query)
                stack.callback(original.close)
                if not native.member(job, original.handle):
                    raise OwnershipError("Captured active process is not in the original Job")
                originals.append(original)
            result["root_exit_before_child"] = native.exited(root.handle)
            for original in originals:
                diagnostic = original.read(_query)
                result["nodes"].append(diagnostic)
                if diagnostic["classification"] not in ("active-match", "original-exit-proven"):
                    raise OwnershipError("Active original identity changed or unavailable; stop refused")
            if task_control("inspect", task=task)["task"] != proof:
                raise OwnershipError("Task changed before stop")
            # Retaining the duplicate Job prevents task-root exit from dropping our
            # containment proof. Task control never targets its shared engine.
            task_control("stop", task=task, expected=proof)
            result["task_stopped"] = True
            # Stop only the Job created before this root could execute. Future ordinary
            # CreateProcess descendants remain owned even if their parents exit first.
            native.check(native.api.TerminateJobObject(job, 1), "Stop original owned API Job")
            for original in originals[len(owned["lineage"]):]:
                native.wait(original.handle)
            if native.members(job):
                raise OwnershipError("Owned Job completion is not proven; do not start another API")
            result["job_empty"] = True
            for original in originals[:len(owned["lineage"])]:
                native.wait(original.handle)
            after = task_control("excluded", task=task, port=config["server"]["port"])
            if not after["task_excluded"] or after["listeners"]:
                raise OwnershipError("Task/listener exclusion is not proven; do not start another API")
            result["listener_excluded"] = True
            lock_path = database.with_name(database.name + ".service.lock")
            if not lock_path.is_file():
                raise OwnershipError("Original shared service lock is missing")
            with any_file_lock([lock_path]) as acquired:
                if acquired is None:
                    raise OwnershipError("Shared service lock is still owned")
                result["shared_lock_released"] = True
                if native.members(job):
                    raise OwnershipError("Owned Job is no longer empty")
                result["original_handles_exited"] = all(native.exited(node.handle) for node in originals)
                if not result["original_handles_exited"]:
                    raise OwnershipError("An original native process handle remains active")
                for diagnostic in result["nodes"]:
                    diagnostic["final_original_handle_signaled"] = True
            result["shared_lock_probe_closed"] = True
            result["stopped"] = True
        return result
    except BaseException as error:
        result["stopped"] = False
        if isinstance(error, OwnershipError) and error.diagnostic is not None:
            result["nodes"].append(error.diagnostic)
        result["failure"] = str(error) if isinstance(error, OwnershipError) else type(error).__name__
        raise
    finally:
        _write_new(receipt, result)

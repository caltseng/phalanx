from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import Mock, patch

from phalanx import windows_supervisor as w
from timing import WAIT_SECONDS


class PublicationTest(unittest.TestCase):
    def test_final_record_is_absent_while_json_write_is_paused(self):
        for name, value in (("ownership.json", {"format": 1, "job_handle": 901}),
                            ("stop.json", {"format": 1, "stopped": False}),
                            ("stop.json", {"format": 1, "stopped": True})):
            with self.subTest(value=value), TemporaryDirectory() as temporary:
                path = Path(temporary) / name
                paused, resume = threading.Event(), threading.Event()
                dump = json.dump

                def paused_dump(data, handle, **options):
                    handle.write("{")
                    handle.flush()
                    paused.set()
                    if not resume.wait(WAIT_SECONDS):
                        raise AssertionError("Publication fixture was not resumed")
                    handle.seek(0)
                    dump(data, handle, **options)

                with ThreadPoolExecutor(max_workers=1) as executor, \
                        patch.object(w.json, "dump", side_effect=paused_dump):
                    future = executor.submit(w._write_new, path, value)
                    try:
                        self.assertTrue(paused.wait(WAIT_SECONDS))
                        self.assertFalse(path.exists(), "Incomplete JSON became a final record")
                    finally:
                        resume.set()
                    future.result(timeout=WAIT_SECONDS)
                self.assertEqual(json.loads(path.read_text()), value)
                self.assertEqual(list(path.parent.iterdir()), [path])

    def test_concurrent_publishers_have_one_winner_and_never_replace_it(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "record.json"
            barrier = threading.Barrier(2)
            link = os.link

            def publish(source, destination):
                barrier.wait(timeout=WAIT_SECONDS)
                link(source, destination)

            with ThreadPoolExecutor(max_workers=2) as executor, \
                    patch.object(w.os, "link", side_effect=publish):
                futures = [executor.submit(w._write_new, path, {"publisher": number})
                           for number in range(2)]
                outcomes = []
                for future in futures:
                    try:
                        future.result(timeout=WAIT_SECONDS)
                        outcomes.append("won")
                    except FileExistsError:
                        outcomes.append("lost")
            self.assertCountEqual(outcomes, ["won", "lost"])
            before = path.read_bytes()
            with self.assertRaises(FileExistsError):
                w._write_new(path, {"publisher": "replacement"})
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_write_flush_close_sync_and_publication_faults_leave_no_final_record(self):
        for fault in ("create", "write", "flush", "close", "sync", "publish", "interrupt"):
            with self.subTest(fault=fault), TemporaryDirectory() as temporary:
                path = Path(temporary) / "record.json"
                actual_open = Path.open

                @contextmanager
                def open_file(candidate, *arguments, **options):
                    if fault == "create":
                        raise OSError("injected create failure")
                    with actual_open(candidate, *arguments, **options) as handle:
                        proxy = Mock(wraps=handle)
                        if fault in ("write", "flush"):
                            getattr(proxy, fault).side_effect = OSError(f"injected {fault} failure")
                        yield proxy
                    if fault == "close":
                        raise OSError("injected close failure")

                with ExitStack() as stack:
                    stack.enter_context(patch.object(Path, "open", open_file))
                    if fault == "sync":
                        stack.enter_context(patch.object(w.os, "fsync", side_effect=OSError("injected sync failure")))
                    if fault == "publish":
                        stack.enter_context(patch.object(w.os, "link", side_effect=OSError("injected publish failure")))
                    if fault == "interrupt":
                        stack.enter_context(patch.object(w.json, "dump", side_effect=KeyboardInterrupt))
                    with self.assertRaises(KeyboardInterrupt if fault == "interrupt" else OSError):
                        w._write_new(path, {"stopped": True})
                self.assertFalse(path.exists())
                self.assertEqual(list(path.parent.iterdir()), [])

    def test_cleanup_failure_surfaces_without_removing_final_or_foreign_files(self):
        for published in (False, True):
            with self.subTest(published=published), TemporaryDirectory() as temporary:
                path = Path(temporary) / "record.json"
                foreign = path.parent / "unrelated.tmp"
                foreign.write_text("foreign")
                with ExitStack() as stack:
                    unlink = stack.enter_context(patch.object(Path, "unlink", autospec=True,
                                                              side_effect=OSError("injected cleanup failure")))
                    if not published:
                        stack.enter_context(patch.object(w.os, "link",
                                                         side_effect=OSError("injected publication failure")))
                    with self.assertRaisesRegex(OSError, "cleanup failure") as caught:
                        w._write_new(path, {"stopped": False})
                self.assertEqual(foreign.read_text(), "foreign")
                leftovers = [entry for entry in path.parent.iterdir()
                             if entry not in (path, foreign)]
                self.assertEqual(len(leftovers), 1)
                unlink.assert_called_once_with(leftovers[0])
                self.assertEqual(json.loads(leftovers[0].read_text()), {"stopped": False})
                if published:
                    self.assertEqual(json.loads(path.read_text()), {"stopped": False})
                else:
                    self.assertFalse(path.exists())
                    self.assertRegex(str(caught.exception.__context__), "publication failure")
                leftovers[0].unlink()

    def test_temporary_name_collision_never_deletes_unowned_file(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "record.json"
            token = Mock(hex="fixed")
            foreign = path.with_name(".record.json.fixed.tmp")
            foreign.write_text("foreign")
            with patch.object(w.uuid, "uuid4", return_value=token):
                with self.assertRaises(FileExistsError):
                    w._write_new(path, {"stopped": True})
            self.assertFalse(path.exists())
            self.assertEqual(foreign.read_text(), "foreign")

    def test_interruption_at_link_leaves_final_absent_or_complete(self):
        for linked in (False, True):
            with self.subTest(linked=linked), TemporaryDirectory() as temporary:
                path = Path(temporary) / "stop.json"
                link = os.link

                def interrupted_link(source, destination):
                    self.assertFalse(destination.exists())
                    self.assertEqual(json.loads(source.read_text()), {"stopped": False})
                    if linked:
                        link(source, destination)
                    raise KeyboardInterrupt

                with patch.object(w.os, "link", side_effect=interrupted_link):
                    with self.assertRaises(KeyboardInterrupt):
                        w._write_new(path, {"stopped": False})
                if linked:
                    self.assertEqual(json.loads(path.read_text()), {"stopped": False})
                    self.assertEqual(list(path.parent.iterdir()), [path])
                else:
                    self.assertEqual(list(path.parent.iterdir()), [])


class IdentityTest(unittest.TestCase):
    def setUp(self):
        self.native = Mock()
        self.native.exited.return_value = False
        self.row = {"pid": 42, "parent": 21, "created": 134402913978617619,
                    "executable_hash": w.digest("fixture.exe"),
                    "arguments_hash": w.digest("private-marker")}
        self.original = w.Original(self.native, 42, 900, self.row["created"], dict(self.row))

    def test_exact_native_filetime_and_only_cim_microsecond_loss(self):
        for delta in (-10, -9, 0, 9, 10):
            with self.subTest(delta=delta):
                result = self.original.compare([dict(self.row, created=self.row["created"] + delta)])
                self.assertEqual(result["classification"], "active-match")
                self.assertEqual(result["native_created_filetime"], self.row["created"])
        for delta in (-11, 11, 10_000):
            with self.subTest(delta=delta):
                self.assertEqual(self.original.compare([
                    dict(self.row, created=self.row["created"] + delta)])["classification"], "changed")

    def test_changed_missing_ambiguous_stale_and_query_error_are_not_authority(self):
        for field in ("pid", "parent", "created", "executable_hash", "arguments_hash"):
            with self.subTest(field=field):
                changed = dict(self.row)
                changed[field] = changed[field] + 100 if isinstance(changed[field], int) else w.digest("other")
                self.assertEqual(self.original.compare([changed])["classification"], "changed")
                changed[field] = None
                self.assertEqual(self.original.compare([changed])["classification"], "unavailable")
        self.assertEqual(self.original.compare([])["classification"], "unavailable")
        self.assertEqual(self.original.compare([self.row, self.row])["classification"], "ambiguous")
        result = self.original.compare([], query_error="private-error-marker")
        self.assertEqual(result["classification"], "query-error")
        self.assertNotIn("private", json.dumps(result))

    def test_original_exit_wins_over_stale_cim_but_never_selects_current_pid(self):
        self.native.exited.return_value = True
        reused = dict(self.row, created=self.row["created"] + 10000, parent=100)
        for rows in ([], [reused], [self.row, reused]):
            with self.subTest(rows=rows):
                result = self.original.compare(rows, query_error="query failed")
                self.assertEqual(result["classification"], "original-exit-proven")
                self.assertTrue(result["original_handle_signaled"])
        self.native.assert_has_calls([unittest.mock.call.exited(900)] * 3)
        self.assertNotIn("stop_authorized", result)

    def test_partial_capture_releases_handle_and_rejects_reuse(self):
        self.native.open.return_value = 900
        self.native.created.return_value = self.row["created"]
        for rows in ([], [self.row, self.row], [dict(self.row, pid=999)],
                     [dict(self.row, created=self.row["created"] + 100)]):
            self.native.close.reset_mock()
            with self.subTest(rows=rows), self.assertRaises(w.OwnershipError):
                w.Original.capture(self.native, 42, lambda pids: rows)
            self.native.close.assert_called_once_with(900)
        with self.assertRaises(w.OwnershipError):
            w.Original.capture(self.native, 42, lambda pids: [self.row],
                               expected=dict(self.row, created=self.row["created"] - 1))

    def test_cleanup_failure_is_visible_not_a_success_receipt(self):
        self.native.open.return_value = 900
        self.native.created.return_value = self.row["created"]
        self.native.close.side_effect = w.OwnershipError("close failed")
        with self.assertRaisesRegex(w.OwnershipError, "close failed"):
            w.Original.capture(self.native, 42, lambda pids: [])

    def test_suspended_root_handles_close_even_when_termination_or_wait_fails(self):
        for termination_failed in (False, True):
            with self.subTest(termination_failed=termination_failed):
                native = Mock()

                def check(result, operation):
                    if not result:
                        raise w.OwnershipError(operation)
                    return result

                def create(*arguments):
                    process = arguments[-1]._obj
                    process.process, process.thread, process.pid = 400, 401, 42
                    return True

                native.check.side_effect = check
                native.api.CreateProcessW.side_effect = create
                native.api.AssignProcessToJobObject.return_value = False
                native.api.TerminateProcess.return_value = not termination_failed
                native.wait.side_effect = w.OwnershipError("cleanup wait failed")
                job = w.Job.__new__(w.Job)
                job.native, job.handle, job.root = native, 123, None
                with self.assertRaises(w.OwnershipError):
                    job.launch([sys.executable, "-c", "pass"], {}, Path.cwd())
                self.assertEqual(native.close.call_args_list,
                                 [unittest.mock.call(400), unittest.mock.call(401)])

    def test_job_handle_cleanup_runs_even_if_completion_wait_is_interrupted(self):
        native = Mock()
        job = w.Job.__new__(w.Job)
        job.native, job.handle, job.root, job.port = native, 123, 400, 456
        job.wait_empty = Mock(side_effect=KeyboardInterrupt)
        with self.assertRaises(KeyboardInterrupt):
            job.close()
        self.assertEqual(native.close.call_args_list,
                         [unittest.mock.call(123), unittest.mock.call(400), unittest.mock.call(456)])
        self.assertIsNone(job.handle)
        self.assertIsNone(job.root)
        self.assertIsNone(job.port)

    def test_untrusted_diagnostic_fields_cannot_echo_command_or_credentials(self):
        unsafe = dict(self.row, executable_hash="raw-private-executable",
                      arguments_hash="raw-private-token")
        original = w.Original(self.native, 42, 900, self.row["created"], unsafe)
        result = original.compare([unsafe])
        self.assertEqual(result["classification"], "unavailable")
        self.assertNotIn("raw-private", json.dumps(result))

    def test_uninstrumented_record_refuses_before_task_control(self):
        with TemporaryDirectory() as temporary, patch.object(w, "_native"), patch.object(w, "task_control") as task:
            root = Path(temporary).resolve()
            receipt = root / "receipt.json"
            with self.assertRaisesRegex(w.OwnershipError, "uninstrumented"):
                w.stop(root / "deployment.json", {"server": {"port": 8000}}, {},
                       task="\\synthetic", record=root / "absent.json", receipt=receipt)
            task.assert_not_called()
            result = json.loads(receipt.read_text())
            self.assertFalse(result["stopped"])
            self.assertEqual(result["request_outcomes"], "transport-unknown")

    def test_supervise_keeps_server_engine_separate_from_task_engine_identity(self):
        for selected, saved, launcher in ((engine, saved, parent)
                                          for engine, saved in ((None, None), ("go", None), ("go", "python"))
                                          for parent in (False, True)):
            with self.subTest(engine=selected, launcher=launcher), TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                database = root / "synthetic.sqlite3"
                database.touch()
                database.with_name(database.name + ".service.lock").touch()
                identity = dict(self.row, pid=os.getpid())
                original = w.Original(self.native, os.getpid(), 900, self.row["created"], identity)
                parent_identity = dict(self.row, pid=identity["parent"], parent=7,
                                       executable_hash=w.digest("venv-launcher.exe"))
                parent = w.Original(self.native, parent_identity["pid"], 899,
                                    self.row["created"], parent_identity)
                action = parent if launcher else original
                proof = {"engine_pid": action.pid, "action_executable_hash": action.expected["executable_hash"],
                         "action_arguments_hash": action.expected["arguments_hash"]}
                job = Mock(handle=901, root=902, identity="owned-fixture", root_identity=self.row)
                job.launch.return_value = 42
                job.wait_empty.return_value = 17

                def control(operation, **arguments):
                    return {"task": proof} if operation == "inspect" else {"listeners": []}

                config = {"server": {"port": 12345}}
                if saved is not None:
                    config["server"]["engine"] = saved
                with patch.object(w, "_native", return_value=self.native), \
                        patch.object(w.Original, "capture", side_effect=[original, parent]), \
                        patch.object(w, "Job", return_value=job), \
                        patch.object(w, "task_control", side_effect=control):
                    code = w.supervise(root / "deployment.json", config,
                                       {"PHALANX_DB": str(database)}, task="\\synthetic",
                                       record=root / "record.json", engine=selected, go_binary=None)
                command = job.launch.call_args.args[0]
                if selected is None:
                    self.assertNotIn("--engine", command)
                else:
                    self.assertEqual(command[command.index("--engine") + 1], selected)
                self.assertEqual(code, 17)
                self.assertEqual(config["server"].get("engine"), saved)

    def test_removed_python_selection_refuses_before_native_task_or_record_changes(self):
        for saved, selected in (("go", "python"), ("python", None), ("python", "python")):
            with self.subTest(saved=saved, selected=selected), TemporaryDirectory() as temporary, \
                    patch.object(w, "_native") as native, patch.object(w, "task_control") as task:
                root = Path(temporary).resolve()
                config = {"server": {"port": 12345, "engine": saved}}
                with self.assertRaisesRegex(ValueError, "Python HTTP server has been removed"):
                    w.supervise(root / "deployment.json", config, {}, task="\\synthetic",
                                record=root / "record.json", engine=selected, go_binary=None)
                native.assert_not_called()
                task.assert_not_called()
                self.assertEqual(list(root.iterdir()), [])
                self.assertEqual(config["server"]["engine"], saved)

    def test_shared_engine_parent_never_substitutes_for_the_executable_action(self):
        identity = dict(self.row, pid=os.getpid())
        original = w.Original(self.native, os.getpid(), 900, self.row["created"], identity)
        parent_identity = dict(self.row, pid=identity["parent"], parent=7)
        parent = w.Original(self.native, parent_identity["pid"], 899, self.row["created"], parent_identity)
        proof = {"engine_pid": parent.pid, "action_executable_hash": w.digest("different-action.exe"),
                 "action_arguments_hash": identity["arguments_hash"]}
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            database = root / "synthetic.sqlite3"
            database.touch()
            with patch.object(w, "_native", return_value=self.native), \
                    patch.object(w.Original, "capture", side_effect=[original, parent]), \
                    patch.object(w, "Job") as job, \
                    patch.object(w, "task_control", return_value={"task": proof}):
                with self.assertRaisesRegex(w.OwnershipError, "not the proven executable action"):
                    w.supervise(root / "deployment.json", {"server": {"port": 12345}},
                                {"PHALANX_DB": str(database)}, task="\\synthetic",
                                record=root / "record.json", engine="go", go_binary=None)
                job.assert_not_called()


@unittest.skipUnless(os.name == "nt", "Win32 native-handle fixtures run in Windows CI")
class WindowsJobTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(TemporaryDirectory())).resolve()
        self.native = w._native()

    def job(self):
        job = w.Job(self.native)
        self.stack.callback(job.close)
        return job

    def wait_file(self, path):
        deadline = time.monotonic() + WAIT_SECONDS
        while not path.exists():
            if time.monotonic() >= deadline:
                self.fail("Owned disposable fixture did not produce its readiness record")
            # Test readiness polling only, never a deployment stop delay.
            time.sleep(.02)
        return json.loads(path.read_text())

    def test_real_cmd_root_exits_before_owned_child_and_unrelated_process_survives(self):
        ready = self.root / "ready.json"
        script = self.root / "child.py"
        script.write_text(
            "import json,os,sys,threading\n"
            "from pathlib import Path\n"
            "Path(sys.argv[1]).write_text(json.dumps({'pid':os.getpid()}))\n"
            "threading.Event().wait()\n", encoding="utf-8")
        batch = self.root / "root.cmd"
        batch.write_text('@echo off\nstart "" /b "' + sys.executable + '" "' + str(script)
                         + '" "' + str(ready) + '"\nexit /b 0\n', encoding="utf-8")
        unrelated = self.job()
        unrelated.launch([sys.executable, "-c", "import threading; threading.Event().wait()"],
                         dict(os.environ), self.root)
        unrelated_handle = unrelated.root
        job = self.job()
        job.launch([os.environ["ComSpec"], "/d", "/c", str(batch)], dict(os.environ), self.root)
        child = self.wait_file(ready)
        self.native.wait(job.root)
        self.assertTrue(self.native.exited(job.root))
        self.assertIn(child["pid"], self.native.members(job.handle))
        original = w.Original.capture(self.native, child["pid"], w._query)
        self.stack.callback(original.close)
        self.native.check(self.native.api.TerminateJobObject(job.handle, 1), "Stop synthetic Job")
        self.native.wait(original.handle)
        job.wait_empty()
        self.assertEqual(self.native.members(job.handle), [])
        self.assertFalse(self.native.exited(unrelated_handle))
        result = original.read(w._query)
        self.assertEqual(result["classification"], "original-exit-proven")

    def test_suspended_capture_failure_cannot_spawn_children_or_leak_job_members(self):
        marker = self.root / "should-not-exist"
        job = self.job()
        with patch.object(self.native, "image", side_effect=w.OwnershipError("partial capture")):
            with self.assertRaisesRegex(w.OwnershipError, "partial capture"):
                job.launch([sys.executable, "-c", "from pathlib import Path; Path(r'"
                            + str(marker) + "').touch()"], dict(os.environ), self.root)
        self.assertEqual(self.native.members(job.handle), [])
        self.assertFalse(marker.exists())

    def test_retain_duplicate_handle_after_natural_exit_and_query_race(self):
        job = self.job()
        job.launch([sys.executable, "-c", "pass"], dict(os.environ), self.root)
        source = self.native.open(os.getpid(), duplicate=True)
        self.stack.callback(self.native.close, source)
        copied = w.Original.retained(self.native, source, job.root, job.root_identity)
        self.stack.callback(copied.close)
        self.native.wait(job.root)
        stale = dict(job.root_identity, parent=999, created=job.root_identity["created"] + 1000)
        self.assertEqual(copied.compare([stale])["classification"], "original-exit-proven")
        self.assertEqual(copied.read(lambda pids: [])["classification"], "original-exit-proven")
        self.assertNotIn("TerminateProcess", str(copied.read(lambda pids: [])))

    def _real_handoff(self, *, cleanup_failure=False):
        record = self.root / "ownership.json"
        database = self.root / "synthetic.sqlite3"
        database.write_bytes(b"owned synthetic lock fixture; not a production database")
        database.with_name(database.name + ".service.lock").touch()
        config_path = self.root / "deployment.json"
        config_path.write_text("{}")
        worker = self.root / "supervisor.py"
        worker.write_text(
            "import json,os,sys\nfrom pathlib import Path\n"
            "sys.path.insert(0,sys.argv[1])\n"
            "from phalanx import windows_supervisor as w\n"
            "root=Path(sys.argv[2]); n=w._native(); j=w.Job(n)\n"
            "s=w.Original.capture(n,os.getpid(),w._query)\n"
            "dump=json.dump\n"
            "def paused_dump(value,handle,**options):\n"
            " handle.write('{'); handle.flush()\n"
            " print('publication-paused',flush=True)\n"
            " if sys.stdin.readline()!='publish\\n': raise RuntimeError('Fixture publication not resumed')\n"
            " handle.seek(0); dump(value,handle,**options)\n"
            "w.json.dump=paused_dump\n"
            "try:\n"
            " p=j.launch([sys.executable,'-c','import threading; threading.Event().wait()'],dict(os.environ),root)\n"
            " w._write_new(root/'ownership.json',{'format':1,'task_path':'\\\\synthetic',"
            "'task':{'fixture':'not-real-task-control'},'supervisor':s.expected,"
            "'lineage':[{'identity':s.expected,'handle':s.handle}],"
            "'engine':{'identity':s.expected,'handle':s.handle},"
            "'job_handle':j.handle,'root_handle':j.root,'root':j.root_identity,'root_pid':p,"
            "'config_hash':w.digest(str(root/'deployment.json')),"
            "'database_path_hash':w.digest(str(root/'synthetic.sqlite3')),'port':12345})\n"
            " j.wait_empty()\n"
            "finally:\n j.close(); s.close()\n", encoding="utf-8")
        process = subprocess.Popen([sys.executable, "-I", str(worker),
                                    str(Path(w.__file__).resolve().parents[1]), str(self.root)],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)

        def cleanup():
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=WAIT_SECONDS)
            process.stdin.close()
            process.stdout.close()

        self.stack.callback(cleanup)
        with ThreadPoolExecutor(max_workers=1) as executor:
            paused = executor.submit(process.stdout.readline)
            try:
                self.assertEqual(paused.result(timeout=WAIT_SECONDS), "publication-paused\n")
                self.assertFalse(record.exists(), "Original handoff exposed incomplete ownership")
            except BaseException:
                cleanup()
                raise
            finally:
                if process.poll() is None:
                    process.stdin.write("publish\n")
                    process.stdin.flush()
        owned = self.wait_file(record)
        real_control = w.task_control
        operations = []

        def control(operation, **arguments):
            if operation == "processes":
                return real_control(operation, **arguments)
            operations.append(operation)
            if operation == "stop":
                process.terminate()  # Exact owned Popen handle, not a task/engine or PID selection.
            if operation == "excluded":
                return {"task_excluded": process.poll() is not None, "listeners": []}
            return {"task": owned["task"]}

        actual_close = w.Original.close
        failed = False

        def close(original):
            nonlocal failed
            actual_close(original)
            if cleanup_failure and not failed:
                failed = True
                raise w.OwnershipError("injected cleanup receipt failure")

        with patch.object(w, "task_control", side_effect=control), patch.object(w.Original, "close", close):
            if cleanup_failure:
                with self.assertRaisesRegex(w.OwnershipError, "cleanup receipt failure"):
                    w.stop(config_path, {"server": {"port": 12345}}, {"PHALANX_DB": str(database)},
                           task="\\synthetic", record=record, receipt=self.root / "stop.json")
                result = json.loads((self.root / "stop.json").read_text())
                self.assertFalse(result["stopped"])
            else:
                result = w.stop(config_path, {"server": {"port": 12345}}, {"PHALANX_DB": str(database)},
                                task="\\synthetic", record=record, receipt=self.root / "stop.json")
                self.assertTrue(result["stopped"], result)
        self.assertTrue(result["original_handles_exited"])
        self.assertTrue(result["job_empty"])
        self.assertEqual(operations.count("stop"), 1)
        self.assertEqual(json.loads((self.root / "stop.json").read_text()), result)

    def test_real_original_handle_handoff_survives_supervisor_stop(self):
        self._real_handoff()

    def test_stop_cleanup_failure_cannot_leave_a_success_receipt(self):
        self._real_handoff(cleanup_failure=True)

    def test_native_launch_and_partial_capture_do_not_leak_process_or_thread_handles(self):
        function = self.native.api.GetProcessHandleCount
        function.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        function.restype = ctypes.c_int

        def count():
            value = ctypes.c_ulong()
            self.native.check(function(self.native.api.GetCurrentProcess(), ctypes.byref(value)), "Count own handles")
            return value.value

        # Exclude one-time process-global Win32/Python launch initialization.
        warmup = w.Job(self.native)
        try:
            warmup.launch([sys.executable, "-c", "pass"], dict(os.environ), self.root)
            warmup.wait_empty()
        finally:
            warmup.close()
        before = count()
        for fail in (False, True, False, True):
            job = w.Job(self.native)
            try:
                if fail:
                    with patch.object(self.native, "image", side_effect=w.OwnershipError("capture fault")):
                        with self.assertRaises(w.OwnershipError):
                            job.launch([sys.executable, "-c", "pass"], dict(os.environ), self.root)
                else:
                    job.launch([sys.executable, "-c", "pass"], dict(os.environ), self.root)
                    job.wait_empty()
            finally:
                job.close()
        self.assertEqual(count(), before)


if __name__ == "__main__":
    unittest.main()

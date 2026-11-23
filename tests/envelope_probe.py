"""PH-19a local operating envelope probe.

Seeds a production-schema store, starts the real HTTP server in a separate process and
drives mixed authenticated traffic against it: claims, renewals and report-stage results
from synthetic workers on several runners, owner intake with urgent arrivals, paginated
board and inbox reads, and rejected credentials. While that load runs it injects the
PH-19 fault cases: a provider throttle, a storage outage, an interrupted worker while a
dependent waits on a blocked prerequisite, and a hard server kill during a dependent
wakeup batch followed by a drain of due external waits.

The synthetic workers only hold a lease for a short sleep. They measure the server's
latency, fairness and recovery, not live worker capacity.

    python tests/envelope_probe.py --json-out envelope.json

Random choices are seeded. Wall-clock timings vary with the host.
"""

import argparse
from bisect import bisect_left
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import http.client
import json
import os
from pathlib import Path
import platform
import random
from secrets import token_urlsafe
import signal
import socket
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from phalanx import dispatch, intake, waits  # noqa: E402
from phalanx.dispatch_api import DispatchSettings  # noqa: E402
from phalanx.intake_models import Intake  # noqa: E402
from phalanx.store import connect, migrate, write_transaction  # noqa: E402
from phalanx.transitions import Event, Outcome, Wait  # noqa: E402


OWNER = "o" * 32
RUNNER = "r" * 32
GENERAL = [f"repo-{index}" for index in range(8)]
LANES = ["urgent-lane", "waits", "fanout-source", "fanout", "prereq", "dependent", "throttle",
         "interrupt"]
PRIORITY_NAMES = ("low", "normal", "urgent")
REQUEST_TIMEOUT = 60

SERVER = """
import os
from pathlib import Path
import sys
from threading import Thread

import uvicorn

Path(sys.argv[2]).write_text(str(os.getpid()))
server = uvicorn.Server(uvicorn.Config("phalanx.api:app", host="127.0.0.1", port=int(sys.argv[1]),
                                     timeout_graceful_shutdown=5, access_log=False,
                                     log_level="warning"))

def stop_on_eof():
    sys.stdin.buffer.read()
    server.should_exit = True

Thread(target=stop_on_eof, daemon=True).start()
server.run()
"""


class Stopped(Exception):
    pass


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, int(round(fraction * len(ordered) + 0.5)) - 1))]


def spread(values, scale=1.0):
    if not values:
        return {"count": 0}
    return {"count": len(values),
            **{name: round(percentile(values, fraction) * scale, 1)
               for name, fraction in (("p50", 0.5), ("p95", 0.95), ("p99", 0.99), ("max", 1.0))}}


def iso_ms(stamp):
    return int(datetime.fromisoformat(stamp).timestamp() * 1000)


class Metrics:
    def __init__(self):
        self.lock = threading.Lock()
        self.samples = defaultdict(list)  # label -> [(monotonic end, seconds, status)]
        self.errors = []  # (monotonic, label, error)
        self.failures = []  # (monotonic, label, status, method and path, detail) for every 5xx
        self.claims = []  # monotonic times of general-worker claims
        self.counts = Counter()

    def record(self, label, status, elapsed, request=None, raw=b""):
        with self.lock:
            stamp = time.monotonic()
            self.samples[label].append((stamp, elapsed, status))
            if status >= 500:
                self.failures.append((stamp, label, status, request, raw[:160].decode("utf-8", "replace")))

    def error(self, label, error):
        with self.lock:
            self.errors.append((time.monotonic(), label, repr(error)[:200]))

    def count(self, name, amount=1):
        with self.lock:
            self.counts[name] += amount


class Client:
    """Keep-alive HTTP client that retries server errors with the same request."""

    def __init__(self, probe):
        self.probe = probe
        self.connection = None
        self.base = None
        with probe.lock:
            probe.clients.append(self)
    def call(self, label, method, path, token=None, body=None, headers=None, retry=True, record=True):
        headers = dict(headers or {})
        if token:
            headers["Authorization"] = f"Bearer {token}"
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        reconnected = False
        while True:
            if self.probe.halt.is_set():
                raise Stopped
            base = self.probe.base
            started = time.perf_counter()
            reused = self.connection is not None and self.base == base
            try:
                if not reused:
                    self.close()
                    host, port = base
                    self.connection = http.client.HTTPConnection(host, port, timeout=REQUEST_TIMEOUT)
                    self.base = base
                self.connection.request(method, path, body=data, headers=headers)
                response = self.connection.getresponse()
                raw = response.read()
                status = response.status
            except (OSError, http.client.HTTPException) as error:
                self.close()
                # The server may close an idle keep-alive connection first; reconnect once.
                if reused and not reconnected:
                    reconnected = True
                    continue
                if record:
                    self.probe.metrics.error(label, error)
                if not retry:
                    return None, None
                time.sleep(0.1 + random.random() * 0.1)
                continue
            if record:
                self.probe.metrics.record(label, status, time.perf_counter() - started, f"{method} {path}", raw)
            if status >= 500 and retry:
                time.sleep(0.2 + random.random() * 0.1)
                continue
            try:
                payload = json.loads(raw) if raw else None
            except ValueError:
                payload = raw.decode("utf-8", "replace")
            return status, payload

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None


class Probe:
    def __init__(self, config, path):
        self.config = config
        self.path = path
        self.rng = random.Random(config.seed)
        self.metrics = Metrics()
        self.stop = threading.Event()
        self.halt = threading.Event()
        self.violations = []
        self.results = {}
        self.windows = []  # (label, start, end) fault windows where 5xx or resets are expected
        self.ids = {}
        self.server = None
        self.pid = None
        self.base = None
        self.lock = threading.Lock()
        self.clients = []

    # Setup -----------------------------------------------------------------------------------

    def violation(self, message):
        with self.lock:
            self.violations.append(message)

    def seed(self):
        config = self.config
        started = time.perf_counter()
        migrate(self.path)
        with write_transaction(self.path) as db:
            for repository in GENERAL + LANES:
                intake.register(db, repository, f"https://github.com/example/{repository}.git")
            dispatch.set_dispatch(db, DispatchSettings(paused=False, aging_seconds=config.aging_seconds))
            for runner in self.runners():
                dispatch.set_capacity(db, runner, config.capacity)
            for lane in LANES:
                dispatch.set_capacity(db, f"lane-{lane}", 8)

        now_ms = int(time.time() * 1000)
        ready, blocked, closed = [], [], []
        for start in range(0, config.executions, 5000):
            with write_transaction(self.path) as db:
                for index in range(start, min(config.executions, start + 5000)):
                    affinity = None
                    if self.rng.random() < 0.1:
                        affinity = self.rng.choice(self.runners())
                    item = intake.create(db, Intake(
                        repository_id=GENERAL[index % len(GENERAL)], goal=f"Backlog item {index}",
                        target="report", priority=self.rng.choice(PRIORITY_NAMES),
                        runner_affinity=affinity,
                    ))
                    share = index % 100
                    bucket = ready if share == 0 else blocked if share < 30 else closed
                    bucket.append(item["execution"]["id"])
        with write_transaction(self.path) as db:
            db.executemany("UPDATE executions SET ready_since=? WHERE id=?",
                           ((now_ms - self.rng.randrange(config.backlog_age_seconds * 1000), execution)
                            for execution in ready))
            db.executemany(
                """UPDATE executions SET ready_since=NULL, state_json=json_set(state_json,
                       '$.status', 'waiting_for_input',
                       '$.wait', json_object('reason', 'Seeded owner question', 'trigger', 'owner reply'))
                   WHERE id=?""", ((execution,) for execution in blocked))
            db.executemany(
                """UPDATE executions SET ready_since=NULL, state_json=json_set(state_json,
                       '$.status', 'completed', '$.stage', 'report_storage',
                       '$.final_output', json_object('reference', 'seed://report', 'draft_revision', NULL))
                   WHERE id=?""", ((execution,) for execution in closed))

        lease = intake.digest(token_urlsafe(32))
        for start in range(0, config.due_waits, 2000):
            with write_transaction(self.path) as db:
                for index in range(start, min(config.due_waits, start + 2000)):
                    item = intake.create(db, Intake(repository_id="waits", goal=f"External gate {index}",
                                                    target="report"))
                    row, state = self.current(db, item)
                    claim = intake.claim_attempt(db, row, state, runner_id="seed-waits", lease_digest=lease,
                                                 lease_expires_at=int(time.time()) + 90)
                    row, state = intake.check_current(db, item["id"], row["id"], row["version"] + 1)
                    waits.schedule(db, row, state, claim["attempt_id"], "Seeded provider gate", 0,
                                   {"gate": "ci", "revision": {"head": "a" * 40, "base": "b" * 40}})
        with write_transaction(self.path) as db:
            db.execute("UPDATE external_rechecks SET due_at_ms = ? - (rowid % 60000)", (now_ms,))

        with write_transaction(self.path) as db:
            upstream = intake.create(db, Intake(repository_id="fanout-source", goal="Shared prerequisite",
                                                target="report"))
            self.ids["fanout_upstream"] = upstream["id"]
            for index in range(config.fanout):
                intake.create(db, Intake(
                    repository_id="fanout", goal=f"Fan-out dependent {index}", target="report",
                    dependencies=[{"upstream_id": upstream["id"], "milestone": "report"}],
                ))
            prerequisite = intake.create(db, Intake(repository_id="prereq", goal="Blocked prerequisite",
                                                    target="report"))
            row, state = self.current(db, prerequisite)
            claim = intake.claim_attempt(db, row, state, runner_id="seed-prereq", lease_digest=lease,
                                         lease_expires_at=int(time.time()) + 90)
            row, state = intake.check_current(db, prerequisite["id"], row["id"], row["version"] + 1)
            question = {"prompt": "Which source should the report cite?", "handoff": "Seeded question"}
            intake.apply(db, row, state, Event.RESULT, details={"question": question},
                         attempt_id=claim["attempt_id"], outcome=Outcome.NEEDS_INPUT,
                         wait=Wait(question["prompt"], "owner reply"))
            self.ids["prerequisite"] = prerequisite["id"]
            self.ids["dependent"] = intake.create(db, Intake(
                repository_id="dependent", goal="Waits on the blocked prerequisite", target="report",
                priority="urgent", dependencies=[{"upstream_id": prerequisite["id"], "milestone": "report"}],
            ))["id"]
            self.ids["throttled"] = [intake.create(db, Intake(
                repository_id="throttle", goal=f"Throttled provider call {index}", target="report",
            ))["id"] for index in range(config.throttled)]
            self.ids["interrupted"] = intake.create(db, Intake(
                repository_id="interrupt", goal="Worker interrupted in review", target="report",
            ))["id"]
        with connect(self.path, mode="ro") as db:
            counts = dict(db.execute("SELECT status, count(*) FROM executions GROUP BY status").fetchall())
        self.results["seed"] = {"seconds": round(time.perf_counter() - started, 1),
                                "executions_by_status": counts,
                                "database_mib": round(self.path.stat().st_size / 2**20, 1)}

    @staticmethod
    def current(db, item):
        execution = item["execution"]
        return intake.check_current(db, item["id"], execution["id"], execution["version"])

    def runners(self):
        return [f"runner-{index}" for index in range(self.config.runners)]

    # Server ----------------------------------------------------------------------------------

    def start_server(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        pid_file = self.path.with_name(f"server-{port}.pid")
        env = dict(os.environ, PHALANX_DB=str(self.path), PHALANX_OWNER_TOKEN=OWNER,
                   PHALANX_RUNNER_TOKEN=RUNNER,
                   PYTHONPATH=os.pathsep.join(filter(None, (str(ROOT), os.environ.get("PYTHONPATH")))))
        log = self.path.with_name(f"server-{port}.log").open("w")
        self.server = subprocess.Popen([sys.executable, "-c", SERVER, str(port), str(pid_file)],
                                       env=env, stdin=subprocess.PIPE, stdout=log,
                                       stderr=subprocess.STDOUT)
        log.close()
        self.base = ("127.0.0.1", port)
        deadline = time.monotonic() + 60
        client = Client(self)
        while time.monotonic() < deadline:
            if self.server.poll() is not None:
                break
            # Refused connections are expected while the server boots, so readiness polls stay out of metrics.
            status, _ = client.call("health", "GET", "/health", retry=False, record=False)
            if status == 200:
                client.close()
                self.pid = int(pid_file.read_text())
                return
            time.sleep(0.05)
        raise RuntimeError(f"Server did not start: {self.path.with_name(f'server-{port}.log').read_text()}")

    def kill_server(self):
        os.kill(self.pid, getattr(signal, "SIGKILL", signal.SIGTERM))
        self.server.wait(timeout=30)
        self.server.stdin.close()

    def stop_server(self):
        for client in self.clients:
            client.close()
        if self.server is None:
            return
        if self.server.stdin and not self.server.stdin.closed:
            self.server.stdin.close()
        for stop in (None, self.server.terminate, self.server.kill):
            if stop:
                stop()
            try:
                self.server.wait(timeout=15)
                return
            except subprocess.TimeoutExpired:
                pass

    # Shared worker helpers -------------------------------------------------------------------

    def pause(self, seconds):
        if self.stop.wait(seconds):
            raise Stopped

    def poll_delay(self, rng):
        return self.config.poll_seconds * rng.uniform(0.8, 1.2)

    def claim(self, client, runner, repositories, label="claim"):
        lease = token_urlsafe(32)
        status, claim = client.call(label, "POST", "/v1/dispatch/claim", RUNNER,
                                    {"runner_id": runner, "repository_ids": repositories},
                                    {"Phalanx-Lease": lease})
        if status != 200:
            self.violation(f"{label} for {runner} returned {status}: {claim}")
            return None, None
        self.metrics.count(f"{label}:{claim['status']}")
        return (claim, lease) if claim["status"] == "claimed" else (None, None)

    def renew(self, client, runner, claim, lease):
        status, body = client.call("renew", "POST",
                                   f"/v1/dispatch/runners/{runner}/attempts/{claim['attempt_id']}/renew",
                                   RUNNER, {}, {"Phalanx-Lease": lease})
        if status != 200:
            self.violation(f"Renewal of {claim['attempt_id']} returned {status}: {body}")

    def finish_stage(self, client, claim, lease, **fields):
        execution = claim["execution"]
        stage = execution["state"]["stage"]
        body = {"expected_version": execution["version"], "attempt_id": claim["attempt_id"],
                "stage": stage, "outcome": "succeeded"}
        if stage == "report_drafting":
            body["report_markdown"] = f"# Probe report {claim['item_id']}\n\nSynthetic draft."
        else:
            body["report_revision"] = execution["state"]["report_draft"]
        body.update(fields)
        status, result = client.call(
            "result", "POST", f"/v1/items/{claim['item_id']}/executions/{execution['id']}/result", RUNNER,
            body, {"Phalanx-Lease": lease, "Idempotency-Key": f"{claim['attempt_id']}-result"},
        )
        if status != 200:
            self.violation(f"Result for {claim['item_id']} at {stage} returned {status}: {result}")
            return None
        return result

    def drive(self, client, runner, repositories, item_id, until="completed", deadline=120):
        """Claim and finish stages of one lane item until it reaches `until`."""
        rng = random.Random(item_id)
        end = time.monotonic() + deadline
        while time.monotonic() < end:
            claim, lease = self.claim(client, runner, repositories, label="lane_claim")
            if claim is None:
                self.pause(self.poll_delay(rng))
                continue
            if claim["item_id"] != item_id:
                self.violation(f"{runner} claimed unexpected item {claim['item_id']}")
                return None
            result = self.finish_stage(client, claim, lease)
            if result is None or result["state"]["status"] == until:
                return result
        self.violation(f"{runner} could not finish {item_id} within {deadline}s")
        return None

    def item(self, client, item_id, token=OWNER):
        status, item = client.call("item", "GET", f"/v1/items/{item_id}", token)
        if status != 200:
            raise RuntimeError(f"Inspect {item_id} returned {status}: {item}")
        return item

    def window(self, label, start, end):
        with self.lock:
            self.windows.append((label, start, end))

    # Load actors -----------------------------------------------------------------------------

    def worker(self, runner, index):
        rng = random.Random(self.config.seed * 1000 + index)
        client = Client(self)
        low, high = self.config.work_seconds
        while not self.stop.is_set():
            claim, lease = self.claim(client, runner, GENERAL)
            if claim is None:
                self.pause(self.poll_delay(rng))
                continue
            with self.metrics.lock:
                self.metrics.claims.append(time.monotonic())
            hold = rng.uniform(low, high)
            time.sleep(hold / 2)
            self.renew(client, runner, claim, lease)
            time.sleep(hold / 2)
            if self.finish_stage(client, claim, lease) is not None:
                self.metrics.count("worker_stages")

    def owner_intake(self):
        rng = random.Random(self.config.seed + 1)
        client = Client(self)
        next_lane = 0.0
        while not self.stop.is_set():
            repository = rng.choice(GENERAL)
            priority = rng.choices(PRIORITY_NAMES, weights=(2, 3, 5))[0]
            self.create(client, repository, priority, rng)
            if time.monotonic() >= next_lane:
                self.create(client, "urgent-lane", "urgent", rng)
                next_lane = time.monotonic() + self.config.lane_interval
            self.pause(1 / self.config.intake_rate)

    def create(self, client, repository, priority, rng):
        status, body = client.call(
            "intake", "POST", "/v1/items", OWNER,
            {"repository_id": repository, "goal": "Probe arrival", "target": "report", "priority": priority},
            {"Idempotency-Key": f"probe-{rng.getrandbits(64):016x}"},
        )
        if status != 201:
            self.violation(f"Intake returned {status}: {body}")

    def urgent_lane(self):
        rng = random.Random(self.config.seed + 2)
        client = Client(self)
        while not self.stop.is_set():
            claim, lease = self.claim(client, "lane-urgent-lane", ["urgent-lane"], label="lane_claim")
            if claim is None:
                self.pause(self.poll_delay(rng))
                continue
            self.finish_stage(client, claim, lease)

    def board(self, index):
        rng = random.Random(self.config.seed + 10 + index)
        client = Client(self)
        token = (OWNER, RUNNER)[index % 2]
        while not self.stop.is_set():
            cursor = None
            for _ in range(3):
                query = "kind=open&limit=50" + (f"&cursor={cursor}" if cursor else "")
                status, page = client.call("board_open", "GET", f"/v1/operator/executions?{query}", token)
                if status != 200:
                    self.violation(f"Board page returned {status}: {page}")
                    break
                cursor = page["next_cursor"]
                if not cursor:
                    break
            status, page = client.call("board_closed", "GET", "/v1/operator/executions?kind=closed&limit=50",
                                       token)
            if status != 200:
                self.violation(f"Closed board page returned {status}: {page}")
            status, page = client.call("inbox", "GET", "/v1/owner/inbox?limit=50", OWNER)
            if status != 200:
                self.violation(f"Inbox returned {status}: {page}")
            self.pause(self.config.board_interval * rng.uniform(0.8, 1.2))

    def auth(self):
        client = Client(self)
        checks = (
            ("GET", "/v1/owner/inbox", None, None, 401),
            ("GET", "/v1/owner/inbox", "x" * 32, None, 401),
            ("POST", "/v1/items", RUNNER, {"repository_id": "repo-0", "goal": "Denied", "target": "report"}, 403),
            ("POST", "/v1/dispatch/claim", OWNER, {"runner_id": "runner-0", "repository_ids": ["repo-0"]}, 403),
        )
        while not self.stop.is_set():
            for method, path, token, body, expected in checks:
                headers = {"Phalanx-Lease": token_urlsafe(32)} if "claim" in path else None
                status, payload = client.call("auth_rejected", method, path, token, body, headers)
                if status != expected:
                    self.violation(f"{method} {path} with {token and token[:1]} returned {status}, "
                                   f"expected {expected}: {payload}")
            self.pause(self.config.auth_interval)

    # Fault scenarios -------------------------------------------------------------------------

    def at(self, fraction):
        self.pause(max(0.0, self.t0 + fraction * self.config.duration - time.monotonic()))

    def throttle(self):
        """A provider throttle defers each item until its retry time, then resumes the same stage."""
        client = Client(self)
        runner, lane = "lane-throttle", ["throttle"]
        retry_at = {}
        for _ in self.ids["throttled"]:
            claim, lease = self.claim(client, runner, lane, label="lane_claim")
            if claim is None:
                self.violation("Throttled item was not claimable")
                return
            # Measure from before the request: a later read can wait out a slow write turn.
            sent_ms = int(time.time() * 1000)
            result = self.finish_stage(
                client, claim, lease, outcome="failed", report_markdown=None,
                failure_reason="Provider returned 429 Too Many Requests",
                retry_after_seconds=self.config.throttle_seconds,
            )
            if result is None:
                continue
            execution = self.item(client, claim["item_id"])["execution"]
            due = execution["retry_at_ms"]
            if (execution["state"]["status"] != "queued" or due is None
                    or due < sent_ms + self.config.throttle_seconds * 1000):
                self.violation(f"Throttled item {claim['item_id']} has no visible provider retry time: "
                               f"status {execution['state']['status']}, retry_at_ms {due}, sent_ms {sent_ms}")
                continue
            retry_at[claim["item_id"]] = execution["retry_at_ms"]
        early = resumed = 0
        delays = {}
        rng = random.Random(self.config.seed + 3)
        while len(delays) < len(retry_at):
            claim, lease = self.claim(client, runner, lane, label="lane_claim")
            if claim is None:
                self.pause(0.5 * rng.uniform(0.8, 1.2))
                continue
            item_id = claim["item_id"]
            if item_id not in delays:
                now_ms = time.time() * 1000
                due = retry_at.get(item_id)
                if due is None or now_ms < due:
                    early += 1
                delays[item_id] = (now_ms - due) / 1000 if due else None
                if claim["execution"]["state"]["stage"] == "report_drafting":
                    resumed += 1
            self.finish_stage(client, claim, lease)
        if early:
            self.violation(f"{early} throttled items were claimed before the provider retry time")
        if resumed != len(retry_at):
            self.violation(f"Only {resumed} of {len(retry_at)} throttled items resumed the throttled stage")
        self.results["throttle"] = {
            "items": len(retry_at), "retry_after_seconds": self.config.throttle_seconds,
            "claimed_before_retry_at": early, "resumed_same_stage": resumed,
            "claim_after_retry_at_seconds": spread([delay for delay in delays.values() if delay is not None]),
        }

    def interrupt_and_prerequisite(self):
        """Interrupt a worker in review while a dependent waits on a blocked prerequisite."""
        self.at(self.config.interrupt_at)
        client = Client(self)
        rng = random.Random(self.config.seed + 4)
        result = {}
        dependent = self.item(client, self.ids["dependent"])["execution"]
        result["dependent_wait_reason"] = [link["reason"] for link in dependent["unmet_dependencies"]]
        if not result["dependent_wait_reason"]:
            self.violation("The dependent of a blocked prerequisite shows no wait reason")
        claim, _ = self.claim(client, "lane-dependent", ["dependent"], label="lane_claim")
        if claim is not None:
            self.violation("A dependent was claimed while its prerequisite was blocked")

        runner, lane, item_id = "lane-interrupt", ["interrupt"], self.ids["interrupted"]
        claim, lease = self.claim(client, runner, lane, label="lane_claim")
        drafted = self.finish_stage(client, claim, lease)
        claim, lease = self.claim(client, runner, lane, label="lane_claim")
        if claim is None or claim["execution"]["state"]["stage"] != "report_review":
            self.violation("Interrupted item did not reach report_review")
            return
        # The worker process dies here and stops renewing. Jump its lease past expiry rather
        # than waiting the production 90 s.
        interrupted = time.monotonic()
        claims_before = len(self.metrics.claims)
        with sqlite3.connect(self.path, timeout=30, isolation_level=None) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE attempts SET lease_expires_at=? WHERE id=?",
                       (int(time.time()) - 1, claim["attempt_id"]))
            db.execute("COMMIT")
        db.close()
        status, candidates = client.call("recovery", "GET", f"/v1/dispatch/runners/{runner}/recovery", RUNNER)
        expired = [entry for entry in candidates or () if entry["attempt_id"] == claim["attempt_id"]]
        if status != 200 or not expired or expired[0]["reason"] != "lease_expired":
            self.violation(f"Expired attempt is not a visible recovery candidate: {status} {candidates}")
            return
        status, recovered = client.call(
            "recover", "POST",
            f"/v1/dispatch/runners/{runner}/attempts/{claim['attempt_id']}/recover-stopped", RUNNER,
            {"expected_version": expired[0]["version"], "item_id": item_id,
             "reconciliation": "Worker process exited before producing a review"},
            {"Idempotency-Key": f"{claim['attempt_id']}-recover"},
        )
        if status != 200 or recovered["state"]["status"] != "queued" or recovered["retry_at_ms"] is None:
            self.violation(f"Stopped-worker recovery did not requeue with a retry time: {status} {recovered}")
            return
        while True:
            resumed, lease = self.claim(client, runner, lane, label="lane_claim")
            if resumed is not None:
                break
            self.pause(self.poll_delay(rng))
        state = resumed["execution"]["state"]
        result["interrupted_resumed_stage"] = state["stage"]
        result["interrupted_kept_draft"] = state["report_draft"] == drafted["state"]["report_draft"]
        result["interrupt_to_resume_seconds"] = round(time.monotonic() - interrupted, 2)
        result["unrelated_claims_while_interrupted"] = len(self.metrics.claims) - claims_before
        if state["stage"] != "report_review" or not result["interrupted_kept_draft"]:
            self.violation(f"Interrupted item did not resume from its recorded review state: {state}")
        if result["unrelated_claims_while_interrupted"] == 0:
            self.violation("No unrelated work advanced while a worker was interrupted")
        self.finish_stage(client, resumed, lease)
        self.drive(client, runner, lane, item_id)

        dependent = self.item(client, self.ids["dependent"])["execution"]
        if not dependent["unmet_dependencies"]:
            self.violation("The dependent lost its wait reason while the prerequisite stayed blocked")
        prerequisite = self.item(client, self.ids["prerequisite"])["execution"]
        status, body = client.call(
            "reply", "POST", f"/v1/items/{self.ids['prerequisite']}/executions/{prerequisite['id']}/reply",
            OWNER, {"expected_version": prerequisite["version"], "answer": "Cite the changelog",
                    "answered_by": "captain"},
            {"Idempotency-Key": f"{prerequisite['id']}-reply"},
        )
        if status != 200:
            self.violation(f"Reply to the blocked prerequisite returned {status}: {body}")
            return
        completed = self.drive(client, "lane-prereq", ["prereq"], self.ids["prerequisite"])
        if completed is None:
            return
        released = time.monotonic()
        while True:
            claim, lease = self.claim(client, "lane-dependent", ["dependent"], label="lane_claim")
            if claim is not None:
                break
            self.pause(self.poll_delay(rng))
        result["prerequisite_to_dependent_claim_seconds"] = round(time.monotonic() - released, 2)
        snapshot = [link["upstream_id"] for link in claim["dependencies"]]
        if snapshot != [self.ids["prerequisite"]]:
            self.violation(f"Dependent claim lacks its prerequisite snapshot: {claim['dependencies']}")
        self.finish_stage(client, claim, lease)
        self.results["interrupt_and_prerequisite"] = result

    def outage(self):
        """Hold an exclusive SQLite lock so every server transaction sees a storage outage."""
        self.at(self.config.outage_at)
        client = Client(self)
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.execute("BEGIN EXCLUSIVE")
        started = time.monotonic()
        status, body = client.call("health", "GET", "/health", retry=False)
        health = status
        self.pause(max(0.0, started + self.config.outage_seconds - time.monotonic()))
        db.execute("ROLLBACK")
        db.close()
        ended = time.monotonic()
        self.window("outage", started, ended + self.config.fault_grace)
        self.results["outage"] = {"seconds": round(ended - started, 2), "health_status": health,
                                  "health_detail": body}
        self.outage_span = (started, ended)

    def restart(self):
        """Hard-kill the server during a dependent wakeup batch and measure recovery."""
        self.at(self.config.restart_at)
        client = Client(self)
        upstream = self.ids["fanout_upstream"]
        runner, lane = "lane-fanout-source", ["fanout-source"]
        self.drive(client, runner, lane, upstream, until="queued")
        while True:
            claim, lease = self.claim(client, runner, lane, label="lane_claim")
            if claim is not None and claim["execution"]["state"]["stage"] == "report_storage":
                break
            if claim is not None:
                self.finish_stage(client, claim, lease)
            else:
                self.pause(0.2)
        client.close()
        result = self.finish_stage(Client(self), claim, lease)
        if result is None or result["state"]["status"] != "completed":
            self.violation(f"Fan-out prerequisite did not complete before the restart: {result}")
        killed = time.monotonic()
        self.kill_server()
        with connect(self.path, mode="ro") as db:
            blocked_at_kill = db.execute(
                """SELECT count(*) FROM executions WHERE queue_repository='fanout'
                   AND dependency_wait_json IS NOT NULL"""
            ).fetchone()[0]
        self.start_server()
        healthy = time.monotonic()
        self.window("restart", killed, healthy + self.config.fault_grace)
        self.restarted = healthy
        drain = threading.Thread(target=self.guard, args=(self.drain,), name="drain")
        drain.start()
        unblocked = None
        while unblocked is None and time.monotonic() < healthy + 300:
            with connect(self.path, mode="ro") as db:
                left = db.execute(
                    """SELECT count(*) FROM executions WHERE queue_repository='fanout'
                       AND dependency_wait_json IS NOT NULL"""
                ).fetchone()[0]
            if left == 0:
                unblocked = time.monotonic()
            else:
                self.pause(0.1)
        drain.join()
        with self.metrics.lock:
            first_claim = next((stamp for stamp in self.metrics.claims if stamp > healthy), None)
        self.results["restart"] = {
            "fanout_dependents": self.config.fanout,
            "blocked_when_killed": blocked_at_kill,
            "kill_to_healthy_seconds": round(healthy - killed, 2),
            "kill_to_first_unrelated_claim_seconds": first_claim and round(first_claim - killed, 2),
            "kill_to_all_dependents_released_seconds": unblocked and round(unblocked - killed, 2),
            **self.results.pop("drain", {}),
        }
        if unblocked is None:
            self.violation("Fan-out dependents were still blocked 300 s after the restart")

    def drain(self):
        """Admit every due external recheck after the restart while renewals continue."""
        started = time.monotonic()
        local = threading.local()

        def recheck(entry):
            if not hasattr(local, "client"):
                local.client = Client(self)
            status, body = local.client.call(
                "recheck", "POST", f"/v1/items/{entry['item_id']}/executions/{entry['execution_id']}/recheck",
                RUNNER, {"expected_version": entry["version"]},
                {"Idempotency-Key": f"{entry['execution_id']}-{entry['version']}-recheck"},
            )
            if status != 200:
                self.violation(f"Recheck of {entry['execution_id']} returned {status}: {body}")
            return status

        client = Client(self)
        admitted = 0
        with ThreadPoolExecutor(self.config.drain_threads) as pool:
            while True:
                status, due = client.call("due_rechecks", "GET", "/v1/runner/due-rechecks?limit=100", RUNNER)
                if status != 200:
                    self.violation(f"Due rechecks returned {status}: {due}")
                    return
                if not due:
                    break
                admitted += sum(status == 200 for status in pool.map(recheck, due))
        self.drain_span = (started, time.monotonic())
        self.results["drain"] = {"due_rechecks_admitted": admitted,
                                 "healthy_to_rechecks_drained_seconds": round(time.monotonic() - started, 2)}

    # Orchestration ---------------------------------------------------------------------------

    def guard(self, target, *args):
        try:
            target(*args)
        except Stopped:
            pass
        except Exception as error:  # Surface every actor failure as a probe violation.
            self.violation(f"{target.__name__} crashed: {error!r}")

    def run(self):
        self.seed()
        self.start_server()
        self.t0 = time.monotonic()
        self.wall_start = datetime.now().astimezone()
        load = [threading.Thread(target=self.guard, args=(self.worker, runner, index), daemon=True)
                for index, runner in enumerate(runner for runner in self.runners()
                                               for _ in range(self.config.capacity + 1))]
        load += [threading.Thread(target=self.guard, args=(target,), daemon=True)
                 for target in (self.owner_intake, self.urgent_lane, self.auth)]
        load += [threading.Thread(target=self.guard, args=(self.board, index), daemon=True)
                 for index in range(self.config.board_readers)]
        scenarios = [threading.Thread(target=self.guard, args=(target,), daemon=True)
                     for target in (self.throttle, self.interrupt_and_prerequisite, self.outage, self.restart)]
        for thread in load + scenarios:
            thread.start()
        deadline = self.t0 + self.config.duration + self.config.scenario_timeout
        for thread in scenarios:
            thread.join(max(0.0, deadline - time.monotonic()))
        if any(thread.is_alive() for thread in scenarios):
            self.violation("Fault scenarios did not finish before the probe deadline")
        self.pause_until(self.t0 + self.config.duration)
        self.stop.set()
        self.ended = time.monotonic()
        for thread in load:
            thread.join(REQUEST_TIMEOUT + 5)
        self.halt.set()
        for thread in load + scenarios:
            thread.join(5)
        self.stop_server()
        return self.summarize()

    def pause_until(self, moment):
        remaining = moment - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)

    # Analysis --------------------------------------------------------------------------------

    def faulted(self, stamp):
        return any(start <= stamp <= end for _, start, end in self.windows)

    def window_offsets(self):
        return [(label, round(start - self.t0, 2), round(end - self.t0, 2)) for label, start, end in self.windows]

    def summarize(self):
        seconds = self.ended - self.t0
        routes = {}
        unexpected = Counter()
        offered = completed = 0
        for label, samples in sorted(self.metrics.samples.items()):
            statuses = Counter(status for _, _, status in samples)
            steady = [elapsed for stamp, elapsed, status in samples
                      if status < 500 and not self.faulted(stamp)]
            routes[label] = {"latency_ms": spread(steady, 1000), "statuses": dict(sorted(statuses.items()))}
            if hasattr(self, "drain_span"):
                start, end = self.drain_span
                with_stamps = [(stamp, elapsed) for stamp, elapsed, status in samples
                               if status < 500 and not self.faulted(stamp)]
                routes[label]["latency_ms_before_drain"] = spread(
                    [elapsed for stamp, elapsed in with_stamps if stamp < start], 1000)
                routes[label]["latency_ms_during_drain"] = spread(
                    [elapsed for stamp, elapsed in with_stamps if start <= stamp <= end], 1000)
            offered += len(samples)
            completed += sum(status < 500 for _, _, status in samples)
            unexpected[label] += sum(status >= 500 and not self.faulted(stamp) for stamp, _, status in samples)
            if statuses.get(500):
                self.violation(f"{statuses[500]} {label} responses were opaque 500s instead of a reasoned 503")
        resets = [entry for entry in self.metrics.errors if not self.faulted(entry[0])]
        for label, count in unexpected.items():
            if count:
                failures = [(round(stamp - self.t0, 2), status, request, detail)
                            for stamp, name, status, request, detail in self.metrics.failures
                            if name == label and not self.faulted(stamp)]
                statuses = dict(Counter(status for _, status, _, _ in failures))
                self.violation(f"{count} {label} responses failed with 5xx outside fault windows: "
                               f"statuses {statuses}, first (seconds, status, request, detail) {failures[:3]}, "
                               f"fault windows {self.window_offsets()}")
        if resets:
            self.violation(f"{len(resets)} connection failures outside fault windows: {resets[:3]}")
        outage = self.results.get("outage")
        if outage and hasattr(self, "outage_span"):
            start, end = self.outage_span
            failures = [(stamp, status) for samples in self.metrics.samples.values()
                        for stamp, _, status in samples
                        if status >= 500 and start <= stamp <= end + self.config.fault_grace]
            outage["server_error_statuses"] = dict(Counter(status for _, status in failures))
            failures = [stamp for stamp, _ in failures]
            outage["release_to_last_error_seconds"] = (
                round(max(failures) - end, 2) if failures else None)
            with self.metrics.lock:
                outage["release_to_first_claim_seconds"] = next(
                    (round(stamp - end, 2) for stamp in self.metrics.claims if stamp > end), None)
            outage_latency = [elapsed for label in ("claim", "renew", "result")
                              for stamp, elapsed, status in self.metrics.samples[label]
                              if start <= stamp <= end + self.config.fault_grace]
            outage["write_latency_ms"] = spread(outage_latency, 1000)
        self.results["traffic"] = {
            "seconds": round(seconds, 1), "offered_requests_per_second": round(offered / seconds, 1),
            "completed_requests_per_second": round(completed / seconds, 1),
            "worker_stage_results": self.metrics.counts["worker_stages"],
            "claim_outcomes": {name.split(":", 1)[1]: count for name, count in self.metrics.counts.items()
                               if name.startswith("claim:")},
            "routes": routes,
        }
        self.results["fairness"] = self.fairness()
        self.results["invariants"] = self.invariants()
        self.results["violations"] = self.violations
        self.results["config"] = vars(self.config)
        self.results["environment"] = {
            "python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
            "platform": platform.platform(), "processor": platform.processor(),
            "logical_cpus": os.cpu_count(),
        }
        return self.results

    def fairness(self):
        start = self.wall_start.timestamp() * 1000
        aging = self.config.aging_seconds * 1000
        bands = defaultdict(list)
        promoted = Counter()
        lane = []
        placeholders = ",".join("?" * len(GENERAL))
        with connect(self.path, mode="ro") as db:
            rows = db.execute(
                f"""SELECT e.queue_repository, e.queue_priority, h.created_at,
                           json_extract(h.details_json, '$.ready_since_ms') AS ready_since
                    FROM transition_history AS h JOIN executions AS e ON e.id = h.execution_id
                    WHERE h.event = 'claim' AND e.queue_repository IN ({placeholders}, 'urgent-lane')""",
                GENERAL,
            ).fetchall()
            now_ms = int(time.time() * 1000)
            oldest = db.execute(
                f"""SELECT min(ready_since) FROM executions WHERE status = 'queued' AND ready_since <= ?
                    AND dependency_wait_json IS NULL AND queue_repository IN ({placeholders})""",
                (now_ms, *GENERAL),
            ).fetchone()[0]
            eligible = db.execute(
                f"""SELECT count(*) FROM executions WHERE status = 'queued' AND ready_since <= ?
                    AND dependency_wait_json IS NULL AND queue_repository IN ({placeholders})""",
                (now_ms, *GENERAL),
            ).fetchone()[0]
        for row in rows:
            claimed = iso_ms(row["created_at"])
            if claimed < start or row["ready_since"] is None:
                continue
            wait = claimed - row["ready_since"]
            if row["queue_repository"] == "urgent-lane":
                lane.append(wait)
                continue
            band = PRIORITY_NAMES[row["queue_priority"]]
            bands[band].append(wait)
            if min(2, row["queue_priority"] + wait // aging) > row["queue_priority"]:
                promoted[band] += 1
        return {
            "eligible_to_claim_seconds_by_base_priority": {
                band: {**spread(bands[band], 0.001), "claimed_after_aging": promoted[band]}
                for band in PRIORITY_NAMES
            },
            "free_slot_eligible_to_claim_seconds": spread(lane, 0.001),
            "eligible_at_end": eligible,
            "oldest_eligible_age_at_end_seconds": oldest and round((now_ms - oldest) / 1000, 1),
        }

    def invariants(self):
        checks = {}
        with connect(self.path, mode="ro") as db:
            checks["quick_check"] = db.execute("PRAGMA quick_check").fetchone()[0]
            limits = dict(db.execute("SELECT runner_id, capacity FROM runner_limits").fetchall())
            events = defaultdict(list)
            for runner, started, ended in db.execute("SELECT runner_id, started_at, ended_at FROM attempts"):
                events[runner].append((started, 1))
                events[runner].append((ended or "9999", -1))
            peaks = {}
            for runner, entries in events.items():
                load = peak = 0
                for _, change in sorted(entries, key=lambda entry: (entry[0], entry[1])):
                    load += change
                    peak = max(peak, load)
                peaks[runner] = peak
            checks["peak_attempts_by_runner"] = {runner: peaks[runner] for runner in sorted(peaks)
                                                 if runner.startswith("runner-")}
            checks["overbooked_runners"] = sorted(
                runner for runner, peak in peaks.items()
                if peak > limits.get(runner, dispatch.DEFAULT_CAPACITY))
            checks["executions_with_two_open_attempts"] = db.execute(
                """SELECT count(*) FROM (SELECT execution_id FROM attempts WHERE ended_at IS NULL
                   GROUP BY execution_id HAVING count(*) > 1)"""
            ).fetchone()[0]
            checks["waits_without_reason"] = db.execute(
                """SELECT count(*) FROM executions WHERE status IN ('waiting_for_input', 'waiting_for_external')
                   AND (json_extract(state_json, '$.wait.reason') IS NULL
                        OR json_extract(state_json, '$.wait.trigger') IS NULL)"""
            ).fetchone()[0]
            checks["dependency_waits_without_reason"] = db.execute(
                """SELECT count(*) FROM executions AS e, json_each(e.dependency_wait_json) AS link
                   WHERE e.dependency_wait_json IS NOT NULL
                     AND coalesce(json_extract(link.value, '$.reason'), '') = ''"""
            ).fetchone()[0]
            checks["queued_without_next_action"] = db.execute(
                "SELECT count(*) FROM executions WHERE status = 'queued' AND ready_since IS NULL"
            ).fetchone()[0]
            checks["running_without_attempt"] = db.execute(
                """SELECT count(*) FROM executions AS e WHERE status = 'running' AND NOT EXISTS (
                       SELECT 1 FROM attempts WHERE execution_id = e.id AND ended_at IS NULL)"""
            ).fetchone()[0]
            checks["due_rechecks_left"] = db.execute(
                """SELECT count(*) FROM external_rechecks AS r JOIN executions AS e
                   ON e.id = r.execution_id AND e.version = r.version
                   WHERE e.status = 'waiting_for_external' AND r.due_at_ms <= ?""",
                (int(time.time() * 1000),),
            ).fetchone()[0]
            checks["fanout_still_blocked"] = db.execute(
                """SELECT count(*) FROM executions WHERE queue_repository = 'fanout'
                   AND dependency_wait_json IS NOT NULL"""
            ).fetchone()[0]
        for name, expected in (("quick_check", "ok"), ("overbooked_runners", []),
                               ("executions_with_two_open_attempts", 0), ("waits_without_reason", 0),
                               ("dependency_waits_without_reason", 0), ("queued_without_next_action", 0),
                               ("running_without_attempt", 0), ("due_rechecks_left", 0),
                               ("fanout_still_blocked", 0)):
            if checks[name] != expected:
                self.violation(f"Invariant {name} is {checks[name]!r}, expected {expected!r}")
        return checks


def parse(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    add = parser.add_argument
    add("--executions", type=int, default=100_000, help="seeded backlog: 1%% ready, 29%% input wait, 70%% closed")
    add("--due-waits", type=int, default=10_000, help="external waits already due at restart")
    add("--fanout", type=int, default=2_000, help="dependents of one report prerequisite")
    add("--throttled", type=int, default=8, help="items failed with a provider throttle")
    add("--runners", type=int, default=4)
    add("--capacity", type=int, default=6, help="slots per runner; each runner gets one extra worker")
    add("--duration", type=float, default=150.0, help="minimum load seconds")
    add("--scenario-timeout", type=float, default=600.0)
    add("--seed", type=int, default=19)
    add("--aging-seconds", type=int, default=60)
    add("--backlog-age-seconds", type=int, default=300, help="spread of seeded ready times")
    add("--poll-seconds", type=float, default=2.0, help="empty-poll delay, jittered by 20%%")
    add("--work-seconds", type=float, nargs=2, default=(0.5, 1.5), help="lease hold per stage")
    add("--intake-rate", type=float, default=2.0, help="owner arrivals per second")
    add("--lane-interval", type=float, default=3.0, help="seconds between free-slot urgent arrivals")
    add("--board-readers", type=int, default=2)
    add("--board-interval", type=float, default=1.0)
    add("--auth-interval", type=float, default=2.0)
    add("--throttle-seconds", type=int, default=20)
    add("--outage-seconds", type=float, default=5.0)
    add("--fault-grace", type=float, default=5.0, help="seconds after a fault where 5xx are expected")
    add("--drain-threads", type=int, default=4)
    add("--interrupt-at", type=float, default=0.1, help="fraction of duration")
    add("--outage-at", type=float, default=0.3)
    add("--restart-at", type=float, default=0.5)
    add("--json-out", type=Path)
    add("--keep", type=Path, help="directory to keep the store and server logs in")
    return parser.parse_args(argv)


def run(config):
    with TemporaryDirectory(prefix="phalanx-envelope-") as temporary:
        directory = config.keep or Path(temporary)
        directory.mkdir(parents=True, exist_ok=True)
        probe = Probe(config, directory / "queue.sqlite3")
        try:
            return probe.run()
        finally:
            probe.halt.set()
            probe.stop.set()
            probe.stop_server()


def main(argv=None):
    config = parse(argv)
    summary = run(config)
    text = json.dumps(summary, indent=2, default=str)
    if config.json_out:
        config.json_out.write_text(text + "\n")
    print(text)
    return 1 if summary["violations"] else 0


if __name__ == "__main__":
    sys.exit(main())

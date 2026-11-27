"""Exact stored/derived flag ordering, boundary ages, pickup and scoped writer views."""

import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import sys

import harness as h


NOW = datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=timezone.utc)
STAMP = NOW.isoformat(timespec="microseconds")
QUEUE_EXECUTION = f"{0x1101:032x}"


def ago(seconds):
    return (NOW - timedelta(seconds=seconds)).isoformat(timespec="microseconds")


def cases():
    result = []

    def add(name, method="GET", path="/v1/flags", body=None, token=h.OWNER, **fixture):
        headers = [("Content-Type", "application/json")]
        if token is not None:
            headers.append(("Authorization", "Bearer " + token))
        result.append({"id": name, "method": method, "path": path, "headers": headers,
                       "body": json.dumps(body) if body is not None else "", **fixture})

    add("flags-initial")
    add("writer-current-initial", path="/v1/writer", token=h.WRITER)
    flag = {"kind": "synthetic_blocker", "reason": "  Synthetic snow \u96ea  ", "recovery": " Retry next verified pass ",
            "notify_owner": "yes"}
    add("flag-store", "PUT", body=flag)
    add("flag-store-update", "PUT", body={**flag, "reason": "Updated reason", "notify_owner": 0})
    add("flags-stored")
    scoped = {**flag, "kind": "scoped_blocker", "item_id": h.SEED, "execution_id": h.SEED_EXECUTION, "captain_id": "fixture-captain"}
    add("flag-store-scoped", "PUT", body=scoped, token=h.RUNNER)
    add("writer-current-scoped", path="/v1/writer", token=h.WRITER)
    add("flag-clear-missing", "POST", "/v1/flags/clear", {"kind": "missing"})
    add("flag-clear", "POST", "/v1/flags/clear", {"kind": "synthetic_blocker"})
    add("flag-clear-replay", "POST", "/v1/flags/clear", {"kind": "synthetic_blocker"})
    for name, edits in (("kind", {"kind": "Invalid"}), ("subject-null", {"item_id": None}),
                        ("subject-long", {"item_id": "x" * 101}), ("reason", {"reason": None}),
                        ("notify", {"notify_owner": []}), ("extra", {"extra": None})):
        add("flag-model-" + name, "PUT", body={**flag, **edits})
    add("flags-captain-just-live", captain={"heartbeat_at": ago(900 - .000001)})
    add("flags-captain-exact-stale", captain={"heartbeat_at": ago(900)})
    add("flags-ending-grace", captain={"state": "ending", "heartbeat_at": STAMP, "ending_at": ago(1800)})
    add("flags-captain-restored", captain={"state": "active", "heartbeat_at": STAMP, "ending_at": None,
                                         "pickup_json": json.dumps({"repositories": {"fixture": None}})})
    add("flags-launch-below", writer={"created_at": ago(600 - .000001)})
    add("flags-launch-exact", writer={"created_at": ago(600)})
    add("writer-current-launch-warning", path="/v1/writer", token=h.WRITER)
    add("flags-busy-below", writer={"state": "busy", "confirmed_at": ago(7200 - .000001)},
        advanced=ago(7200 - .000001))
    add("flags-busy-exact", writer={"confirmed_at": ago(7200)}, advanced=ago(7200))
    add("flags-capacity-wait-suppresses-progress", writer={"building": 0, "building_requested_at": STAMP})
    add("writer-current-capacity-wait", path="/v1/writer", token=h.WRITER)
    add("flags-building-available-progress", dispatch={"paused": 0})
    add("flags-writer-stopped", writer={"state": "stopped", "state_reported_at": ago(60)})
    add("flags-writer-error", writer={"state": "error"})
    sessions = [{"id": "synthetic-orphan", "name": "Other native session", "first_seen": ago(300)},
                {"id": "synthetic-duplicate", "name": "Duplicate native writer", "first_seen": ago(200),
                 "writer_nonce": "synthetic-nonce"},
                {"id": "synthetic-deleted", "name": "Ignore missing session", "first_seen": ago(400), "availability": "deleted"}]
    add("flags-session-orphan-duplicate", writer={"session_id": "synthetic-confirmed", "state": "idle", "building_requested_at": None},
        captain={"sessions_json": json.dumps(sessions)})
    add("writer-current-only-owned-session", path="/v1/writer", token=h.WRITER)
    add("flag-archive-refusal", "PUT", "/v1/session-archives", {"session_id": "synthetic-orphan", "outcome": "refused",
        "reason": "Synthetic runtime refusal", "reported_by": "synthetic-observer"})
    add("flags-archive-refusal")
    add("flags-queue-below-thirty", dispatch={"paused": 1}, queue_ready=int(NOW.timestamp()*1000)-1800000+1)
    add("flags-queue-exact-thirty", queue_ready=int(NOW.timestamp()*1000)-1800000)
    add("flags-queue-eligible-before-sixty", dispatch={"paused": 0}, queue_ready=int(NOW.timestamp()*1000)-3600000+1)
    add("flags-queue-eligible-exact-sixty", queue_ready=int(NOW.timestamp()*1000)-3600000)
    add("flags-queue-repository-blocked", captain={"pickup_json": json.dumps({"repositories": {"fixture": "Trust required"}})})
    add("flags-queue-uninitialized", captain={"pickup_json": '{"repositories":{}}'})
    add("flags-queue-outside-scope", captain={"pickup_json": '{"repositories":{"elsewhere":null}}'})
    add("flags-queue-hold", captain={"pickup_json": '{"repositories":{"fixture":null}}'},
        hold=True)
    add("flags-queue-hold-expired", expire_hold=True)
    add("flags-first-attempt-exact-ninety", first_attempt=ago(5400))
    add("flags-first-attempt-above-ninety", first_attempt=ago(5400 + .000001))
    add("flags-new-attempt-does-not-reset-age", current_attempt=ago(600),
        state_edits={"status": "running"}, writer={"last_progress_at": STAMP, "progress_note": "Current progress \u96ea"})
    add("flag-current-age-context", "PUT", body={**scoped, "kind": "current_feedback", "reason": "Named active blocker"})
    add("writer-current-active-age", path="/v1/writer", token=h.WRITER)
    base = f"/v1/items/{h.SEED}/executions/{h.SEED_EXECUTION}"
    pull = {"number": 7, "state": "open", "merged": False, "draft": False,
            "head_ref": "phalanx/" + h.digest(h.SEED.encode()), "head_sha": "b" * 40,
            "base_ref": "other", "base_sha": "d" * 40, "mergeable": False}
    observation = {"observed_at": ago(60), "pr": pull, "target_base": "main", "checks": "failed",
                   "check_reasons": ["Synthetic required check failed"], "feedback": ["Review item"]}
    add("flag-observe-head-drift", "POST", base + "/github-observation", observation, token=h.RUNNER,
        state_edits={"candidate": "a" * 40})
    add("flag-local-checks-unicode-lines", "POST", base + "/local-checks",
        {"head": "b" * 40, "failures": ["First line\u2028Second line"]}, token=h.RUNNER)
    add("flags-drift-suppresses-old-head-blockers")
    add("flags-current-head-blockers", state_edits={"candidate": "b" * 40})
    add("flags-handoff-head-priority", handoff="c" * 40)
    add("flags-observation-exact-stale", handoff=None, observation_age=ago(900))
    add("writer-current-observation-flags", path="/v1/writer", token=h.WRITER)
    add("flag-reconciler-heartbeat", "POST", "/v1/reconcilers/synthetic/heartbeat", {}, token=h.RUNNER)
    add("flags-reconciler-exact-stale", reconciler_age=ago(900))
    merged = {**observation, "observed_at": ago(30), "pr": {**pull, "state": "closed", "merged": True,
        "base_ref": "main", "merge_commit": "c" * 40, "merge_method": "merge", "merged_at": ago(60)},
        "deployment": {"commit": "c" * 40, "check_name": "Synthetic deploy", "state": "pending"}}
    add("flag-deployment-terminal-observation", "POST", base + "/github-observation", merged, token=h.RUNNER,
        terminal=True)
    add("flags-deployment-below-twenty-four", closed_at=ago(86400 - .000001))
    add("flags-deployment-exact-twenty-four", closed_at=ago(86400))
    add("flag-deployment-success-expired", "POST", base + "/github-observation",
        {**merged, "observed_at": ago(10), "deployment": {**merged["deployment"], "state": "deployed"}}, token=h.RUNNER)
    add("flags-deployment-success-retired")
    for token in (None, h.WRITER):
        add("flags-role-" + str(token), token=token)
        add("flag-write-role-" + str(token), "PUT", body=flag, token=token)
    for token in (None, h.OWNER, h.RUNNER):
        add("writer-current-role-" + str(token), path="/v1/writer", token=token)
    add("flags-method", "DELETE")
    add("writer-current-method", "POST", "/v1/writer", {}, token=h.WRITER)
    add("flags-trailing-slashes", path="/v1/flags///")
    return result


def fixture(database, case):
    with closing(sqlite3.connect(database)) as db:
        for key, table, identifier in (("writer", "writers", "fixture-writer"), ("captain", "captains", "fixture-captain")):
            if key in case:
                values = case[key]
                columns = ",".join(name + "=?" for name in values)
                db.execute(f"UPDATE {table} SET {columns} WHERE id=?", (*values.values(), identifier))
        if "dispatch" in case:
            values = case["dispatch"]
            db.execute("UPDATE dispatch_settings SET " + ",".join(name+"=?" for name in values) + " WHERE id=1", tuple(values.values()))
        if "advanced" in case:
            db.execute("UPDATE operator_progress SET last_advanced_at=? WHERE execution_id=?", (case["advanced"], h.SEED_EXECUTION))
        if "queue_ready" in case:
            db.execute("UPDATE executions SET ready_since=? WHERE id=?", (case["queue_ready"], QUEUE_EXECUTION))
        if case.get("hold"):
            db.execute("INSERT INTO captain_actions VALUES ('synthetic-hold',?,'fixture-captain',1,'held','Explicit hold',?)",
                       (QUEUE_EXECUTION, STAMP))
        if case.get("expire_hold"):
            db.execute("UPDATE captain_actions SET created_at=? WHERE id='synthetic-hold'", (ago(1800),))
        if "first_attempt" in case:
            db.execute("""INSERT INTO attempts VALUES ('synthetic-first-attempt',?,'report_drafting',?,?)
                ON CONFLICT(id) DO UPDATE SET started_at=excluded.started_at""", (h.SEED_EXECUTION, case["first_attempt"], STAMP))
        if "current_attempt" in case:
            db.execute("INSERT INTO attempts VALUES ('synthetic-current-attempt',?,'report_drafting',?,NULL)",
                       (h.SEED_EXECUTION, case["current_attempt"]))
        if "state_edits" in case:
            state, = db.execute("SELECT state_json FROM executions WHERE id=?", (h.SEED_EXECUTION,)).fetchone()
            state = {**json.loads(state), **case["state_edits"]}
            db.execute("UPDATE executions SET state_json=? WHERE id=?", (json.dumps(state), h.SEED_EXECUTION))
        if "handoff" in case:
            db.execute("UPDATE executions SET handoff_head=? WHERE id=?", (case["handoff"], h.SEED_EXECUTION))
        if "observation_age" in case:
            db.execute("UPDATE github_observations SET last_observed_at=? WHERE execution_id=?", (case["observation_age"], h.SEED_EXECUTION))
        if "reconciler_age" in case:
            db.execute("UPDATE reconciler_heartbeats SET last_seen=?", (case["reconciler_age"],))
        if case.get("terminal"):
            state, = db.execute("SELECT state_json FROM executions WHERE id=?", (h.SEED_EXECUTION,)).fetchone()
            state = json.loads(state)
            state.update(target="checked_in", stage="delivery", status="completed", candidate="b"*40,
                         primary_pr="https://github.com/example/fixture/pull/7", wait=None,
                         final_output={"url": "https://github.com/example/fixture/pull/7",
                             "revision": {"head": "b"*40, "base": "d"*40}, "draft": False,
                             "merge_commit": "c"*40, "closed": True, "merge_method": "merge", "merged_at": ago(60)})
            db.execute("UPDATE executions SET state_json=? WHERE id=?", (json.dumps(state), h.SEED_EXECUTION))
        if "closed_at" in case:
            db.execute("UPDATE executions SET updated_at=? WHERE id=?", (case["closed_at"], h.SEED_EXECUTION))
        db.commit()


def differential(binary, output):
    h.verify_manifest()
    h.verify_vectors()
    output.mkdir(parents=True, exist_ok=True)
    with h.disposable_root() as root:
        sources = {label: h.export(label, root) for label in h.PINS}
        for retained in (False, True):
            shape = "retained" if retained else "fresh"
            expected = None
            for label in ("P0", "D0", "Go"):
                directory = root / f"flags-{shape}-{label}"
                database = h.fresh(directory, sources, retained)
                command = [str(binary), "--fixture", str(database)] if label == "Go" else [
                    sys.executable, "-P", str(h.HERE / "oracle.py"), "serve", str(sources[label]), str(database), "--flags-clock"]
                actual, tokens = [], {}
                with h.server(command, directory) as url:
                    for case in cases():
                        fixture(database, case)
                        before = h.snapshot(database)
                        response = h.wire(url, case["method"], case["path"], case["headers"], case["body"])
                        after = h.snapshot(database)
                        if (response["status"] >= 400 or case["method"] == "GET") and before != after:
                            raise AssertionError("Flag read/refusal changed durable data: " + case["id"])
                        token = after["tables"]["operator_change"]["rows"][0][0]["value"]
                        tokens.setdefault(token, len(tokens))
                        actual.append({"request": case, "response": response, "durable": after,
                                       "change_token_ordinal": tokens[token]})
                (output / f"flags-{shape}-{label}.json").write_text(json.dumps(actual, ensure_ascii=True), encoding="utf-8")
                if expected is None:
                    expected = actual
                    continue
                for first, second in zip(expected, actual, strict=True):
                    if h.comparable(first["response"]) != h.comparable(second["response"]):
                        raise AssertionError(f"{shape}/{label}/{first['request']['id']}: wire mismatch\n"
                                             f"{h.comparable(first['response'])!r}\n{h.comparable(second['response'])!r}")
                    if h.comparable_durable(first) != h.comparable_durable(second):
                        raise AssertionError(f"{shape}/{label}/{first['request']['id']}: durable mismatch")
            print(f"{shape}: {len(expected)} stored/derived/scoped flag vectors x P0,D0,Go PASS")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    differential(args.binary.resolve(), args.output.resolve())


if __name__ == "__main__":
    main()

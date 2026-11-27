"""Real-loopback writer binding/capacity/dispatch tests on existing synthetic allocations."""

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys

import harness as h


TOKEN2 = "conformance-second-writer-only-000000000000"
TOKEN3 = "conformance-delivery-writer-only-0000000000"
ITEM2, EXECUTION2 = f"{0x1100:032x}", f"{0x1101:032x}"
ITEM3, EXECUTION3 = f"{0x1102:032x}", f"{0x1103:032x}"
STAMP = "2026-01-02T03:04:05.123456+00:00"


def seed(database):
    with closing(sqlite3.connect(database)) as db:
        for writer, item, execution, token, building in (
                ("fixture-second", ITEM2, EXECUTION2, TOKEN2, 1),
                ("fixture-delivery", ITEM3, EXECUTION3, TOKEN3, 0)):
            db.execute("""INSERT INTO writers
                (id,execution_id,item_id,captain_id,runtime,nonce,token_digest,state,created_at,building)
                VALUES (?,?,?,'fixture-captain','synthetic-runtime',?,?,'idle',?,?)""",
                       (writer, execution, item, "synthetic-valid-" + writer, h.digest(token.encode()), STAMP, building))
        state, = db.execute("SELECT state_json FROM executions WHERE id=?", (EXECUTION3,)).fetchone()
        state = json.loads(state)
        state.update(stage="delivery", status="waiting_for_external",
                     primary_pr="https://github.com/example/fixture/pull/42",
                     candidate="a" * 40,
                     wait={"reason": "GitHub checks, reviews and merge of the primary PR",
                           "trigger": "reconciler observation"})
        state = json.dumps(state, sort_keys=True, separators=(",", ":"))
        db.execute("UPDATE executions SET state_json=?,ready_since=NULL WHERE id=?", (state, EXECUTION3))
        db.execute("UPDATE transition_history SET state_json=? WHERE execution_id=?", (state, EXECUTION3))
        db.commit()


def cases():
    result = []
    writer1 = f"/v1/items/{h.SEED}/executions/{h.SEED_EXECUTION}/writer"
    writer2 = f"/v1/items/{ITEM2}/executions/{EXECUTION2}/writer"
    registration = {"expected_version": 1, "captain_id": "fixture-captain",
                    "runtime": "synthetic-runtime", "nonce": "synthetic-valid-fixture-second",
                    "token_digest": h.digest(TOKEN2.encode())}

    def add(name, method="POST", path="/v1/writer/progress", body=None, token=h.WRITER, raw=None, headers=()):
        fields = [] if token is None else [("Authorization", "Bearer " + token)]
        fields += [("Content-Type", "application/json"), *headers]
        result.append({"id": name, "method": method, "path": path, "headers": fields,
                       "body": raw if raw is not None else (json.dumps(body) if body is not None else "")})

    add("dispatch-saved", "GET", "/v1/dispatch/settings", token=h.OWNER)
    add("registered-nonce-replay", path=writer2, body=registration, token=h.RUNNER)
    add("nonce-replay-ignores-version", path=writer2, body={**registration, "expected_version": 99}, token=h.OWNER)
    add("nonce-conflicting-runtime", path=writer2, body={**registration, "runtime": "changed"}, token=h.OWNER)
    add("new-admission-paused", path=writer2, body={**registration, "nonce": "new-admission-synthetic",
                                                "token_digest": h.digest(b"new-token")}, token=h.OWNER)
    add("writer-claims-existing-building-while-paused", path="/v1/writer/attempt")
    add("writer-attempt-retry-is-current", path="/v1/writer/attempt")
    add("writer-progress-empty", body={})
    add("writer-progress-unicode", body={"note": "  Native progress \u96ea  "})
    add("writer-progress-preserves-note", body={"note": None})
    add("writer-cannot-release-report-building", path="/v1/writer/capacity",
        body={"phase": "delivering", "expected_revision": 0})
    add("delivery-request-building-paused", path="/v1/writer/capacity", token=TOKEN3,
        body={"phase": "building", "expected_revision": 0})
    add("delivery-capacity-stale", path="/v1/writer/capacity", token=TOKEN3,
        body={"phase": "building", "expected_revision": 0})
    add("delivery-cancels-building-request", path="/v1/writer/capacity", token=TOKEN3,
        body={"phase": "delivering", "expected_revision": 1})
    add("confirm-native-session", "PATCH", writer1,
        {"captain_id": "fixture-captain", "writer_id": "fixture-writer",
         "session_id": "synthetic-native", "session_url": "runtime://synthetic-native"}, token=h.RUNNER)
    add("different-native-session-refused", "PATCH", writer1,
        {"captain_id": "fixture-captain", "writer_id": "fixture-writer", "session_id": "different"}, token=h.OWNER)
    for name, tail in (("unicode-space", "native\u00a0session"), ("tail-bound", "x" * 2000),
                       ("tail-above", "x" * 2001), ("info-separator", "native\u001csession")):
        add("session-url-" + name, "PATCH", writer1,
            {"captain_id": "fixture-captain", "writer_id": "fixture-writer", "session_url": "runtime://" + tail},
            token=h.OWNER)
    for token in (None, h.OWNER, h.RUNNER):
        add("writer-required-" + str(token), body={}, token=token)
    add("coordinator-required-for-update", "PATCH", writer1,
        {"captain_id": "fixture-captain", "writer_id": "fixture-writer", "state": "idle"})
    add("withdraw-confirmed-requires-stop", path=writer1 + "/withdraw",
        body={"captain_id": "fixture-captain", "writer_id": "fixture-writer", "reason": "stopped"},
        token=h.OWNER)
    for name, value in (("boolean-text", "false"), ("boolean-number", 0), ("boolean-invalid", 2),
                        ("boolean-fraction", 0.1), ("boolean-null", None)):
        add("writer-abandon-" + name, "PATCH", writer1,
            {"captain_id": "fixture-captain", "writer_id": "fixture-writer", "abandon": value}, token=h.OWNER)
    for name, body in (
        ("null-before-extra", {"unknown": None}), ("strict-bool", {"paused": 1}),
        ("strict-int", {"max_writers": 3.0}), ("positive", {"max_writers": 0}),
        ("upper", {"max_writers": 2147483648}), ("pair", {"max_writers": 1}),
        ("extra", {"unknown": 1})):
        add("dispatch-" + name, "PUT", "/v1/dispatch/settings", body, token=h.OWNER)
    add("dispatch-resume", "PUT", "/v1/dispatch/settings", {"paused": False}, token=h.OWNER)
    add("new-admission-total-full", path=writer2,
        body={**registration, "nonce": "new-admission-synthetic", "token_digest": h.digest(b"new-token")},
        token=h.RUNNER)
    add("delivery-request-building-exhausted", path="/v1/writer/capacity", token=TOKEN3,
        body={"phase": "building", "expected_revision": 2})
    add("withdraw-first-writer", path=writer1 + "/withdraw",
        body={"captain_id": "fixture-captain", "writer_id": "fixture-writer", "reason": "stopped", "stopped": True},
        token=h.OWNER)
    add("revoked-first-writer", body={})
    add("delivery-acquires-freed-building", path="/v1/writer/capacity", token=TOKEN3,
        body={"phase": "building", "expected_revision": 3})
    add("delivery-release-needs-handoff", path="/v1/writer/capacity", token=TOKEN3,
        body={"phase": "delivering", "expected_revision": 4})
    add("delivery-release-exact-head", path="/v1/writer/capacity", token=TOKEN3,
        body={"phase": "delivering", "expected_revision": 4, "handoff_head": "b" * 40})
    add("dispatch-after-release", "GET", "/v1/dispatch/settings", token=h.RUNNER)
    add("withdraw-unconfirmed-writer", path=writer2 + "/withdraw",
        body={"captain_id": "fixture-captain", "writer_id": "fixture-second", "reason": "never launched"},
        token=h.OWNER)
    add("ended-nonce-replay", path=writer2, body=registration, token=h.OWNER)
    add("writer-progress-method", "GET")
    add("writer-update-method", "GET", writer1, token=h.OWNER)
    add("writer-repeated-slash", path="/v1/writer/attempt///", token=TOKEN3)
    add("dispatch-method", "POST", "/v1/dispatch/settings", token=h.OWNER)
    return result


def sequence(url, database):
    observations, tokens = [], {}
    for case in cases():
        before = h.snapshot(database)
        response = h.wire(url, case["method"], case["path"], case["headers"], case["body"])
        after = h.snapshot(database)
        if response["status"] >= 400 and before != after:
            raise AssertionError("Writer refusal changed durable state: " + case["id"])
        token = after["tables"]["operator_change"]["rows"][0][0]["value"]
        tokens.setdefault(token, len(tokens))
        observations.append({"request": case, "response": response, "durable": after,
                             "change_token_ordinal": tokens[token]})
    return observations


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
                directory = root / f"writers-{shape}-{label}"
                database = h.fresh(directory, sources, retained)
                seed(database)
                command = [str(binary), "--fixture", str(database)] if label == "Go" else [
                    sys.executable, "-P", str(h.HERE / "oracle.py"), "serve", str(sources[label]), str(database)]
                with h.server(command, directory) as url:
                    actual = sequence(url, database)
                (output / f"writers-{shape}-{label}.json").write_text(
                    json.dumps(actual, ensure_ascii=True), encoding="utf-8")
                if expected is None:
                    expected = actual
                    continue
                for first, second in zip(expected, actual, strict=True):
                    if h.comparable(first["response"]) != h.comparable(second["response"]):
                        raise AssertionError(f"{shape}/{label}/{first['request']['id']}: wire mismatch\n"
                                             f"{h.comparable(first['response'])!r}\n"
                                             f"{h.comparable(second['response'])!r}")
                    if h.comparable_durable(first) != h.comparable_durable(second):
                        raise AssertionError(f"{shape}/{label}/{first['request']['id']}: durable mismatch")
            print(f"{shape}: {len(expected)} writer/capacity/dispatch vectors x P0,D0,Go PASS")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    differential(args.binary.resolve(), args.output.resolve())


if __name__ == "__main__":
    main()

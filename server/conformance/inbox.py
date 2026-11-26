"""Exact owner inbox ordering, query coercion, authorization and cursor comparison."""

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
from urllib.parse import urlencode

import harness as h


STAMP = "2026-01-02T03:04:05.123456+00:00"


def seed(database):
    with closing(sqlite3.connect(database)) as db:
        for execution_id, stamp in (
                (h.SEED_EXECUTION, STAMP),
                (f"{0x1101:032x}", "2026-01-02T03:04:05.123457+00:00")):
            state, = db.execute("SELECT state_json FROM executions WHERE id=?", (execution_id,)).fetchone()
            state = json.loads(state)
            state["status"] = "waiting_for_input"
            state["wait"] = {"reason": "Synthetic question \u96ea", "trigger": "owner reply"}
            text = json.dumps(state, sort_keys=True, separators=(",", ":"))
            details = json.dumps({"stage": state["stage"], "outcome": "needs_input", "question": {
                "prompt": "Synthetic owner question \u96ea", "options": ["Retry", "Defer"],
                "proposed_default": None, "handoff": "Synthetic context " + execution_id,
            }}, sort_keys=True, separators=(",", ":"))
            db.execute("UPDATE executions SET version=2,state_json=?,ready_since=NULL,updated_at=? WHERE id=?",
                       (text, stamp, execution_id))
            db.execute("INSERT INTO transition_history VALUES (?,2,NULL,'result',?,?,?)",
                       (execution_id, details, text, stamp))
        db.commit()


def cases():
    result = []

    def add(name, query=(), token=h.OWNER, method="GET", suffix=""):
        headers = [] if token is None else [("Authorization", "Bearer " + token)]
        path = "/v1/owner/inbox" + suffix
        if query:
            path += "?" + urlencode(query)
        result.append({"id": name, "method": method, "path": path, "headers": headers, "body": ""})

    add("inbox-default")
    add("inbox-limit-one", [("limit", "1")])
    add("inbox-time-id-cursor", [("after_time", STAMP), ("after_id", h.SEED_EXECUTION)])
    add("inbox-time-only-cursor", [("after_time", STAMP)])
    add("inbox-time-after-all", [("after_time", "z" * 40)])
    add("inbox-duplicate-last-limit", [("limit", "0"), ("limit", "1")])
    add("inbox-duplicate-last-invalid-limit", [("limit", "1"), ("limit", "0")])
    add("inbox-unknown-query", [("unknown", "ignored")])
    for name, value in (
        ("zero", "0"), ("negative", "-1"), ("maximum", "100"), ("above", "101"),
        ("leading-plus", "+001"), ("zero-decimal", "1.00"), ("leading-zero", "01"),
        ("exponent", "1e0"), ("underscore", "1_0"), ("double-underscore", "1__0"),
        ("underscore-prefix", "_1"), ("underscore-suffix", "1_"),
        ("fraction", "0.1"), ("underscore-decimal", "1_0.0"), ("decimal-underscore", "1.0_0"),
        ("blank", ""), ("space", " 1 "), ("unicode-space", "\u00a01\u00a0"),
        ("ascii-control-space", "\u001c1\u001c"), ("negative-zero", "-0"),
        ("unicode-digit", "\u0661"), ("integer-at-size-bound", "9" * 4300),
        ("integer-above-size-bound", "9" * 4301),
        ("spaces-above-size-bound", " " + "9" * 4300 + " "),
        ("decimal-above-size-bound", "1." + "0" * 4300),
        ("leading-zero-size-bound", "0" * 4301 + "1"),
        ("signed-size-bound", "+" + "9" * 4300),
        ("leading-zero-large-size-bound", "0" * 100 + "9" * 4300),
        ("underscores-size-bound", "1_" * 2200 + "1"),
        ("invalid-size-bound", "x" * 4301),
    ):
        add("inbox-query-" + name, [("limit", value)])
    add("inbox-time-too-long", [("after_time", "x" * 41)])
    add("inbox-invalid-id", [("after_id", "!")])
    add("inbox-empty-id", [("after_id", "")])
    add("inbox-errors-array-order", [("limit", "0"), ("after_time", "x" * 41), ("after_id", "!")])
    add("inbox-duplicate-last-time", [("after_time", "x" * 41), ("after_time", STAMP)])
    for token in (None, h.RUNNER, h.WRITER):
        add("inbox-role-" + str(token), token=token)
        add("inbox-role-invalid-query-" + str(token), [("limit", "0")], token=token)
    add("inbox-trailing-slash", suffix="/")
    add("inbox-repeated-trailing-slash", suffix="///")
    add("inbox-method", method="POST")
    return result


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
                directory = root / f"inbox-{shape}-{label}"
                database = h.fresh(directory, sources, retained)
                seed(database)
                command = [str(binary), "--fixture", str(database)] if label == "Go" else [
                    sys.executable, "-P", str(h.HERE / "oracle.py"), "serve", str(sources[label]), str(database)]
                observations = []
                with h.server(command, directory) as url:
                    for case in cases():
                        before = h.snapshot(database)
                        response = h.wire(url, case["method"], case["path"], case["headers"], "")
                        after = h.snapshot(database)
                        if before != after:
                            raise AssertionError("Inbox read changed durable state: " + case["id"])
                        observations.append({"request": case, "response": response, "durable": after,
                                             "change_token_ordinal": 0})
                (output / f"inbox-{shape}-{label}.json").write_text(
                    json.dumps(observations, ensure_ascii=True), encoding="utf-8")
                if expected is None:
                    expected = observations
                else:
                    for first, second in zip(expected, observations, strict=True):
                        if h.comparable(first["response"]) != h.comparable(second["response"]):
                            raise AssertionError(f"{shape}/{label}/{first['request']['id']}: wire mismatch\n"
                                                 f"{h.comparable(first['response'])!r}\n"
                                                 f"{h.comparable(second['response'])!r}")
                        if h.comparable_durable(first) != h.comparable_durable(second):
                            raise AssertionError(f"{shape}/{label}/{first['request']['id']}: durable mismatch")
            print(f"{shape}: {len(expected)} owner inbox/query vectors x P0,D0,Go PASS")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    differential(args.binary.resolve(), args.output.resolve())


if __name__ == "__main__":
    main()

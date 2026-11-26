"""Native archive evidence and ownership checks, without native archive actions."""

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys

import harness as h


def cases():
    result = []

    def add(name, method="GET", session="synthetic-native", body=None, token=h.OWNER):
        path = "/v1/session-archives"
        if method == "GET" and session is not None:
            path += "?session_id=" + session
        headers = [] if token is None else [("Authorization", "Bearer " + token)]
        headers.append(("Content-Type", "application/json"))
        result.append({"id": name, "method": method, "path": path, "headers": headers,
                       "body": json.dumps(body) if body is not None else ""})

    add("archive-live-writer")
    add("archive-live-writer-refused", "PUT", body={"session_id": "synthetic-native", "outcome": "archived",
                                                 "reason": "Unsafe", "reported_by": "captain"})
    add("archive-live-writer-blocked", "PUT", body={"session_id": "synthetic-native", "outcome": "blocked",
                                                 "reason": "Still owns work", "reported_by": "captain"})
    add("archive-unknown-refused", "PUT", body={"session_id": "unknown", "outcome": "refused",
                                             "reason": "Runtime refusal", "reported_by": "captain"})
    add("archive-refusal-kept", "PUT", body={"session_id": "unknown", "outcome": "blocked",
                                         "reason": "Cannot erase refusal", "reported_by": "captain"})
    add("archive-unknown-verified", "PUT", body={"session_id": "unknown", "outcome": "archived",
                                             "reason": "Now archived", "reported_by": "captain"})
    add("archive-verified-kept", "PUT", body={"session_id": "unknown", "outcome": "refused",
                                          "reason": "Cannot undo evidence", "reported_by": "captain"})
    add("archive-live-captain", session="synthetic-session")
    add("archive-live-captain-refused", "PUT", body={"session_id": "synthetic-session", "outcome": "archived",
                                                  "reason": "Unsafe", "reported_by": "captain"})
    add("archive-query-required", session=None)
    add("archive-query-blank", session="")
    add("archive-extra-field", "PUT", body={"session_id": "x", "outcome": "blocked", "reason": "x",
                                          "reported_by": "captain", "extra": True})
    for token in (None, h.WRITER):
        add("archive-coordinator-" + str(token), token=token)
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
                directory = root / f"archives-{shape}-{label}"
                database = h.fresh(directory, sources, retained)
                with closing(sqlite3.connect(database)) as db:
                    db.execute("UPDATE writers SET session_id='synthetic-native',confirmed_at=created_at WHERE id='fixture-writer'")
                    db.commit()
                command = [str(binary), "--fixture", str(database)] if label == "Go" else [
                    sys.executable, "-P", str(h.HERE / "oracle.py"), "serve", str(sources[label]), str(database),
                    "--captain-clock"]
                actual, tokens = [], {}
                with h.server(command, directory) as url:
                    for case in cases():
                        before = h.snapshot(database)
                        response = h.wire(url, case["method"], case["path"], case["headers"], case["body"])
                        after = h.snapshot(database)
                        if response["status"] >= 400 and before != after:
                            raise AssertionError("Archive refusal changed durable state: " + case["id"])
                        token = after["tables"]["operator_change"]["rows"][0][0]["value"]
                        tokens.setdefault(token, len(tokens))
                        actual.append({"request": case, "response": response, "durable": after,
                                       "change_token_ordinal": tokens[token]})
                (output / f"archives-{shape}-{label}.json").write_text(
                    json.dumps(actual, ensure_ascii=True), encoding="utf-8")
                if expected is None:
                    expected = actual
                    continue
                for first, second in zip(expected, actual, strict=True):
                    if h.comparable(first["response"]) != h.comparable(second["response"]):
                        raise AssertionError(f"{shape}/{label}/{first['request']['id']}: wire mismatch\n"
                                             f"{h.comparable(first['response'])!r}\n{h.comparable(second['response'])!r}")
                    if h.comparable_durable(first) != h.comparable_durable(second):
                        raise AssertionError(f"{shape}/{label}/{first['request']['id']}: durable mismatch")
            print(f"{shape}: {len(expected)} archive/ownership vectors x P0,D0,Go PASS")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    differential(args.binary.resolve(), args.output.resolve())


if __name__ == "__main__":
    main()

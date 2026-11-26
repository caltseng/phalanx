"""Owner stop/finalization route comparison on disposable fresh and retained stores."""

import argparse
import json
from pathlib import Path
import sys

import harness as h


def cases():
    result = []
    created_item, created_execution = f"{1:032x}", f"{2:032x}"
    base = f"/v1/items/{created_item}/executions/{created_execution}"

    def add(name, method="POST", path=base + "/cancel", body="", token=h.OWNER, key=None):
        headers = [] if token is None else [("Authorization", "Bearer " + token)]
        headers.append(("Content-Type", "application/json"))
        if key is not None:
            headers.append(("Idempotency-Key", key))
        result.append({"id": name, "method": method, "path": path, "body": body, "headers": headers})

    add("create-to-cancel", path="/v1/items", body='{"repository_id":"fixture","goal":"Cancel native owner work"}')
    add("cancel-stale-version", body='{"expected_version":2,"reason":"wrong version"}', key="stale")
    for name, raw in (
        ("cancel-missing-version", '{"reason":"reason"}'),
        ("cancel-missing-reason", '{"expected_version":1}'),
        ("cancel-float-version", '{"expected_version":1.0,"reason":"reason"}'),
        ("cancel-negative-version", '{"expected_version":0,"reason":"reason"}'),
        ("cancel-unbounded-version", '{"expected_version":99999999999999999999999999,"reason":"reason"}'),
        ("cancel-extra-field", '{"expected_version":1,"reason":"reason","extra":true}'),
        ("cancel-blank-reason", '{"expected_version":1,"reason":"  " }'),
        ("cancel-null-body", 'null'),
        ("cancel-empty-body", ''),
    ):
        add(name, body=raw, key=name)
    for token in (None, h.RUNNER, h.WRITER):
        add("cancel-role-" + str(token), body='{"expected_version":1,"reason":"reason"}', token=token)
    add("cancel-queued", body='{"expected_version":1,"reason":"  Unicode \\u96ea stop  "}', key="cancel")
    add("cancel-replay", body='{"expected_version":1,"reason":"Unicode \\u96ea stop"}', key="cancel")
    add("cancel-conflicting-key", body='{"expected_version":1,"reason":"other reason"}', key="cancel")
    add("cancel-closed", body='{"expected_version":3,"reason":"closed"}', key="closed")
    add("inspect-cancelled", "GET", f"/v1/items/{created_item}")
    add("cancelled-history", "GET", f"/v1/items/{created_item}/executions")
    add("cancelled-timeline", "GET", base + "/timeline")
    add("retry-nonstalled-finalization", path=base + "/retry-finalization", body='{"expected_version":3}', key="retry")
    add("cancel-writer-bound-report", path=f"/v1/items/{h.SEED}/executions/{h.SEED_EXECUTION}/cancel",
        body='{"expected_version":1,"reason":"Seed cancellation"}', key="bound")
    add("ended-writer-history", "GET", f"/v1/items/{h.SEED}/executions", token=h.WRITER)
    add("cancel-wrong-method", "GET")
    add("cancel-trailing-slash", path=base + "/cancel/")
    return result


def sequence(url, database):
    observations, tokens = [], {}
    for case in cases():
        before = h.snapshot(database)
        response = h.wire(url, case["method"], case["path"], case["headers"], case["body"])
        after = h.snapshot(database)
        if response["status"] >= 400 and before != after:
            raise AssertionError("Refusal changed durable state: " + case["id"])
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
                directory = root / f"cancellation-{shape}-{label}"
                database = h.fresh(directory, sources, retained)
                command = [str(binary), "--fixture", str(database)] if label == "Go" else [
                    sys.executable, "-P", str(h.HERE / "oracle.py"), "serve",
                    str(sources[label]), str(database)]
                with h.server(command, directory) as url:
                    actual = sequence(url, database)
                (output / f"cancellation-{shape}-{label}.json").write_text(
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
            print(f"{shape}: {len(expected)} stop/finalization vectors x P0,D0,Go PASS")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    differential(args.binary.resolve(), args.output.resolve())


if __name__ == "__main__":
    main()

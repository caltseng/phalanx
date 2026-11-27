"""Additional native history/title wire and durable comparisons, without refreezing oracles."""

import argparse
import json
from pathlib import Path
import sys

import harness as h


def cases():
    result = []
    title = f"/v1/items/{h.SEED}/title"
    execution = f"/v1/items/{h.SEED}/executions/{h.SEED_EXECUTION}"

    def add(name, method="GET", path=title, body="", token=h.OWNER, key=None, headers=()):
        fields = [] if token is None else [("Authorization", "Bearer " + token)]
        fields += [("Content-Type", "application/json")]
        if key is not None:
            fields.append(("Idempotency-Key", key))
        fields += list(headers)
        result.append({"id": name, "method": method, "path": path, "body": body, "headers": fields})

    add("title-empty")
    add("history-existing", path=f"/v1/items/{h.SEED}/executions")
    add("timeline-existing", path=execution + "/timeline")
    add("history-missing", path="/v1/items/missing/executions")
    add("timeline-missing", path=execution.replace(h.SEED, "missing") + "/timeline")
    add("draft-missing", path=execution + "/drafts/" + "a" * 64)
    add("draft-invalid-revision", path=execution + "/drafts/INVALID")
    add("timeline-invalid-execution", path=execution.replace(h.SEED_EXECUTION, "%21") + "/timeline")
    add("writer-other-execution", path=execution.replace(h.SEED_EXECUTION, "another-execution") + "/timeline",
        token=h.WRITER)
    for token in (None, h.RUNNER, h.WRITER):
        add("title-role-" + str(token), token=token)
        add("history-role-" + str(token), token=token, path=f"/v1/items/{h.SEED}/executions")
        add("timeline-role-" + str(token), token=token, path=execution + "/timeline")
    add("title-put", "PUT", body='{"expected_revision":0,"title":"  Snow \\u96ea  "}', key="set-title")
    add("title-replay", "PUT", body='{"expected_revision":0,"title":"Snow \\u96ea"}', key="set-title")
    add("title-conflicting-key", "PUT", body='{"expected_revision":0,"title":"Changed"}', key="set-title")
    add("title-stale", "PUT", body='{"expected_revision":0,"title":"Changed"}', key="stale")
    add("title-current")
    add("title-cleared", "PUT", body='{"expected_revision":1,"title":null}', key="clear")
    add("title-replay-after-clear", "PUT", body='{"expected_revision":0,"title":"Snow \\u96ea"}', key="set-title")
    add("title-current-cleared")
    add("title-key-required", "PUT", body='{"expected_revision":2,"title":"required"}')
    for name, raw in (
        ("missing-title", '{"expected_revision":2}'),
        ("missing-revision", '{"title":null}'),
        ("float-revision", '{"expected_revision":2.0,"title":null}'),
        ("string-revision", '{"expected_revision":"2","title":null}'),
        ("bool-revision", '{"expected_revision":true,"title":null}'),
        ("negative-revision", '{"expected_revision":-1,"title":null}'),
        ("unbounded-revision", '{"expected_revision":999999999999999999999999999999,"title":null}'),
        ("blank-title", '{"expected_revision":2,"title":"  " }'),
        ("newline-title", '{"expected_revision":2,"title":"two\\nlines"}'),
        ("long-title", json.dumps({"expected_revision": 2, "title": "x" * 201})),
        ("extra-title", '{"expected_revision":2,"title":null,"extra":1}'),
        ("null-body", 'null'),
        ("malformed-body", '{"title":'),
        ("empty-body", ''),
        ("discarded-surrogate", '{"expected_revision":2,"title":"\\ud800","title":"surviving"}'),
        ("surviving-surrogate", '{"expected_revision":3,"title":"\\ud800"}'),
    ):
        add(name, "PUT", body=raw, key=name)
    add("cross-origin-title", "PUT", body='{"expected_revision":3,"title":"wrong origin"}',
        key="cross-origin", headers=[("Origin", "https://other.invalid")])
    add("title-trailing-slash", path=title + "/")
    add("title-repeated-trailing-slash", path=title + "///")
    add("history-trailing-slash", path=f"/v1/items/{h.SEED}/executions/")
    add("timeline-trailing-slash", path=execution + "/timeline/")
    add("title-wrong-method", "POST")
    add("history-wrong-method", "POST", path=f"/v1/items/{h.SEED}/executions")
    add("repository-wrong-method", "PUT", path="/v1/repositories")
    return result


def sequence(url, database):
    observations, tokens = [], {}
    for case in cases():
        before = h.snapshot(database)
        response = h.wire(url, case["method"], case["path"], case["headers"], case["body"])
        after = h.snapshot(database)
        if response["status"] >= 400 and before != after:
            raise AssertionError("Refusal changed durable data: " + case["id"])
        token = after["tables"]["operator_change"]["rows"][0][0]["value"]
        tokens.setdefault(token, len(tokens))
        observations.append({"request": case, "response": response, "durable": after,
                             "change_token_ordinal": tokens[token]})
    return observations


def differential(binary, output):
    h.verify_manifest()
    h.verify_vectors()
    h.verify_comparator()
    output.mkdir(parents=True, exist_ok=True)
    with h.disposable_root() as root:
        sources = {label: h.export(label, root) for label in h.PINS}
        for retained in (False, True):
            shape = "retained" if retained else "fresh"
            expected = None
            for label in ("P0", "D0", "Go"):
                directory = root / f"history-{shape}-{label}"
                database = h.fresh(directory, sources, retained)
                command = [str(binary), "--fixture", str(database)] if label == "Go" else [
                    sys.executable, "-P", str(h.HERE / "oracle.py"), "serve",
                    str(sources[label]), str(database)]
                with h.server(command, directory) as url:
                    actual = sequence(url, database)
                (output / f"history-{shape}-{label}.json").write_text(
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
            print(f"{shape}: {len(expected)} history/title vectors x P0,D0,Go PASS")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    differential(args.binary.resolve(), args.output.resolve())


if __name__ == "__main__":
    main()

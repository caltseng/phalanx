"""Version-fenced owner steering/rerun comparison without clock normalization."""

import argparse
import json
from pathlib import Path
import sys

import harness as h


def cases():
    result = []
    item, execution = f"{1:032x}", f"{2:032x}"
    base = f"/v1/items/{item}/executions/{execution}"

    def add(name, method="PATCH", path=base, body=None, key=None, token=h.OWNER):
        headers = [] if token is None else [("Authorization", "Bearer " + token)]
        headers.append(("Content-Type", "application/json"))
        if key is not None:
            headers.append(("Idempotency-Key", key))
        result.append({"id": name, "method": method, "path": path, "headers": headers,
                       "body": json.dumps(body) if body is not None else ""})

    add("steer-create", "POST", "/v1/items", {"repository_id": "fixture", "goal": "Original PR scope"})
    add("steer-empty", body={"expected_version": 1})
    add("steer-queued", body={"expected_version": 1, "goal": "  Revised \u96ea  ", "priority": "urgent",
                             "label": "sample", "context_links": [], "acceptance_criteria": ["Preserve clients"]}, key="steer")
    add("steer-replay", body={"expected_version": 1, "goal": "Revised \u96ea", "priority": "urgent",
                            "label": "sample", "context_links": [], "acceptance_criteria": ["Preserve clients"]}, key="steer")
    add("steer-key-conflict", body={"expected_version": 1, "goal": "Changed"}, key="steer")
    add("steer-target", body={"expected_version": 2, "target": "ready_for_check_in", "steered_by": "captain"})
    add("steer-null-edit", body={"expected_version": 3, "goal": None})
    add("steer-report-target", body={"expected_version": 3, "target": "report"})
    add("steer-self-cycle", body={"expected_version": 3, "dependencies": [{"upstream_id": item}]})
    add("steer-clear-label", body={"expected_version": 3, "label": None})
    add("rerun-open-refused", "POST", base + "/rerun", {"expected_version": 4, "target": "checked_in"})
    add("steer-cancel", "POST", base + "/cancel", {"expected_version": 4, "reason": "Owner disposition"})
    add("steer-closed-refused", body={"expected_version": 6, "goal": "Closed scope"})
    add("rerun-output-type-refused", "POST", base + "/rerun", {"expected_version": 6, "target": "report"})
    add("rerun-closed", "POST", base + "/rerun", {"expected_version": 6, "target": "checked_in"}, key="rerun")
    add("rerun-replay", "POST", base + "/rerun", {"expected_version": 6, "target": "checked_in"}, key="rerun")
    add("rerun-item-current", "GET", f"/v1/items/{item}")
    add("rerun-execution-history", "GET", f"/v1/items/{item}/executions")
    add("steer-retains-report", "GET", base + "/report")
    for token in (None, h.RUNNER, h.WRITER):
        add("steer-role-" + str(token), body={"expected_version": 1, "goal": "Denied"}, token=token)
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
                directory = root / f"steering-{shape}-{label}"
                database = h.fresh(directory, sources, retained)
                command = [str(binary), "--fixture", str(database)] if label == "Go" else [
                    sys.executable, "-P", str(h.HERE / "oracle.py"), "serve", str(sources[label]), str(database)]
                actual, tokens = [], {}
                with h.server(command, directory) as url:
                    for case in cases():
                        before = h.snapshot(database)
                        response = h.wire(url, case["method"], case["path"], case["headers"], case["body"])
                        after = h.snapshot(database)
                        if response["status"] >= 400 and before != after:
                            raise AssertionError("Steering refusal changed durable state: " + case["id"])
                        token = after["tables"]["operator_change"]["rows"][0][0]["value"]
                        tokens.setdefault(token, len(tokens))
                        actual.append({"request": case, "response": response, "durable": after,
                                       "change_token_ordinal": tokens[token]})
                (output / f"steering-{shape}-{label}.json").write_text(
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
            print(f"{shape}: {len(expected)} owner steering/rerun vectors x P0,D0,Go PASS")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    differential(args.binary.resolve(), args.output.resolve())


if __name__ == "__main__":
    main()

"""Every pinned OpenAPI operation/model is inventoried and exercised without mutations."""

import argparse
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import re
import sqlite3
import sys

import harness as h


def inventory():
    schema = json.loads((h.ROOT / "server" / "internal" / "httpapi" / "contract-docs" / "openapi.json").read_text())
    operations = []
    replacements = {"item_id": h.SEED, "execution_id": h.SEED_EXECUTION, "writer_id": "fixture-writer",
                    "captain_id": "fixture-captain", "runner_id": "missing-reconciler",
                    "repository_id": "fixture", "pass_id": "missing-pass", "revision": "a"*64,
                    "label": "missing-label", "number": "1"}
    for template, methods in schema["paths"].items():
        path = re.sub(r"\{([^}]+)\}", lambda match: replacements[match[1]], template)
        for method, description in methods.items():
            token = h.OWNER
            if method == "get" and (path == "/v1/writer" or path.startswith("/v1/writer/")):
                token = h.WRITER
            if path == "/v1/runner/status":
                token = h.RUNNER
            operations.append({"id": method.upper() + " " + template, "method": method.upper(), "path": path,
                "headers": [("Content-Type", "application/json"), ("Authorization", "Bearer " + token)],
                "body": "{}" if method != "get" else "", "operation_id": description["operationId"]})
    return schema, operations


def comparable(response, case):
    response = deepcopy(response)
    if case["path"] == "/v1/muster/status" and response["json"]:
        response["json"]["identity"]["home"] = "$DISPOSABLE_HOME"
    return h.comparable(response)


def differential(binary, output):
    h.verify_manifest()
    h.verify_vectors()
    schema, requests = inventory()
    if len(requests) != 66 or len(schema["components"]["schemas"]) != 51:
        raise AssertionError("Pinned operation/model inventory drifted")
    output.mkdir(parents=True, exist_ok=True)
    (output / "inventory.json").write_text(json.dumps({"pins": h.PINS, "operations": requests,
        "models": sorted(schema["components"]["schemas"]), "schema_sha256": h.digest(json.dumps(schema, sort_keys=True).encode())},
        indent=2), encoding="utf-8")
    with h.disposable_root() as root:
        sources = {label: h.export(label, root) for label in h.PINS}
        for retained in (False, True):
            shape = "retained" if retained else "fresh"
            expected = None
            for label in ("P0", "D0", "Go"):
                directory = root / f"routes-{shape}-{label}"
                database = h.fresh(directory, sources, retained)
                with closing(sqlite3.connect(database)) as db:
                    db.execute("UPDATE operator_change SET token='AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'")
                    db.commit()
                command = [str(binary), "--fixture", str(database)] if label == "Go" else [
                    sys.executable, "-P", str(h.HERE / "oracle.py"), "serve", str(sources[label]), str(database),
                    "--operator-clock", "--captain-clock", "--flags-clock"]
                actual = []
                with h.server(command, directory) as url:
                    before = h.snapshot(database)
                    openapi = h.wire(url, "GET", "/openapi.json", [], "")["json"]
                    if openapi != schema:
                        raise AssertionError("Complete public request/model documentation mismatch: " + label)
                    for case in requests:
                        response = h.wire(url, case["method"], case["path"], case["headers"], case["body"])
                        after = h.snapshot(database)
                        if before != after:
                            raise AssertionError("Read/refusal inventory changed durable data: " + case["id"])
                        actual.append({"request": case, "response": response, "durable": after})
                (output / f"routes-{shape}-{label}.json").write_text(json.dumps(actual, ensure_ascii=True), encoding="utf-8")
                if expected is None:
                    expected = actual
                    continue
                for first, second in zip(expected, actual, strict=True):
                    if comparable(first["response"], first["request"]) != comparable(second["response"], first["request"]):
                        raise AssertionError(f"{shape}/{label}/{first['request']['id']}: route/model error mismatch\n"
                            f"{comparable(first['response'],first['request'])!r}\n{comparable(second['response'],first['request'])!r}")
            print(f"{shape}: all66 public operations and51 complete request-model schemas x P0,D0,Go PASS")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    differential(args.binary.resolve(), args.output.resolve())


if __name__ == "__main__":
    main()

"""Pinned Scribe bytes/CSP/static ranges and direct local-board auth on real loopback."""

import argparse
from contextlib import closing
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys

import harness as h


ASSETS = ("operator-board.css", "operator-board.html", "operator-board.js", "operator-helpers.js",
          "operator-http.js", "operator-markdown.js", "operator-shell.js", "phalanx-brand.svg")


def cases():
    result = []

    def add(name, method="GET", path="/operator/", body=None, token=None, headers=(), **extra):
        fields = [("Content-Type", "application/json"), *headers]
        if token is not None:
            fields.append(("Authorization", "Bearer " + token))
        result.append({"id": name, "method": method, "path": path, "headers": fields,
                       "body": json.dumps(body) if body is not None else "", **extra})

    add("scribe-shell")
    add("scribe-shell-root-redirect", path="/operator")
    add("scribe-shell-queue", path="/operator/queue")
    add("scribe-shell-history", path="/operator/history")
    add("scribe-shell-item", path="/operator/items/" + h.SEED)
    add("scribe-shell-execution", path="/operator/executions/" + h.SEED_EXECUTION)
    add("scribe-shell-id-invalid", path="/operator/items/invalid")
    add("scribe-shell-head", "HEAD")
    add("scribe-shell-token-ignored", token="invalid")
    add("scribe-shell-conditional-ignored", headers=[("If-None-Match", "*")])
    for path in ("/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"):
        add("scribe-existing-docs-" + path, path=path)
    add("scribe-existing-docs-method", "HEAD", "/openapi.json")
    for asset in ASSETS:
        add("scribe-asset-" + asset, path="/operator/assets/" + asset)
    asset = "/operator/assets/operator-board.js"
    add("scribe-static-validator", path=asset, save_headers=True)
    add("scribe-static-etag-304", path=asset, use_header="etag")
    add("scribe-static-weak-etag-304", path=asset, use_header="etag", weak=True)
    add("scribe-static-star-304", path=asset, headers=[("If-None-Match", "*")])
    add("scribe-static-modified-304", path=asset, use_header="last-modified")
    add("scribe-static-head", "HEAD", asset)
    add("scribe-static-root", path="/operator/assets")
    add("scribe-static-directory", path="/operator/assets/")
    add("scribe-static-missing", path="/operator/assets/missing.js")
    add("scribe-static-traversal", path="/operator/assets/../operator_web.py")
    add("scribe-static-dot-normalized", path="/operator/assets/unknown/../operator-board.js")
    add("scribe-static-null", path="/operator/assets/%00bad")
    add("scribe-static-method", "POST", asset, {})
    for name, value in (
            ("simple", "bytes=0-9"), ("suffix", "bytes=-9"), ("open-end", "bytes=9-"),
            ("clamped-end", "bytes=0-999999999"), ("spaces", "BYTES = 0 - 9"),
            ("underscores", "bytes=0_0-0_9"), ("bad-unit", "items=0-9"),
            ("malformed", "invalid"), ("empty", "bytes="), ("non-numeric", "bytes=abc"),
            ("out-of-range", "bytes=999999999-"), ("reversed", "bytes=9-0"),
            ("priority", "bytes=9-0,999999999-"), ("mixed-ignored", "bytes=bad,0-9"),
            ("too-many", "bytes=" + ",".join("0-1" for _ in range(101))),
            ("merged", "bytes=0-9,8-19"), ("multipart", "bytes=0-9,30-39")):
        add("scribe-range-" + name, path=asset, headers=[("Range", value)], multipart=name == "multipart")
    add("scribe-range-head", "HEAD", asset, headers=[("Range", "bytes=0-9")])
    add("scribe-range-if-range-etag", path=asset, headers=[("Range", "bytes=0-9")], if_range="etag")
    add("scribe-range-if-range-date", path=asset, headers=[("Range", "bytes=0-9")], if_range="last-modified")
    add("scribe-range-if-range-mismatch", path=asset, headers=[("Range", "bytes=0-9"), ("If-Range", '"wrong"')])
    add("scribe-shell-range", headers=[("Range", "bytes=0-9")])
    add("scribe-board-repositories", path="/operator/api/repositories")
    add("scribe-board-dispatch", path="/operator/api/dispatch/settings")
    add("scribe-board-read-bearer-owner", path="/operator/api/repositories", token=h.OWNER)
    add("scribe-board-read-bearer-runner", path="/operator/api/repositories", token=h.RUNNER)
    add("scribe-board-read-writer", path="/operator/api/repositories", token=h.WRITER)
    add("scribe-board-read-empty-authorization", path="/operator/api/repositories", headers=[("Authorization", "")])
    add("scribe-local-operator-overview", path="/v1/operator/overview")
    add("scribe-local-captains", path="/v1/captains")
    add("scribe-local-flags", path="/v1/flags")
    add("scribe-local-scout", path="/v1/issue-scout")
    add("scribe-v1-owner-probe-not-local", path="/v1/owner/status")
    add("scribe-v1-repositories-not-local", path="/v1/repositories")
    add("scribe-v1-write-not-local", "POST", "/v1/items",
        {"repository_id": "fixture", "goal": "No local authority here"}, headers=[("Origin", "$ORIGIN")])
    body = {"repository_id": "fixture", "goal": "Synthetic board work"}
    add("scribe-board-write-origin-required", "POST", "/operator/api/items", body)
    add("scribe-board-write-origin", "POST", "/operator/api/items", body, headers=[("Origin", "$ORIGIN")])
    add("scribe-board-write-owner-nonbrowser", "POST", "/operator/api/items",
        {**body, "goal": "Synthetic bearer board work"}, token=h.OWNER)
    add("scribe-board-write-wrong-origin", "POST", "/operator/api/items", body, headers=[("Origin", "https://invalid.example")])
    add("scribe-board-write-model", "POST", "/operator/api/items", {"goal": None}, headers=[("Origin", "$ORIGIN")])
    add("scribe-board-dispatch-write", "PUT", "/operator/api/dispatch/settings", {"paused": False},
        headers=[("Origin", "$ORIGIN")])
    add("scribe-v1-dispatch-write-not-local", "PUT", "/v1/dispatch/settings", {"paused": False},
        headers=[("Origin", "$ORIGIN")])
    add("scribe-board-decision-str-repo", "POST", "/operator/api/issue-scout/!/-1/decision",
        {"expected_fingerprint": "a"*64, "decision": "approved"}, token=h.OWNER)
    add("scribe-v1-decision-identifier", "POST", "/v1/issue-scout/issues/!/-1/decision",
        {"expected_fingerprint": "a"*64, "decision": "approved"}, token=h.OWNER)
    for name, headers in (
            ("forwarded", [("Forwarded", "for=127.0.0.1")]), ("x-forwarded", [("X-Forwarded-For", "127.0.0.1")]),
            ("real-ip", [("X-Real-IP", "127.0.0.1")]), ("untrusted-host", [("Host", "invalid.example")]),
            ("localhost-host", [("Host", "localhost")]), ("ipv6-host", [("Host", "[::1]")]),
            ("origin-cross", [("Origin", "https://invalid.example")]),
            ("duplicate-origin", [("Origin", "$ORIGIN"), ("Origin", "$ORIGIN")]),
            ("fetch-cross", [("Sec-Fetch-Site", "cross-site")]),
            ("fetch-same-no-origin", [("Sec-Fetch-Site", "same-origin")])):
        add("scribe-local-read-" + name, path="/operator/api/repositories", headers=headers)
    return result


def verify_assets(sources):
    evidence = {"pins": h.PINS, "assets": {}}
    for name in ASSETS:
        reference = subprocess.check_output(["git", "--no-pager", "show", h.PINS["P0"] + ":phalanx/static/" + name],
                                            cwd=h.ROOT, timeout=60)
        value = {"size": len(reference), "sha256": h.digest(reference), "implementations": {}}
        for label in ("P0", "D0"):
            actual = (sources[label] / "phalanx" / "static" / name).read_bytes()
            if actual != reference:
                raise AssertionError("Pinned Scribe asset differs: " + label + "/" + name)
            value["implementations"][label] = h.digest(actual)
        actual = subprocess.check_output(["git", "--no-pager", "show", "HEAD:phalanx/static/" + name], cwd=h.ROOT, timeout=60)
        if actual != reference:
            raise AssertionError("Go package Scribe bytes changed: " + name)
        value["implementations"]["Go"] = h.digest(actual)
        checkout = (h.ROOT / "phalanx" / "static" / name).read_bytes()
        if checkout != actual and checkout.replace(b"\r\n", b"\n") != actual:
            raise AssertionError("Worktree Scribe asset has non-checkout content drift: " + name)
        value["checkout"] = {"sha256": h.digest(checkout), "size": len(checkout),
                             "canonical_bytes": checkout == actual,
                             "fixture_source": "Immutable HEAD Git blob, not wire newline normalization"}
        evidence["assets"][name] = value
    return evidence


def normalize(response, case):
    response = deepcopy(response)
    if case.get("multipart") and response["status"] == 206:
        content_type = next(value for name, value in response["headers"] if name == "content-type")
        match = re.fullmatch(r"multipart/byteranges; boundary=([0-9a-f]{26})", content_type)
        if not match:
            raise AssertionError("Malformed generated multipart boundary")
        boundary = match[1]
        raw = bytes.fromhex(response["body_hex"])
        if raw.count(boundary.encode()) != 3:
            raise AssertionError("Multipart boundary did not fence exactly two ranges")
        response["text"] = response["text"].replace(boundary, "$BOUNDARY")
        response["headers"] = [(name, value.replace(boundary, "$BOUNDARY")) for name, value in response["headers"]]
    return h.comparable(response)


def sequence(url, database):
    actual, tokens, saved = [], {}, {}
    for case in cases():
        request = deepcopy(case)
        if "use_header" in case:
            name = case["use_header"]
            value = saved[name]
            if case.get("weak"):
                value = "W/" + value
            request["headers"].append(("If-None-Match" if name == "etag" else "If-Modified-Since", value))
        if "if_range" in case:
            request["headers"].append(("If-Range", saved[case["if_range"]]))
        with closing(sqlite3.connect(database)) as db:
            db.execute("UPDATE operator_change SET token='AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'")
            db.commit()
        before = h.snapshot(database)
        response = h.wire(url, request["method"], request["path"], request["headers"], request["body"])
        after = h.snapshot(database)
        if (request["method"] in ("GET", "HEAD") or response["status"] >= 400) and before != after:
            raise AssertionError("Scribe read/refusal changed durable data: " + case["id"])
        if response["status"] not in (304,) and request["method"] != "HEAD":
            lengths = [value for name, value in response["headers"] if name == "content-length"]
            if lengths and int(lengths[0]) != len(bytes.fromhex(response["body_hex"])):
                raise AssertionError("Static response length does not match actual bytes")
        if case.get("save_headers"):
            saved = dict(response["headers"])
        token = after["tables"]["operator_change"]["rows"][0][0]["value"]
        tokens.setdefault(token, len(tokens))
        actual.append({"request": request, "response": response, "durable": after,
                       "change_token_ordinal": tokens[token]})
    return actual


def differential(binary, output):
    h.verify_manifest()
    h.verify_vectors()
    output.mkdir(parents=True, exist_ok=True)
    with h.disposable_root() as root:
        sources = {label: h.export(label, root) for label in h.PINS}
        (output / "assets-proof.json").write_text(json.dumps(verify_assets(sources), indent=2), encoding="utf-8")
        for label in ("P0", "D0"):
            for name in ASSETS:
                os.utime(sources[label] / "phalanx" / "static" / name, (1500000000.125, 1500000000.125))
        for retained in (False, True):
            for local_board in (False, True):
                shape = ("retained" if retained else "fresh") + ("-local" if local_board else "-bearer")
                expected = None
                for label in ("P0", "D0", "Go"):
                    directory = root / f"scribe-{shape}-{label}"
                    database = h.fresh(directory, sources, retained)
                    assets = directory / "assets"
                    assets.mkdir()
                    for name in ASSETS:
                        content = subprocess.check_output(["git", "--no-pager", "show", "HEAD:phalanx/static/" + name],
                                                          cwd=h.ROOT, timeout=60)
                        (assets / name).write_bytes(content)
                        os.utime(assets / name, (1500000000.125, 1500000000.125))
                    command = [str(binary), "--fixture", str(database), "--fixture-assets"] if label == "Go" else [
                        sys.executable, "-P", str(h.HERE / "oracle.py"), "serve", str(sources[label]), str(database),
                        "--operator-clock", "--captain-clock", "--flags-clock"]
                    if local_board:
                        command.append("--local-board")
                    with h.server(command, directory) as url:
                        actual = sequence(url, database)
                    (output / f"scribe-{shape}-{label}.json").write_text(json.dumps(actual, ensure_ascii=True), encoding="utf-8")
                    if expected is None:
                        expected = actual
                        continue
                    for first, second in zip(expected, actual, strict=True):
                        case = first["request"]
                        if normalize(first["response"], case) != normalize(second["response"], case):
                            raise AssertionError(f"{shape}/{label}/{case['id']}: wire mismatch\n"
                                f"{normalize(first['response'],case)!r}\n{normalize(second['response'],case)!r}")
                        if h.comparable_durable(first) != h.comparable_durable(second):
                            raise AssertionError(f"{shape}/{label}/{case['id']}: durable mismatch")
                print(f"{shape}: {len(expected)} Scribe/static/local-board vectors x P0,D0,Go PASS")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    differential(args.binary.resolve(), args.output.resolve())


if __name__ == "__main__":
    main()

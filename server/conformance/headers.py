"""Production-launcher incomplete-header boundaries, raw wire and typed stores."""

import argparse
from contextlib import closing
import http.client
import io
import json
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import time
from urllib.parse import urlsplit

import harness as h
import framework
import release


def comparable_responses(raw):
    class Buffer(io.BytesIO):
        def close(self):
            pass

    class Socket:
        def makefile(self, mode):
            return buffer

    result = []
    while raw:
        buffer = Buffer(raw)
        response = http.client.HTTPResponse(Socket())
        response.begin()
        body = response.read()
        result.append(framework.comparable({
            "protocol": "HTTP/1.1" if response.version == 11 else "HTTP/1.0",
            "status": response.status,
            "headers": [(name.lower(), value) for name, value in response.getheaders()],
            "decoded_body_hex": body.hex(), "closed": True,
        }))
        raw = raw[buffer.tell():]
    return result


def cases():
    result = []
    for size in (16383, 16384, 16385, 17000, 33000, 64000):
        result.append((f"incomplete-{size}", size, size, False, False))
        result.append((f"complete-{size}", size, None, False, False))
    for size in (16383, 16384):
        result.append((f"split-then-complete-{size}", 33000, size, False, False))
    for split in (16383, 16384, 16385):
        result.append((f"split-terminator-{split}", split + 1, split, True, False))
    result.append(("refused-mutation", 17000, 17000, False, True))
    result.append(("refused-intake-receipt", 17000, 17000, False, "intake"))
    for ending in ("lf", "multiple-fields"):
        result.append((ending + "-incomplete", 17000, 17000, False, False))
        result.append((ending + "-complete", 64000, None, False, False))
    for previous in ("pipeline", "fixed-body", "chunked-body"):
        result.append((previous + "-incomplete", 17000, 17000, False, False))
        result.append((previous + "-complete", 64000, None, False, False))
    return result


def post_prefix_cases():
    prefixes = [("leading-cr", b"\r"), ("leading-lf", b"\n"),
                ("leading-crlf", b"\r\n"), ("leading-space", b" "),
                ("leading-tab", b"\t"),
                ("prefixed-get", b"\r\nGET /health HTTP/1.1\r\nHost: localhost\r\n\r\n")]
    return [(f"{context}/{name}", prefix, context)
            for context in ("initial", "post-empty", "post-fixed", "post-chunked",
                            "coalesced-fixed", "coalesced-chunked")
            for name, prefix in prefixes
            if context == "post-empty" or name not in ("leading-crlf", "prefixed-get")]


def paused_response(client, timeout=.3):
    client.settimeout(timeout)
    raw, closed, close_error = b"", False, None
    while True:
        try:
            block = client.recv(65536)
        except TimeoutError:
            break
        except (ConnectionResetError, ConnectionAbortedError) as error:
            closed = True
            close_error = repr(error)
            break
        if not block:
            closed = True
            break
        raw += block
    return raw, closed, close_error


def post_prefixes(url, database):
    target = urlsplit(url)
    result = []
    initial = f"POST /health HTTP/1.1\r\nHost: {target.netloc}\r\n".encode()
    body = b'{"paused":false}'
    oversized = ((f"PUT /v1/dispatch/settings HTTP/1.1\r\nHost: {target.netloc}\r\n"
                  f"Authorization: Bearer {h.OWNER}\r\nContent-Type: application/json\r\n"
                  f"Content-Length: {len(body)}\r\nConnection: close\r\nX-Boundary: ").encode()
                 + b"x" * 17000)
    for name, prefix, context in post_prefix_cases():
        if context == "initial":
            requests = (prefix, oversized)
        else:
            if context.endswith("fixed"):
                first = initial + b"Content-Length: 2\r\n\r\n{}"
            elif context.endswith("chunked"):
                first = initial + b"Transfer-Encoding: chunked\r\n\r\n2\r\n{}\r\n0\r\n\r\n"
            else:
                first = initial + b"Content-Length: 0\r\n\r\n"
            requests = (first + prefix, oversized) if context.startswith("coalesced") else (first, prefix, oversized)
        stages = []
        with socket.create_connection((target.hostname, target.port), timeout=10) as client:
            for index, request in enumerate(requests):
                before = h.snapshot(database)
                client.sendall(request)
                raw, closed, close_error = paused_response(client)
                early = raw
                suffix = b""
                if index == len(requests) - 1 and not raw and not closed:
                    suffix = b"\r\n\r\n" + body
                    client.sendall(suffix)
                    raw, closed, close_error = paused_response(client, timeout=10)
                after = h.snapshot(database)
                stages.append({"request_hex": request.hex(), "suffix_hex": suffix.hex(),
                               "early_raw_hex": early.hex(), "raw_hex": raw.hex(),
                               "responses": comparable_responses(raw), "closed": closed,
                               "close_error": close_error,
                               "before": before, "after": after, "durable_unchanged": before == after})
                if closed:
                    break
        result.append({"id": name, "stages": stages})
    return result


def exchange(url, case):
    name, size, split, terminator, mutation = case
    target = urlsplit(url)
    if mutation == "intake":
        body = b'{"repository_id":"fixture","goal":"Header refusal receipt neighbor"}'
        start = (f"POST /v1/items HTTP/1.1\r\nHost: {target.netloc}\r\n"
                 f"Authorization: Bearer {h.OWNER}\r\nContent-Type: application/json\r\n"
                 f"Content-Length: {len(body)}\r\nIdempotency-Key: header-refusal-neighbor\r\n").encode()
    elif mutation:
        body = b'{"paused":false}'
        start = (f"PUT /v1/dispatch/settings HTTP/1.1\r\nHost: {target.netloc}\r\n"
                 f"Authorization: Bearer {h.OWNER}\r\nContent-Type: application/json\r\n"
                 f"Content-Length: {len(body)}\r\n").encode()
    else:
        body = b""
        start = f"GET /health HTTP/1.1\r\nHost: {target.netloc}\r\n".encode()
    start += b"Connection: close\r\nX-Boundary: "
    separator = b"\r\n\r\n"
    if name.startswith("lf-"):
        start, separator = start.replace(b"\r\n", b"\n"), b"\n\n"
    if terminator:
        request = start + b"x" * (size - len(start) - len(separator)) + separator + body
    else:
        request = start + b"x" * (size - len(start)) + separator + body
    if name.startswith("multiple-fields-"):
        request = request.replace(b"x" * 100, b"x" * 92 + b"\r\nX-N: x")
    parts = [request] if split is None else [request[:split], request[split:]]
    previous = b""
    if name.startswith(("pipeline-", "fixed-body-", "chunked-body-")):
        previous = f"GET /health HTTP/1.1\r\nHost: {target.netloc}\r\n".encode()
        payload = b"x" * 17000 + b"\r\n\r\n" + b"y" * 17000
        if name.startswith("fixed-body-"):
            previous += f"Content-Length: {len(payload)}\r\n\r\n".encode() + payload
        elif name.startswith("chunked-body-"):
            previous += (b"Transfer-Encoding: chunked\r\n\r\n" +
                         f"{len(payload):x}\r\n".encode() + payload + b"\r\n0\r\nX-Trailer: yes\r\n\r\n")
        else:
            previous += b"\r\n"
        parts[0] = previous + parts[0]
    raw = b""
    early = False
    closed, close_error = False, None
    started = time.monotonic()
    first_elapsed = None
    with socket.create_connection((target.hostname, target.port), timeout=10) as client:
        client.sendall(parts[0])
        if split is not None:
            client.settimeout(.3)
            while True:
                try:
                    block = client.recv(65536)
                except TimeoutError:
                    break
                except (ConnectionResetError, ConnectionAbortedError) as error:
                    closed, close_error = True, repr(error)
                    break
                if not block:
                    closed = True
                    break
                if not raw:
                    first_elapsed = time.monotonic() - started
                raw += block
            early = b"HTTP/1.1 400 " in raw
        client.settimeout(10)
        if not early and not closed and len(parts) == 2:
            client.sendall(parts[1])
        while not closed:
            try:
                block = client.recv(65536)
            except (ConnectionResetError, ConnectionAbortedError) as error:
                closed = True
                close_error = repr(error)
                break
            if not block:
                closed = True
                break
            if not raw:
                first_elapsed = time.monotonic() - started
            raw += block
    refusals = raw.split(b"HTTP/1.1 400 Bad Request")[1:]
    return {"id": name, "parts_hex": [part.hex() for part in parts], "early": early,
            "first_response_seconds": first_elapsed, "total_seconds": time.monotonic() - started,
            "closed": closed, "raw_response": raw.decode("latin1"),
            "close_error": close_error,
            "refusals_hex": [part.hex() for part in refusals],
            "responses": comparable_responses(raw),
            "statuses": [int(status) for status in re.findall(rb"HTTP/1\.[01] (\d{3}) ", raw)],
            "status": int(raw.split(b" ", 2)[1]) if raw else None}


def differential(binary, output, record_only=False):
    h.verify_manifest()
    output.mkdir(parents=True, exist_ok=True)
    shutil.copytree(h.ROOT / "phalanx" / "static", binary.parent / "assets", dirs_exist_ok=True)
    failures = []
    with h.disposable_root() as root:
        sources = {label: h.export(label, root) for label in h.PINS}
        for retained in (False, True):
            shape = "retained" if retained else "fresh"
            expected = None
            for label in ("P0", "D0", "Go"):
                directory = root / f"headers-{shape}-{label}"
                database = h.fresh(directory, sources, retained)
                actual, framing = [], []
                with release.production(binary, database, directory,
                                        python_source=None if label == "Go" else sources[label]) as (url, _):
                    for case in cases():
                        before = h.snapshot(database)
                        observation = exchange(url, case)
                        after = h.snapshot(database)
                        observation.update(before=before, after=after, durable_unchanged=before == after)
                        actual.append(observation)
                        (output / f"headers-{shape}-{label}.json").write_text(json.dumps(actual), encoding="utf-8")
                    for case in framework.cases():
                        before = h.snapshot(database)
                        response = framework.wire(url, case["raw"])
                        after = h.snapshot(database)
                        framing.append({"case": case, "response": response, "before": before, "after": after})
                        if before != after:
                            failures.append(f"{shape}/{label}/{case['id']}: native framing changed durable state")
                    prefixes = post_prefixes(url, database)
                    (output / f"post-prefixes-{shape}-{label}.json").write_text(json.dumps(prefixes), encoding="utf-8")
                    neighbor = h.wire(url, "GET", "/health", [], "")
                    if neighbor["status"] != 200 or neighbor["json"] != {"status": "ok"}:
                        raise AssertionError("Refusal affected a valid neighboring connection")
                    before = h.snapshot(database)
                    receipt_neighbor = h.mutation(url, {"repository_id": "fixture",
                                                       "goal": "Header refusal receipt neighbor"},
                                                  "header-refusal-neighbor")
                    after = h.snapshot(database)
                    with closing(sqlite3.connect(database)) as db:
                        receipt_count, = db.execute(
                            "SELECT count(*) FROM request_receipts WHERE scope='intake' "
                            "AND idempotency_key='header-refusal-neighbor'").fetchone()
                    if (receipt_neighbor["status"] != 201 or receipt_count != 1 or
                            len(after["tables"]["work_items"]["rows"]) != len(before["tables"]["work_items"]["rows"]) + 1):
                        failures.append(f"{shape}/{label}: valid receipt neighbor was not the sole accepted intake")
                    (output / f"neighbors-{shape}-{label}.json").write_text(
                        json.dumps({"before": before, "response": receipt_neighbor, "after": after}), encoding="utf-8")
                (output / f"headers-{shape}-{label}.json").write_text(json.dumps(actual), encoding="utf-8")
                (output / f"framework-production-{shape}-{label}.json").write_text(json.dumps(framing), encoding="utf-8")
                if expected is None:
                    expected = actual
                    expected_framing = framing
                    expected_prefixes = prefixes
                for first, second in zip(expected, actual, strict=True):
                    for key in ("responses", "early", "closed", "durable_unchanged"):
                        if first[key] != second[key]:
                            failures.append(f"{shape}/{label}/{second['id']}/{key}: differs; exact wire/state retained")
                    if second["status"] == 400 and second["raw_response"] != first["raw_response"]:
                        failures.append(f"{shape}/{label}/{second['id']}: refusal bytes differ")
                for first, second in zip(expected_framing, framing, strict=True):
                    target = framework.approved_go(second["case"]) if label == "Go" else None
                    if target is None:
                        target = framework.comparable(first["response"])
                    if framework.comparable(second["response"]) != target:
                        failures.append(f"{shape}/{label}/{second['case']['id']}: native framing precedence changed")
                for first, second in zip(expected_prefixes, prefixes, strict=True):
                    if len(first["stages"]) != len(second["stages"]):
                        failures.append(f"{shape}/{label}/{second['id']}: malformed prefix did not close at its boundary")
                    for before, after in zip(first["stages"], second["stages"]):
                        for key in ("responses", "closed", "durable_unchanged"):
                            if before[key] != after[key]:
                                failures.append(f"{shape}/{label}/{second['id']}/{key}: prefix alignment differs")
                print(f"{shape}/{label}: {len(actual)} production header cases recorded", flush=True)
    (output / "failures.json").write_text(json.dumps(failures, indent=2), encoding="utf-8")
    if failures and not record_only:
        raise AssertionError("\n".join(failures))
    print("\n".join(failures) if failures else "Header boundaries PASS")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--record-only", action="store_true")
    args = parser.parse_args()
    differential(args.binary.resolve(), args.output.resolve(), args.record_only)


if __name__ == "__main__":
    main()

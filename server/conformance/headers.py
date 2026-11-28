"""Production-launcher incomplete-header boundaries, raw wire and typed stores."""

import argparse
from contextlib import ExitStack, closing
import http.client
import io
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import sqlite3
import time
from threading import Event, Thread
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


def parsed_responses(raw):
    try:
        return {"responses": comparable_responses(raw), "response_error": None}
    except (http.client.HTTPException, ValueError) as error:
        return {"responses": None, "response_error": repr(error)}


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
                               **parsed_responses(raw), "closed": closed,
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
            **parsed_responses(raw),
            "statuses": [int(status) for status in re.findall(rb"HTTP/1\.[01] (\d{3}) ", raw)],
            "status": int(raw.split(b" ", 2)[1]) if raw else None}


def response_preservation(url, database, reference):
    selected = [case for case in cases() if case[0] in ("fixed-body-complete", "chunked-body-complete")]
    before, records, passed = h.snapshot(database), [], True
    for index in range(100):
        for case in selected:
            record = exchange(url, case)
            after = h.snapshot(database)
            record.update(index=index, before=before, after=after)
            records.append(record)
            if (after != before or record["statuses"] != [200, 400] or not record["closed"] or
                    record["response_error"] is not None or
                    record["responses"] != reference[case[0]]):
                passed = False
    return records, passed


def refusal_ownership(url, process, database, sources, python):
    target = urlsplit(url)
    prefix = f"GET /health HTTP/1.1\r\nHost: {target.netloc}\r\nX-Refused: ".encode() + b"x" * 17000
    noise_body = b'{"repository_id":"fixture","goal":"Must not be dispatched"}'
    noise = ((f"POST /v1/items HTTP/1.1\r\nHost: {target.netloc}\r\n"
              f"Authorization: Bearer {h.OWNER}\r\nContent-Type: application/json\r\n"
              f"Idempotency-Key: refused-noise\r\nContent-Length: {len(noise_body)}\r\n\r\n").encode()
             + noise_body)
    baseline, peers = h.snapshot(database), []
    stop, sent, errors = Event(), [], []
    with ExitStack() as stack:
        for kind in ("quiet", "sending"):
            client = stack.enter_context(closing(socket.create_connection((target.hostname, target.port), timeout=10)))
            client.sendall(prefix)
            raw, closed, error = paused_response(client, timeout=10)
            after = h.snapshot(database)
            peers.append({"kind": kind, "request_hex": prefix.hex(), "raw_hex": raw.hex(),
                          "closed": closed, "close_error": error, "before": baseline, "after": after})
            if after != baseline or not closed or not raw.startswith(b"HTTP/1.1 400 "):
                raise AssertionError("Refusal did not finish its wire reply before peer EOF")
            if kind == "sending":
                def send_noise():
                    while not stop.is_set():
                        try:
                            client.sendall(noise)
                        except OSError as error:
                            errors.append(repr(error))
                            return
                        sent.append(len(noise))
                        stop.wait(.01)
                sender = Thread(target=send_noise)
                sender.start()
        try:
            with closing(sqlite3.connect(database, isolation_level=None)) as probe:
                probe.execute("BEGIN EXCLUSIVE")
                probe.execute("ROLLBACK")
            no_transaction = h.snapshot(database)
            if no_transaction != baseline:
                raise AssertionError("Refused peers mutated the store or retained a transaction")
            control = h.wire(url, "GET", "/v1/dispatch/settings", [("Authorization", "Bearer " + h.OWNER)], "")
            accepted = h.mutation(url, {"repository_id": "fixture", "goal": "Ordinary work while refused peers remain"},
                                  "refusal-runtime")
            if control["status"] != 200 or accepted["status"] != 201:
                raise AssertionError("Refused peers retained store admission or accepted-operation ownership")
            after = h.snapshot(database)
            with closing(sqlite3.connect(database)) as probe:
                refused_count, = probe.execute(
                    "SELECT count(*) FROM request_receipts WHERE idempotency_key='refused-noise'").fetchone()
                accepted_count, = probe.execute(
                    "SELECT count(*) FROM request_receipts WHERE scope='intake' AND idempotency_key='refusal-runtime'").fetchone()
            if refused_count != 0 or accepted_count != 1:
                raise AssertionError("Refused bytes were dispatched or ordinary work lost its receipt")
        finally:
            stop.set()
            sender.join(timeout=10)
            if sender.is_alive():
                raise AssertionError("Owned refused-peer sender did not stop")
        started = time.monotonic()
        if python:
            process.stdin.close()
        else:
            process.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGTERM)
        process.wait(timeout=15)
        if process.returncode != 0 or h.snapshot(database) != after:
            raise AssertionError("Quiet/sending refusal drain changed shutdown or durable effects")
        lock = h.invoke("lock", sources["P0"], database, capture_output=True, text=True)
        if lock.stdout.strip() != "acquired":
            raise AssertionError("Refused peers retained server ownership after shutdown")
        return {"peers": peers, "noise_hex": noise.hex(), "sent_bytes": sum(sent), "send_errors": errors,
                "before_control": no_transaction, "control": control, "accepted": accepted,
                "after_control": after, "after_shutdown": h.snapshot(database),
                "shutdown_seconds": time.monotonic() - started, "lock_probe": lock.stdout,
                "Go_ordinary_runtime_debt": None if python else {
                    "close_work": "only a finite snapshot of bytes already queued by the kernel; no wait for future input or peer EOF",
                    "prompt_disposal_evidence": "native HTTP tracking and raw-socket closure tests complete before peer EOF or lifetime cancellation",
                    "store_transaction": False, "store_admission": False, "accepted_operation": False,
                    "request_buffers_released": True, "service_shutdown_interrupts": True}}


def native_response(status, body, *, content_type="application/json", close=True, headers=()):
    fields = list(headers) + [("content-type", content_type)]
    if close:
        fields.append(("connection", "close"))
    if content_type == "application/json":
        body = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    return "HTTP/1.1", status, sorted(fields), body.encode().hex(), True


def native_refusal():
    return native_response(400, "Invalid HTTP request received.", content_type="text/plain; charset=utf-8")


def native_header(case):
    if case not in cases():
        raise AssertionError("Unclassified native header case")
    name, _, split, _, _ = case
    if not name.startswith(("incomplete-", "complete-", "split-then-complete-", "split-terminator-",
                            "pipeline-", "fixed-body-", "chunked-body-", "lf-", "multiple-fields-")) \
            and name not in ("refused-mutation", "refused-intake-receipt"):
        raise AssertionError("Unclassified native header case: " + name)
    previous = name.startswith(("pipeline-", "fixed-body-", "chunked-body-"))
    early = split is not None and split > 16384
    refused = early or name in ("fixed-body-complete", "chunked-body-complete")
    responses = [native_response(200, {"status": "ok"}, close=False)] if previous else []
    responses.append(native_refusal() if refused else native_response(200, {"status": "ok"}))
    result = {"id": name, "responses": responses, "early": early, "closed": True, "durable_unchanged": True}
    if not previous and refused:
        result["raw_response"] = ("HTTP/1.1 400 Bad Request\r\ncontent-type: text/plain; charset=utf-8\r\n"
                                  "Connection: close\r\n\r\nInvalid HTTP request received.")
    return result


def native_prefixes():
    method = native_response(405, {"detail": "Method Not Allowed"}, close=False, headers=(("allow", "GET"),))
    result = []
    for name, _, context in post_prefix_cases():
        if context == "initial":
            stages = [([native_refusal()], True)]
        elif context.startswith("coalesced"):
            stages = [([method, native_refusal()], True)]
        else:
            stages = [([method], False), ([native_refusal()], True)]
        result.append({"id": name, "stages": [
            {"responses": responses, "closed": closed, "durable_unchanged": True} for responses, closed in stages]})
    return result


def native_framing(case, before):
    if case not in framework.cases():
        raise AssertionError("Unclassified native framing case")
    approved = framework.approved_go(case)
    if approved is not None:
        return approved
    name = case["id"]
    if name in ("valid-http", "same-content-length", "http-1.0-without-host",
                "http10-health-with-host", "http10-health-keepalive"):
        response = native_response(200, {"status": "ok"})
    elif name == "invalid-chunk":
        response = native_refusal()
    elif name == "http10-repositories-close":
        table = before["tables"]["repositories"]
        rows = [dict(zip(table["columns"], (cell["value"] for cell in row), strict=True)) for row in table["rows"]]
        response = native_response(200, [{key: row[key] for key in ("id", "remote", "created_at")} for row in rows])
    elif name == "http10-repositories-denied":
        response = native_response(401, {"detail": "Valid bearer token required"},
                                   headers=(("www-authenticate", "Bearer"),))
    elif name == "http10-invalid-body":
        response = native_response(422, {"detail": [
            {"loc": ["body", field], "msg": "Field required"} for field in ("repository_id", "goal")]})
    elif name == "http10-head-method":
        response = native_response(405, {"detail": "Method Not Allowed"}, headers=(("allow", "GET"),))
        response = response[:3] + ("", response[4])
    else:
        raise AssertionError("Unclassified native framing case: " + name)
    return response


def header_differences(expected, actual):
    failures = []
    for first, second in zip(expected, actual, strict=True):
        for key in ("responses", "early", "closed", "durable_unchanged"):
            if first[key] != second[key]:
                failures.append(f"{second['id']}/{key}: differs; exact wire/state retained")
        if second["status"] == 400 and second["raw_response"] != first["raw_response"]:
            failures.append(f"{second['id']}: refusal bytes differ")
    return failures


def differential(binary, output, record_only=False, *, historical=False):
    h.verify_manifest()
    output.mkdir(parents=True, exist_ok=True)
    shutil.copytree(h.ROOT / "phalanx" / "static", binary.parent / "assets", dirs_exist_ok=True)
    failures = []
    with h.disposable_root() as root:
        sources = {label: h.export(label, root) for label in h.PINS}
        for retained in (False, True):
            shape = "retained" if retained else "fresh"
            expected = None if historical else [native_header(case) for case in cases()]
            expected_prefixes = None if historical else native_prefixes()
            for label in release.load_implementations(historical):
                directory = root / f"headers-{shape}-{label}"
                database = h.fresh(directory, sources, retained)
                actual, framing = [], []
                with release.production(binary, database, directory,
                                        python_source=None if label == "Go" else sources[label]) as (url, process):
                    for case in cases():
                        before = h.snapshot(database)
                        observation = exchange(url, case)
                        after = h.snapshot(database)
                        observation.update(before=before, after=after, durable_unchanged=before == after)
                        actual.append(observation)
                        if observation["response_error"] is not None:
                            failures.append(f"{shape}/{label}/{case[0]}: malformed response retained")
                        (output / f"headers-{shape}-{label}.json").write_text(json.dumps(actual), encoding="utf-8")
                    for case in framework.cases():
                        before = h.snapshot(database)
                        response = framework.wire(url, case["raw"])
                        after = h.snapshot(database)
                        framing.append({"case": case, "response": response, "before": before, "after": after})
                        if before != after:
                            failures.append(f"{shape}/{label}/{case['id']}: native framing changed durable state")
                        if not historical and framework.comparable(response) != native_framing(case, before):
                            failures.append(f"{shape}/{label}/{case['id']}: native framing contract changed")
                    prefixes = post_prefixes(url, database)
                    (output / f"post-prefixes-{shape}-{label}.json").write_text(json.dumps(prefixes), encoding="utf-8")
                    if any(stage["response_error"] is not None for record in prefixes for stage in record["stages"]):
                        failures.append(f"{shape}/{label}: malformed prefix response retained")
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
                    preservation, preserved = response_preservation(
                        url, database, {record["id"]: record["responses"]
                                        for record in (actual if historical else expected)})
                    (output / f"preservation-{shape}-{label}.json").write_text(json.dumps(preservation), encoding="utf-8")
                    if not preserved:
                        failures.append(f"{shape}/{label}: repeated coalesced refusal lost response or durable integrity")
                    ownership = refusal_ownership(url, process, database, sources, python=label != "Go")
                    (output / f"refusal-ownership-{shape}-{label}.json").write_text(json.dumps(ownership), encoding="utf-8")
                (output / f"headers-{shape}-{label}.json").write_text(json.dumps(actual), encoding="utf-8")
                (output / f"framework-production-{shape}-{label}.json").write_text(json.dumps(framing), encoding="utf-8")
                if expected is None:
                    expected = actual
                    expected_framing = framing
                    expected_prefixes = prefixes
                failures.extend(f"{shape}/{label}/{failure}" for failure in header_differences(expected, actual))
                for first, second in zip(expected_framing if historical else [], framing if historical else [], strict=True):
                    target = framework.approved_go(second["case"]) if label == "Go" else None
                    if target is None:
                        target = framework.comparable(first["response"])
                    if label == "Go" and second["case"]["id"] == "http10-invalid-body":
                        # Only the expected validation payload changed; keep the
                        # oracle's protocol/status/headers/closure and state checks.
                        expected_response = first["response"].copy()
                        payload = json.loads(bytes.fromhex(expected_response["decoded_body_hex"]))
                        payload = {"detail": [{"loc": issue["loc"], "msg": issue["msg"]}
                                              for issue in payload["detail"]]}
                        expected_response["decoded_body_hex"] = json.dumps(payload).encode().hex()
                        target = framework.comparable(expected_response)
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
    (output / "mode.json").write_text(json.dumps({
        "mode": "historical-differential" if historical else "native-runtime",
        "implementations": release.load_implementations(historical),
        "header_cases": [case[0] for case in cases()],
        "framing_cases": [case["id"] for case in framework.cases()],
        "prefix_cases": [case[0] for case in post_prefix_cases()],
        "preservation_repeats_per_case": 100,
        "expectations": "live P0 baseline" if historical else "explicit independent native contracts",
    }, indent=2), encoding="utf-8")
    if failures and not record_only:
        raise AssertionError("\n".join(failures))
    print("\n".join(failures) if failures else "Header boundaries PASS")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--record-only", action="store_true")
    parser.add_argument("--historical", action="store_true",
                        help="Also run the unchanged live P0/D0 header differential")
    args = parser.parse_args()
    differential(args.binary.resolve(), args.output.resolve(), args.record_only, historical=args.historical)


if __name__ == "__main__":
    main()

"""Production-launcher incomplete-header boundaries, raw wire and typed stores."""

import argparse
import json
from pathlib import Path
import re
import shutil
import socket
import time
from urllib.parse import urlsplit

import harness as h
import release


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
    for previous in ("pipeline", "fixed-body", "chunked-body"):
        result.append((previous + "-incomplete", 17000, 17000, False, False))
        result.append((previous + "-complete", 64000, None, False, False))
    return result


def exchange(url, case):
    name, size, split, terminator, mutation = case
    target = urlsplit(url)
    if mutation:
        body = b'{"paused":false}'
        start = (f"PUT /v1/dispatch/settings HTTP/1.1\r\nHost: {target.netloc}\r\n"
                 f"Authorization: Bearer {h.OWNER}\r\nContent-Type: application/json\r\n"
                 f"Content-Length: {len(body)}\r\n").encode()
    else:
        body = b""
        start = f"GET /health HTTP/1.1\r\nHost: {target.netloc}\r\n".encode()
    start += b"Connection: close\r\nX-Boundary: "
    if terminator:
        request = start + b"x" * (size - len(start) - 4) + b"\r\n\r\n" + body
    else:
        request = start + b"x" * (size - len(start)) + b"\r\n\r\n" + body
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
    started = time.monotonic()
    with socket.create_connection((target.hostname, target.port), timeout=10) as client:
        client.sendall(parts[0])
        if split is not None:
            client.settimeout(.3)
            while True:
                try:
                    block = client.recv(65536)
                except TimeoutError:
                    break
                if not block:
                    break
                raw += block
            early = b"HTTP/1.1 400 " in raw
        first_elapsed = time.monotonic() - started
        client.settimeout(10)
        if not early and len(parts) == 2:
            client.sendall(parts[1])
        closed = False
        while True:
            try:
                block = client.recv(65536)
            except (ConnectionResetError, ConnectionAbortedError):
                closed = True
                break
            if not block:
                closed = True
                break
            raw += block
    return {"id": name, "parts_hex": [part.hex() for part in parts], "early": early,
            "first_response_seconds": first_elapsed, "total_seconds": time.monotonic() - started,
            "closed": closed, "raw_response": raw.decode("latin1"),
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
                actual = []
                with release.production(binary, database, directory,
                                        python_source=None if label == "Go" else sources[label]) as (url, _):
                    for case in cases():
                        before = h.snapshot(database)
                        observation = exchange(url, case)
                        after = h.snapshot(database)
                        observation.update(before=before, after=after, durable_unchanged=before == after)
                        actual.append(observation)
                    neighbor = h.wire(url, "GET", "/health", [], "")
                    if neighbor["status"] != 200 or neighbor["json"] != {"status": "ok"}:
                        raise AssertionError("Refusal affected a valid neighboring connection")
                (output / f"headers-{shape}-{label}.json").write_text(json.dumps(actual), encoding="utf-8")
                if expected is None:
                    expected = actual
                for first, second in zip(expected, actual, strict=True):
                    for key in ("statuses", "early", "closed", "durable_unchanged"):
                        if first[key] != second[key]:
                            failures.append(f"{shape}/{label}/{second['id']}/{key}: {first[key]} != {second[key]}")
                    if second["status"] == 400 and second["raw_response"] != first["raw_response"]:
                        failures.append(f"{shape}/{label}/{second['id']}: refusal bytes differ")
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

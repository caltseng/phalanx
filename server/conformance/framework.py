"""Raw HTTP parser/body edge characterization; differences remain explicit blockers."""

import argparse
from copy import deepcopy
import json
from pathlib import Path
import socket
import sys
from urllib.parse import urlsplit

import harness as h


def cases():
    base = "GET /health HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n"
    return [
        {"id": "valid-http", "raw": base + "\r\n"},
        {"id": "duplicate-host", "raw": base + "Host: localhost\r\n\r\n"},
        {"id": "missing-host", "raw": "GET /health HTTP/1.1\r\nConnection: close\r\n\r\n"},
        {"id": "invalid-version", "raw": "GET /health HTTP/xyz\r\nHost: localhost\r\nConnection: close\r\n\r\n"},
        {"id": "invalid-percent-url", "raw": "GET /%GG HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"},
        {"id": "conflicting-content-length", "raw": base + "Content-Length: 0\r\nContent-Length: 1\r\n\r\n"},
        {"id": "same-content-length", "raw": base + "Content-Length: 0\r\nContent-Length: 0\r\n\r\n"},
        {"id": "unsupported-transfer-encoding", "raw": base + "Transfer-Encoding: gzip\r\n\r\n"},
        {"id": "invalid-header-name", "raw": base + "Bad Header: value\r\n\r\n"},
        {"id": "invalid-method", "raw": "G ET /health HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"},
        {"id": "http-1.0-without-host", "raw": "GET /health HTTP/1.0\r\nConnection: close\r\n\r\n"},
        {"id": "invalid-chunk", "raw": "POST /v1/items HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n"
            "Content-Type: application/json\r\nAuthorization: Bearer " + h.OWNER + "\r\nTransfer-Encoding: chunked\r\n\r\nZ\r\nbad\r\n0\r\n\r\n"},
    ]


def wire(url, data):
    parsed = urlsplit(url)
    with socket.create_connection((parsed.hostname, parsed.port), timeout=5) as client:
        client.sendall(data.encode("ascii"))
        response = bytearray()
        while True:
            try:
                chunk = client.recv(65536)
            except (socket.timeout, ConnectionResetError):
                break
            if not chunk:
                break
            response.extend(chunk)
    header, _, body = bytes(response).partition(b"\r\n\r\n")
    lines = header.decode("latin-1").split("\r\n")
    status_line = lines[0].split() if lines else []
    status = int(status_line[1]) if len(status_line) >= 2 else None
    headers = [(name.lower(), value.strip()) for name, _, value in (line.partition(":") for line in lines[1:])]
    decoded = body
    if any(name == "transfer-encoding" and value.lower() == "chunked" for name, value in headers):
        chunks, remaining = bytearray(), body
        while remaining:
            size, separator, remaining = remaining.partition(b"\r\n")
            if not separator:
                raise AssertionError("Malformed response chunk framing")
            length = int(size.split(b";", 1)[0], 16)
            if length == 0:
                break
            chunks.extend(remaining[:length])
            remaining = remaining[length+2:]
        decoded = bytes(chunks)
    return {"raw_hex": bytes(response).hex(), "protocol": status_line[0] if status_line else None,
            "status": status, "headers": headers,
            "body_hex": body.hex(), "decoded_body_hex": decoded.hex()}


def comparable(response):
    response = deepcopy(response)
    response["headers"] = sorted((name, value) for name, value in response["headers"]
                                if name not in ("date", "server", "content-length", "transfer-encoding"))
    return response["protocol"], response["status"], response["headers"], response["decoded_body_hex"]


def characterize(binary, output):
    h.verify_manifest()
    h.verify_vectors()
    output.mkdir(parents=True, exist_ok=True)
    mismatches = []
    with h.disposable_root() as root:
        sources = {label: h.export(label, root) for label in h.PINS}
        expected = None
        for label in ("P0", "D0", "Go"):
            directory = root / ("framework-" + label)
            database = h.fresh(directory, sources, True)
            command = [str(binary), "--fixture", str(database)] if label == "Go" else [
                sys.executable, "-P", str(h.HERE / "oracle.py"), "serve", str(sources[label]), str(database)]
            actual = []
            with h.server(command, directory) as url:
                for case in cases():
                    before = h.snapshot(database)
                    response = wire(url, case["raw"])
                    after = h.snapshot(database)
                    if before != after:
                        raise AssertionError("Raw HTTP framing changed durable state: " + case["id"])
                    actual.append({"case": case, "response": response, "durable": after})
            (output / f"framework-{label}.json").write_text(json.dumps(actual, indent=2), encoding="utf-8")
            if expected is None:
                expected = actual
            else:
                for first, second in zip(expected, actual, strict=True):
                    if comparable(first["response"]) != comparable(second["response"]):
                        mismatches.append({"implementation": label, "case": first["case"]["id"],
                            "P0_protocol": first["response"]["protocol"], "actual_protocol": second["response"]["protocol"],
                            "P0_headers": comparable(first["response"])[2], "actual_headers": comparable(second["response"])[2],
                            "P0_status": first["response"]["status"], "actual_status": second["response"]["status"],
                            "P0_body": bytes.fromhex(first["response"]["decoded_body_hex"]).decode("latin-1"),
                            "actual_body": bytes.fromhex(second["response"]["decoded_body_hex"]).decode("latin-1")})
    result = {"pins": h.PINS, "request_count": len(cases()), "mismatches": mismatches,
              "effect": "All raw framing/read/refusal cases zero durable effects",
              "classification": "Unresolved parser-level differences are blockers until explicitly classified; no status/body normalization"}
    (output / "framework-characterization.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    characterize(args.binary.resolve(), args.output.resolve())


if __name__ == "__main__":
    main()

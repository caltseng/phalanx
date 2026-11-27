"""Exact owner-v12 transport expectations; all other comparisons remain strict."""

import argparse
from copy import deepcopy
import json
from pathlib import Path
import socket
import sys
from urllib.parse import urlsplit

import harness as h


APPROVED = {
    "duplicate-host": ("471e3e2f6d2c72470f3aa20c8b94dbabd22df5e7fad3241cc031d97a8d9f1a8a", 400, "400 Bad Request"),
    "missing-host": ("2b318cedffd6575d4e21f3dbc669c0d1fa8439fe3fe2aa85d330884eefa0c339", 400, "400 Bad Request: missing required Host header"),
    "invalid-version": ("cb2fe1fd72032dbe27c5709a61f4c73525edf3ab464a7a324e0b0ebf99ee1a2e", 400, "400 Bad Request"),
    "invalid-percent-url": ("26f6c87d088320bbd58f7c65c5ad5c6040f188f7f844b3b82b80ec37a1cd6926", 400, "400 Bad Request"),
    "conflicting-content-length": ("d980e8cc60543985cdc348f1289187b76f66f570029efe9210e715bbd9ed2415", 400, "400 Bad Request"),
    "unsupported-transfer-encoding": ("350cd2844bc80f47ea29ae316290157214580cd3a7b4ec428e102b8ec4ba2f6b", 501, "Unsupported transfer encoding"),
    "invalid-header-name": ("f646284d23f24055b86c49360646a245fb9fbc173adf7248f75b8319d3720b29", 400, "400 Bad Request: invalid header name"),
    "invalid-method": ("30e482d51ee56170b24720f7ba17a4a775beb30b23ac17b80425e422d7c9f81c", 400, "400 Bad Request"),
}


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
        {"id": "http10-repositories-close", "raw": "GET /v1/repositories HTTP/1.0\r\nAuthorization: Bearer " +
            h.OWNER + "\r\nConnection: close\r\n\r\n"},
        {"id": "http10-health-with-host", "raw": "GET /health HTTP/1.0\r\nHost: localhost\r\nConnection: close\r\n\r\n"},
        {"id": "http10-health-keepalive", "raw": "GET /health HTTP/1.0\r\nHost: localhost\r\nConnection: keep-alive\r\n\r\n"},
        {"id": "http10-repositories-denied", "raw": "GET /v1/repositories HTTP/1.0\r\nHost: localhost\r\nConnection: close\r\n\r\n"},
        {"id": "http10-invalid-body", "raw": "POST /v1/items HTTP/1.0\r\nHost: localhost\r\nConnection: keep-alive\r\n"
            "Content-Type: application/json\r\nAuthorization: Bearer " + h.OWNER + "\r\nContent-Length: 2\r\n\r\n{}"},
        {"id": "http10-head-method", "raw": "HEAD /health HTTP/1.0\r\nHost: localhost\r\nConnection: close\r\n\r\n"},
    ]


def wire(url, data):
    parsed = urlsplit(url)
    with socket.create_connection((parsed.hostname, parsed.port), timeout=5) as client:
        client.sendall(data.encode("ascii"))
        response = bytearray()
        closed = False
        while True:
            try:
                chunk = client.recv(65536)
            except socket.timeout:
                break
            except ConnectionResetError:
                closed = True
                break
            if not chunk:
                closed = True
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
            "status": status, "headers": headers, "closed": closed,
            "body_hex": body.hex(), "decoded_body_hex": decoded.hex()}


def comparable(response):
    response = deepcopy(response)
    response["headers"] = sorted((name, value) for name, value in response["headers"]
                                if name not in ("date", "server", "content-length", "transfer-encoding"))
    body = response["decoded_body_hex"]
    if body and any(name == "content-type" and value.split(";", 1)[0].strip().lower() == "application/json"
           for name, value in response["headers"]):
        payload = json.loads(bytes.fromhex(body).decode("utf-8"))
        body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode().hex()
    return response["protocol"], response["status"], response["headers"], body, response["closed"]


def approved_go(case):
    if case["id"] not in APPROVED:
        return None
    digest, status, body = APPROVED[case["id"]]
    if h.digest(case["raw"].encode("ascii")) != digest:
        raise AssertionError("Owner transport classification does not cover changed request: " + case["id"])
    headers = [("content-type", "text/plain; charset=utf-8"), ("connection", "close")]
    return "HTTP/1.1", status, sorted(headers), body.encode().hex(), True


def characterize(binary, output):
    h.verify_manifest()
    h.verify_vectors()
    output.mkdir(parents=True, exist_ok=True)
    mismatches, failures = [], []
    with h.disposable_root() as root:
        sources = {label: h.export(label, root) for label in h.PINS}
        for retained in (False, True):
            shape, expected = "retained" if retained else "fresh", None
            for label in ("P0", "D0", "Go"):
                directory = root / ("framework-" + shape + "-" + label)
                database = h.fresh(directory, sources, retained)
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
                        if not response["closed"]:
                            failures.append({"shape": shape, "implementation": label, "case": case["id"], "error": "connection did not close"})
                        actual.append({"case": case, "request_sha256": h.digest(case["raw"].encode("ascii")),
                                       "response": response, "durable": after})
                (output / f"framework-{shape}-{label}.json").write_text(json.dumps(actual, indent=2), encoding="utf-8")
                if expected is None:
                    expected = actual
                else:
                    for first, second in zip(expected, actual, strict=True):
                        reference = comparable(first["response"])
                        accepted = approved_go(second["case"]) if label == "Go" else None
                        target = accepted if accepted is not None else reference
                        if comparable(second["response"]) != target:
                            failures.append({"shape": shape, "implementation": label, "case": second["case"]["id"],
                                             "expected": target, "actual": comparable(second["response"])})
                        if reference != comparable(second["response"]):
                            mismatches.append({"shape": shape, "implementation": label, "case": first["case"]["id"],
                                "P0_protocol": first["response"]["protocol"], "actual_protocol": second["response"]["protocol"],
                                "P0_headers": reference[2], "actual_headers": comparable(second["response"])[2],
                                "P0_status": first["response"]["status"], "actual_status": second["response"]["status"],
                                "P0_body": bytes.fromhex(first["response"]["decoded_body_hex"]).decode("latin-1"),
                                "actual_body": bytes.fromhex(second["response"]["decoded_body_hex"]).decode("latin-1")})
    result = {"pins": h.PINS, "request_count": len(cases()), "mismatches": mismatches,
              "failures": failures, "schema_shapes": ["fresh", "retained"],
              "effect": "All raw framing/read/refusal cases zero durable effects",
              "classification": "Explicit owner reply v12: eight exact malformed requests. Native HTTP1.0 replies now restore strict P0/D0 compatibility, including the separately permitted health deviation; original owner/raw evidence retained."}
    (output / "framework-characterization.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    if failures:
        raise AssertionError(json.dumps(failures, indent=2))
    print(f"Framework: {len(cases())} requests x two shapes x P0/D0/Go PASS; {len(mismatches)} exact owner-approved raw differences retained")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    characterize(args.binary.resolve(), args.output.resolve())


if __name__ == "__main__":
    main()

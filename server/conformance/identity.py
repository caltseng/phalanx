"""Current-source production entrypoint proxy/home parity; disposable schema26 only."""

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory

import harness as h
import release


ROOT = Path(__file__).resolve().parents[2]


def cases(trusted):
    browser = [("Host", "board.example.test"), ("Origin", "https://board.example.test"),
               ("Sec-Fetch-Site", "same-origin")]
    yield "https-settings", "PUT", "/v1/dispatch/settings", browser + [
        ("X-Forwarded-Proto", "https")], '{"paused":false}', h.OWNER, 200 if trusted else 403
    yield "https-redirect", "GET", "/v1/owner/status/", [
        ("Host", "board.example.test"), ("X-Forwarded-Proto", "https")], "", h.OWNER, 307
    for label, headers in (
        ("forged-peer", [("X-Forwarded-For", "127.0.0.1"), ("X-Forwarded-Proto", "https")]),
        ("last-proto", [("X-Forwarded-Proto", "http"), ("X-Forwarded-Proto", "https")]),
    ):
        yield label, "PUT", "/v1/dispatch/settings", browser + headers, \
            '{"paused":false}', h.OWNER, 200 if trusted else 403
    yield "direct-http", "PUT", "/v1/dispatch/settings", [
        ("Host", "board.example.test"), ("Origin", "http://board.example.test"),
        ("Sec-Fetch-Site", "same-origin")], '{"paused":true}', h.OWNER, 200
    for label, headers in (
        ("wrong-origin", browser + [("X-Forwarded-Proto", "http")]),
        ("wrong-site", browser[:2] + [("Sec-Fetch-Site", "cross-site"), ("X-Forwarded-Proto", "https")]),
        ("forwarded-only", browser + [("Forwarded", "proto=https")]),
        ("forwarded-host", browser[:1] + [("Origin", "https://other.example.test"),
                                         ("X-Forwarded-Host", "other.example.test"),
                                         ("X-Forwarded-Proto", "https")]),
        ("invalid-proto", browser + [("X-Forwarded-Proto", "https,http")]),
    ):
        yield label, "PUT", "/v1/dispatch/settings", headers, '{"paused":false}', h.OWNER, 403
    for token, label, expected in ((None, "anonymous", 401), (h.RUNNER, "runner", 403),
                                   (h.WRITER, "writer", 403)):
        yield label, "PUT", "/v1/dispatch/settings", browser + [
            ("X-Forwarded-Proto", "https")], '{"paused":false}', token, expected
    yield "local-direct", "PUT", "/operator/api/dispatch/settings", [
        ("Host", "localhost"), ("Origin", "http://localhost")], '{"paused":true}', None, 200
    for header in ("X-Forwarded-Proto", "X-Forwarded-For", "X-Real-IP", "Forwarded"):
        yield "local-refuse-" + header, "PUT", "/operator/api/dispatch/settings", [
            ("Host", "localhost"), ("Origin", "http://localhost"), (header, "https")], \
            '{"paused":false}', None, 401


def receipt_check(url, database, trusted, failures, observations):
    headers = [("Host", "board.example.test"), ("Origin", "https://board.example.test"),
               ("Sec-Fetch-Site", "same-origin"), ("X-Forwarded-Proto", "https"),
               ("Authorization", "Bearer " + h.OWNER), ("Content-Type", "application/json"),
               ("Idempotency-Key", "production-proxy-intake")]
    body = '{"repository_id":"fixture","goal":"Trusted production intake","target":"report"}'
    accepted = None
    for label, payload, expected in (("intake", body, 201 if trusted else 403),
                                     ("replay", body, 201 if trusted else 403),
                                     ("conflict", body.replace("Trusted", "Changed"), 409 if trusted else 403)):
        before = h.snapshot(database)
        response = h.wire(url, "POST", "/v1/items", headers, payload)
        after = h.snapshot(database)
        observations.append({"id": label, "request": {"method": "POST", "path": "/v1/items",
                                                      "headers": headers, "body": payload},
                             "response": response, "before": before, "after": after})
        if response["status"] != expected:
            failures.append(f"intake/{label}: {response['status']} != {expected}")
        if (not trusted or label != "intake") and before != after:
            failures.append(f"intake/{label}: replay/refusal changed durable state")
        if trusted and label == "intake" and response["status"] == 201:
            accepted = response["json"]
            if len(after["tables"]["request_receipts"]["rows"]) != len(
                    before["tables"]["request_receipts"]["rows"]) + 1:
                failures.append("intake: missing immutable receipt")
        if trusted and label == "replay" and response["json"] != accepted:
            failures.append("intake: replay changed accepted response")


def run(binary, output):
    output.mkdir(parents=True, exist_ok=True)
    shutil.copytree(ROOT / "phalanx" / "static", binary.parent / "assets", dirs_exist_ok=True)
    original = release.environment
    failures, records = [], []
    with TemporaryDirectory(prefix="phalanx-identity-") as temporary:
        root = Path(temporary)
        home = root / "muster"
        home.mkdir()
        (home / "child").mkdir()
        if os.name == "nt":
            import _winapi
            _winapi.CreateJunction(str(home / "child"), str(root / "link"))
        else:
            (root / "link").symlink_to(home / "child", target_is_directory=True)
        homes = {"unset": None, "relative": "relative-muster",
                 "absolute-dots": str(home) + os.sep + "child" + os.sep + "..",
                 "missing-tail": str(home / "absent") + os.sep + ".." + os.sep + "next",
                 "tilde": "~" + os.sep + "muster",
                 "resolved-link": str(root / "link") + os.sep + "missing",
                 "missing-before-link": str(root / "absent") + os.sep + ".." + os.sep + "link"}
        if os.name != "nt":
            homes["symlink-parent"] = str(root / "link") + "/.."
        configurations = [(name, value, None, True) for name, value in homes.items()]
        configurations += [("trust-" + name, None, value, trusted) for name, value, trusted in (
            ("empty", "", False), ("other", "192.0.2.1", False),
            ("network", "192.0.2.1, 127.0.0.0/8", True), ("all", "*", True))]
        for name, home_value, trust, trusted in configurations:
            compared = {}
            for runtime in ("Python", "Go"):
                directory = root / (name + "-" + runtime)
                database = h.fresh(directory, {"P0": ROOT})

                def environment(database):
                    env = original(database)
                    env.pop("FORWARDED_ALLOW_IPS", None)
                    env["USERPROFILE"] = env["HOME"] = str(root)
                    env["PHALANX_LOCAL_BOARD"] = "1"
                    if home_value is not None:
                        env["PHALANX_HOME"] = home_value
                    if trust is not None:
                        env["FORWARDED_ALLOW_IPS"] = trust
                    return env

                release.environment = environment
                observations = []
                try:
                    with release.production(binary, database, directory,
                                            python_source=ROOT if runtime == "Python" else None) as (url, _):
                        requests = [("identity", "GET", "/v1/muster/status", [], "", h.RUNNER, 200)]
                        if name == "unset" or name.startswith("trust-"):
                            requests.extend(cases(trusted))
                        for label, method, path, headers, body, token, expected in requests:
                            before = h.snapshot(database)
                            request_headers = [("Content-Type", "application/json"), *headers]
                            if token is not None:
                                request_headers.append(("Authorization", "Bearer " + token))
                            response = h.wire(url, method, path, request_headers, body)
                            after = h.snapshot(database)
                            record = {"configuration": name, "runtime": runtime, "id": label,
                                      "request": {"method": method, "path": path, "headers": request_headers,
                                                  "body": body},
                                      "response": response, "before": before, "after": after}
                            observations.append(record)
                            if response["status"] != expected:
                                failures.append(f"{name}/{runtime}/{label}: {response['status']} != {expected}")
                            if method == "GET" or expected >= 400:
                                if before != after:
                                    failures.append(f"{name}/{runtime}/{label}: unexpected durable effect")
                            elif response["status"] == 200:
                                with closing(sqlite3.connect(database)) as db:
                                    paused = db.execute("SELECT paused FROM dispatch_settings WHERE id=1").fetchone()[0]
                                if type(paused) is not int or paused != int(json.loads(body)["paused"]):
                                    failures.append(f"{name}/{runtime}/{label}: incorrect typed persisted setting")
                            projection = dict(response)
                            if label == "identity":
                                projection["json"] = dict(response["json"])
                                projection["json"].pop("observed_at")
                            compared.setdefault(label, []).append(h.comparable(projection))
                        if name == "unset" or name.startswith("trust-"):
                            receipt_check(url, database, trusted, failures, observations)
                        if name in {"unset", "relative", "absolute-dots", "tilde"}:
                            (home / "shift.json").write_text(json.dumps({
                                "base_url": url, "runtime": "any", "worktrees": str(home / "trees"),
                                "repositories": {"fixture": {"checkout": str(home / "checkout")}}}),
                                encoding="utf-8")
                            (home / "charter.toml").write_text("", encoding="utf-8")
                            (home / "private.env").write_text("PHALANX_RUNNER_TOKEN=" + h.RUNNER,
                                                            encoding="utf-8")
                            before = h.snapshot(database)
                            command = [sys.executable, "-P", "-c",
                                       "import sys; sys.path.insert(0,sys.argv.pop(1)); "
                                       "from phalanx.__main__ import main; main()",
                                       str(ROOT), "muster", "status", "--home", str(home)]
                            client = subprocess.run(command, env=environment(database), cwd=directory,
                                                    capture_output=True, text=True, timeout=15)
                            accepted = name in {"absolute-dots", "tilde"}
                            if (client.returncode == 0) != accepted or before != h.snapshot(database):
                                failures.append(f"{name}/{runtime}: unchanged muster client identity check failed")
                            observations.append({"id": "unchanged-muster-client", "exit": client.returncode,
                                                 "stdout": client.stdout, "stderr": client.stderr,
                                                 "before": before, "after": h.snapshot(database)})
                finally:
                    release.environment = original
                    (output / f"{name}-{runtime}.json").write_text(json.dumps(observations), encoding="utf-8")
                records.append({"configuration": name, "runtime": runtime, "count": len(observations)})
            for label, values in compared.items():
                if values[0] != values[1]:
                    failures.append(f"{name}/{label}: Python/Go wire mismatch")
        summary = {"source": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                   "binary_sha256": h.digest(binary.read_bytes()), "records": records, "failures": failures}
        (output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    if failures:
        raise AssertionError("\n".join(failures))
    print(f"Production proxy/home parity: {sum(row['count'] for row in records)} observations")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    run(args.binary.resolve(), args.output.resolve())

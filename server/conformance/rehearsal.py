"""Package-only rollback on evolved current data; no old snapshot is restored."""

import argparse
import json
from pathlib import Path
import socket
import sys
import time
from urllib.parse import urlsplit

import harness as h


STAMP = "2026-01-02T03:04:05.123456+00:00"
MARKDOWN = "# Package-switch report \u96ea\n\nExact current-data artifact.\n"


def receipt_rows(snapshot):
    table = snapshot["tables"]["request_receipts"]
    return {tuple(row[index]["value"] for index in (0, 1)): row for row in table["rows"]}


def call(url, method, path, body=None, token=h.OWNER, key=None):
    headers = [("Content-Type", "application/json"), ("Authorization", "Bearer " + token)]
    if key is not None:
        headers.append(("Idempotency-Key", key))
    return h.wire(url, method, path, headers, json.dumps(body) if body is not None else "")


def succeeded(response):
    if response["status"] >= 400:
        raise AssertionError(f"Rehearsal request failed: {response['status']} {response['text']}")
    return response["json"]


def live_binding(binding):
    if binding["writer"]["id"] != "fixture-writer" or not binding["writer"]["building"]:
        raise AssertionError("Continuation lost the same writer/Building allocation")
    return binding


def lost_response(url, path, body, key):
    target = urlsplit(url)
    data = json.dumps(body).encode()
    with socket.create_connection((target.hostname, target.port), timeout=10) as client:
        headers = (f"POST {path} HTTP/1.1\r\nHost: {target.netloc}\r\nContent-Type: application/json\r\n"
                   f"Authorization: Bearer {h.WRITER}\r\nIdempotency-Key: {key}\r\nContent-Length: {len(data)}\r\n\r\n").encode()
        client.sendall(headers + data)
        client.shutdown(socket.SHUT_WR)
        # Deliberately leave before the response; committed effects must reconcile.


def run(binary, output):
    h.verify_manifest()
    h.verify_vectors()
    output.mkdir(parents=True, exist_ok=True)
    with h.disposable_root() as root:
        sources = {label: h.export(label, root) for label in h.PINS}
        for reference in ("P0", "D0"):
            for retained in (False, True):
                directory = root / f"switch-{reference}-{'retained' if retained else 'fresh'}"
                database = h.fresh(directory, sources, retained)
                initial = h.snapshot(database)
                immutable = receipt_rows(initial)
                records, binding, draft_body, draft_receipt, review_body, review_receipt = [], None, None, None, None, None
                pass_id, page_body = None, None
                source_issue = {"number": 101, "author_login": "synthetic-other", "author_id": 9002,
                    "state": "open", "title": "Scouted other work", "body": "Retain explicit approval",
                    "updated_at": "2026-01-02T03:04:05"}
                usage = {"runtime": "synthetic-runtime", "provider": "synthetic-provider", "account": "synthetic-account",
                    "origin": "synthetic-origin", "session_id": "synthetic-switch-session", "source": "synthetic-source",
                    "source_version": "v1", "epoch": "synthetic-epoch", "sequence": 1, "window_start": STAMP,
                    "cutoff": STAMP, "observed_at": STAMP, "scope": "item_exclusive", "status": "pending_final",
                    "reason": "Synthetic coverage", "tokens": {"input": 10, "output": 0}}
                result_path = f"/v1/items/{h.SEED}/executions/{h.SEED_EXECUTION}/result"

                def observe(name, response=None):
                    nonlocal immutable
                    snapshot = h.snapshot(database)
                    if (snapshot["ddl"] != initial["ddl"] or snapshot["application_id"] != initial["application_id"]
                            or snapshot["version"] != 26 or snapshot["journal"] != "delete"):
                        raise AssertionError("Package switch changed schema/identity/journal")
                    current = receipt_rows(snapshot)
                    for key, row in immutable.items():
                        if current.get(key) != row:
                            raise AssertionError("Package switch rewrote immutable receipt: " + str(key))
                    immutable = current
                    records.append({"name": name, "response": response, "durable": snapshot})

                for phase, label in enumerate((reference, "Go", reference, "Go", reference, "Go", reference, "Go"), 1):
                    command = [str(binary), "--fixture", str(database), "--id-start", str(phase * 0x100000)] if label == "Go" else [
                        sys.executable, "-P", str(h.HERE / "oracle.py"), "serve", str(sources[label]), str(database),
                        "--id-start", str(phase * 0x100000), "--captain-clock", "--scout-clock", "--operator-clock", "--flags-clock"]
                    with h.server(command, directory) as url:
                        if phase == 1:
                            succeeded(call(url, "PUT", "/v1/dispatch/settings", {"paused": False}))
                            succeeded(call(url, "PUT", "/v1/captains/fixture-captain", {"charter_revision": "synthetic-charter",
                                "wake_count": 1, "state": "active", "pickup": {"repositories": {"fixture": None}}}))
                            succeeded(call(url, "PATCH", f"/v1/items/{h.SEED}/executions/{h.SEED_EXECUTION}/writer",
                                {"captain_id": "fixture-captain", "writer_id": "fixture-writer", "session_id": "synthetic-switch-session"}))
                            binding = live_binding(succeeded(call(url, "POST", "/v1/writer/attempt", token=h.WRITER)))
                            succeeded(call(url, "PUT", f"/v1/items/{h.SEED}/title", {"expected_revision": 0, "title": "Switch title"}, key="switch-title"))
                            succeeded(call(url, "PUT", "/v1/flags", {"kind": "switch_note", "item_id": h.SEED,
                                "execution_id": h.SEED_EXECUTION, "reason": "Retained cause", "recovery": "Continue same writer"}))
                            succeeded(call(url, "POST", "/v1/writer/usage", usage, token=h.WRITER))
                            observe("reference-retained-live-binding", binding)
                        elif phase == 2:
                            draft_body = {"expected_version": binding["version"], "attempt_id": binding["writer"]["attempt_id"],
                                "stage": binding["state"]["stage"], "outcome": "succeeded", "report_markdown": MARKDOWN}
                            lost_response(url, result_path, draft_body, "switch-draft")
                            until = time.monotonic() + 10
                            while time.monotonic() < until:
                                binding = live_binding(succeeded(call(url, "GET", "/v1/writer", token=h.WRITER)))
                                if binding["state"]["stage"] == "report_review":
                                    break
                                time.sleep(.05)
                            else:
                                raise AssertionError("Caller loss did not leave a reconcilable committed draft")
                            draft_receipt = call(url, "POST", result_path, draft_body, token=h.WRITER, key="switch-draft")
                            succeeded(draft_receipt)
                            observe("Go-committed-after-caller-loss", draft_receipt)
                        elif phase == 3:
                            replay = call(url, "POST", result_path, draft_body, token=h.WRITER, key="switch-draft")
                            if h.comparable(replay) != h.comparable(draft_receipt):
                                raise AssertionError("Previous package could not replay Go-created stage receipt")
                            binding = live_binding(succeeded(call(url, "GET", "/v1/writer", token=h.WRITER)))
                            question = {"expected_version": binding["version"], "attempt_id": binding["writer"]["attempt_id"],
                                "stage": binding["state"]["stage"], "outcome": "needs_input",
                                "question": {"prompt": "Synthetic direction", "options": ["Continue"], "handoff": "Retain binding"}}
                            binding = live_binding(succeeded(call(url, "POST", result_path, question, token=h.WRITER, key="switch-question")))
                            observe("reference-rollback-live-question", binding)
                        elif phase == 4:
                            succeeded(call(url, "POST", f"/v1/items/{h.SEED}/executions/{h.SEED_EXECUTION}/reply",
                                {"expected_version": binding["version"], "answer": "Continue"}, key="switch-reply"))
                            binding = live_binding(succeeded(call(url, "POST", "/v1/writer/attempt", token=h.WRITER)))
                            review_body = {"expected_version": binding["version"], "attempt_id": binding["writer"]["attempt_id"],
                                "stage": binding["state"]["stage"], "outcome": "succeeded", "report_revision": h.digest(MARKDOWN.encode())}
                            review_receipt = call(url, "POST", result_path, review_body, token=h.WRITER, key="switch-review")
                            binding = live_binding(succeeded(review_receipt))
                            succeeded(call(url, "POST", "/v1/writer/usage", {**usage, "sequence": 2,
                                "status": "unavailable", "tokens": {}, "reason": "Synthetic acquisition gap"}, token=h.WRITER))
                            observe("Go-same-writer-through-reply-review-and-gap", binding)
                        elif phase == 5:
                            replay = call(url, "POST", result_path, review_body, token=h.WRITER, key="switch-review")
                            if h.comparable(replay) != h.comparable(review_receipt):
                                raise AssertionError("Previous package altered immutable Go review receipt")
                            binding = live_binding(succeeded(call(url, "GET", "/v1/writer", token=h.WRITER)))
                            final = call(url, "POST", result_path, {"expected_version": binding["version"],
                                "attempt_id": binding["writer"]["attempt_id"], "stage": binding["state"]["stage"],
                                "outcome": "succeeded", "report_revision": h.digest(MARKDOWN.encode())}, token=h.WRITER, key="switch-store")
                            succeeded(final)
                            if call(url, "GET", "/v1/writer", token=h.WRITER)["status"] != 401:
                                raise AssertionError("Finalization revived/retained ended writer credential")
                            observe("reference-finalizes-Go-reviewed-artifact", final)
                        elif phase == 6:
                            report = call(url, "GET", f"/v1/items/{h.SEED}/executions/{h.SEED_EXECUTION}/report")
                            if report["text"] != MARKDOWN:
                                raise AssertionError("Current-data artifact bytes changed across restart")
                            succeeded(call(url, "PUT", "/v1/issue-scout/settings", {"expected_revision": 0, "enabled": True,
                                "author_login": "synthetic-author", "author_id": 9001, "repositories": ["fixture"]}))
                            claim = succeeded(call(url, "POST", "/v1/issue-scout/claim", {"captain_id": "fixture-captain"}))["claim"]
                            pass_id = claim["id"]
                            page_body = {"claim_id": claim["claim_id"], "repository_id": "fixture", "page": 1,
                                "verified_login": "synthetic-author", "verified_id": 9001,
                                "issues": [{**source_issue, "number": 100, "author_login": "synthetic-author", "author_id": 9001}, source_issue],
                                "has_more": False}
                            succeeded(call(url, "POST", "/v1/issue-scout/passes/" + pass_id + "/page", page_body))
                            observe("Go-scout-source-intake-and-page-receipts")
                        elif phase == 7:
                            before = h.snapshot(database)
                            succeeded(call(url, "POST", "/v1/issue-scout/passes/" + pass_id + "/page", page_body))
                            if before != h.snapshot(database):
                                raise AssertionError("Previous package replayed scout intake effects")
                            fingerprint = h.digest(json.dumps({key: value for key, value in source_issue.items() if key != "updated_at"},
                                sort_keys=True, separators=(",", ":")).encode())
                            succeeded(call(url, "POST", "/v1/issue-scout/issues/fixture/101/decision",
                                {"expected_fingerprint": fingerprint, "decision": "approved"}))
                            succeeded(call(url, "POST", f"/v1/items/{h.SEED}/executions/{h.SEED_EXECUTION}/writers/fixture-writer/usage",
                                {"captain_id": "fixture-captain", "snapshot": {**usage, "sequence": 3,
                                    "status": "observed", "tokens": {"input": 20, "output": 0}}}))
                            observe("reference-rollback-scout-replay-approval-and-late-usage")
                        else:
                            claim = succeeded(call(url, "POST", "/v1/issue-scout/claim", {"captain_id": "fixture-captain"}))["claim"]
                            succeeded(call(url, "POST", "/v1/issue-scout/passes/" + claim["id"] + "/page",
                                {**page_body, "claim_id": claim["claim_id"], "issues": [source_issue]}))
                            for path in ("/v1/operator/overview", "/v1/operator/executions",
                                         "/v1/operator/executions/" + h.SEED_EXECUTION + "/timeline"):
                                before = h.snapshot(database)
                                succeeded(call(url, "GET", path))
                                if before != h.snapshot(database):
                                    raise AssertionError("Final current-data projection changed storage")
                            observe("Go-resumes-approved-source-on-evolved-data")
                (output / (directory.name + ".json")).write_text(json.dumps(records, ensure_ascii=True), encoding="utf-8")
                print(f"{reference}/{'retained' if retained else 'fresh'}: eight package-only stages, immutable receipts/current data/caller-loss/same-writer/scout/usage/report PASS")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run(args.binary.resolve(), args.output.resolve())


if __name__ == "__main__":
    main()

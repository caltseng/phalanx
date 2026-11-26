"""Exact Captain lifecycle/snapshot proofs with explicit common clock and ID injection."""

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys

import harness as h


def cases():
    result = []
    id = f"{1:032x}"
    base = "/v1/captains/" + id
    registration = {"session_ref": "synthetic-new-captain", "charter_revision": "charter-1",
                    "pickup": {"repositories": {"fixture": None}}}

    def add(name, method="GET", path="/v1/captains", body=None, token=h.OWNER, key=None):
        fields = [] if token is None else [("Authorization", "Bearer " + token)]
        fields.append(("Content-Type", "application/json"))
        if key is not None:
            fields.append(("Idempotency-Key", key))
        result.append({"id": name, "method": method, "path": path, "headers": fields,
                       "body": json.dumps(body) if body is not None else ""})

    add("captains-initial")
    add("captain-initial-current", path="/v1/captains/current")
    add("captain-register", "POST", body=registration, key="captain")
    add("captain-register-replay", "POST", body=registration, key="captain")
    add("captain-register-conflicting-key", "POST", body={**registration, "charter_revision": "changed"}, key="captain")
    add("captain-current-ambiguous", path="/v1/captains/current")
    add("captain-detail", path=base)
    heartbeat = {"charter_revision": "charter-1", "wake_count": 1, "state": "active",
                 "compaction_count": 3, "sessions": [
                     {"id": "synthetic-visible", "name": "Native session", "writer_nonce": "visible-nonce",
                      "availability": "present", "runtime": "synthetic-runtime"}]}
    add("captain-heartbeat", "PUT", base, heartbeat, token=h.RUNNER)
    add("captain-compaction-monotone", "PUT", base, {**heartbeat, "wake_count": 0, "compaction_count": 1})
    add("captain-charter-resets-pickup", "PUT", base, {**heartbeat, "charter_revision": "charter-2"})
    add("captain-pickup-restored", "PUT", base, {**heartbeat, "charter_revision": "charter-2",
                                             "pickup": {"repositories": {"fixture": None}}})
    add("captain-reinforcement-future", "PUT", base + "/reinforcement", {"interval": 1})
    add("captain-reinforcement-invalid", "PUT", base + "/reinforcement", {"interval": 0})
    action = {"captain_id": id, "action": "held", "reason": "Preserve the native binding"}
    add("captain-action", "POST", f"/v1/items/{h.SEED}/captain-actions", action, key="action")
    add("captain-action-replay", "POST", f"/v1/items/{h.SEED}/captain-actions", action, key="action")
    add("captain-ending", "PUT", base, {**heartbeat, "charter_revision": "charter-2", "state": "ending"})
    add("captain-ending-current", path="/v1/captains/current")
    add("captain-backwards-refused", "PUT", base, heartbeat)
    add("captain-ended", "PUT", base, {**heartbeat, "charter_revision": "charter-2", "state": "ended"})
    add("captain-ended-detail", path=base)
    add("captain-ended-action-refused", "POST", f"/v1/items/{h.SEED}/captain-actions",
        {**action, "reason": "Cannot act"}, key="new-action")
    add("captain-ended-action-replay", "POST", f"/v1/items/{h.SEED}/captain-actions", action, key="action")
    add("captain-successor", "POST", body={**registration, "session_ref": "successor-session", "parent_id": id})
    add("captain-ended-successor-detail", path=base)
    add("captain-limit-one", path="/v1/captains?limit=1")
    add("captain-limit-invalid", path="/v1/captains?limit=0")
    add("captain-pickup-key-invalid", "POST", body={**registration, "pickup": {"repositories": {"!": None}}})
    add("captain-session-invalid", "PUT", base, {**heartbeat, "sessions": [{"id": None, "name": "x"}]})
    add("captain-session-error-order", "PUT", base, {**heartbeat, "sessions": [
        {"id": None, "name": None, "writer_nonce": 1, "availability": "invalid", "runtime": 1}]})
    add("captain-compaction-upper", "PUT", base, {**heartbeat, "compaction_count": 9007199254740992})
    for token in (None, h.WRITER):
        add("captain-read-role-" + str(token), token=token)
        add("captain-write-role-" + str(token), "POST", body=registration, token=token)
    add("captain-trailing-slash", path=base + "///")
    add("captain-invalid-method", "DELETE", base)
    for name, previous in (
            ("null-first-seen", [{"id": "synthetic-visible", "first_seen": None}]),
            ("missing-first-seen", [{"id": "synthetic-visible"}]),
            ("missing-id", [{"first_seen": None}]),
            ("null-id", [{"id": None, "first_seen": None}]),
            ("duplicate-id", [{"id": "synthetic-visible", "first_seen": "first"},
                              {"id": "synthetic-visible", "first_seen": "last"}])):
        add("captain-stored-" + name, "PUT", base, {**heartbeat, "state": "ended"})
        result[-1]["stored_sessions"] = previous
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
                directory = root / f"captains-{shape}-{label}"
                database = h.fresh(directory, sources, retained)
                command = [str(binary), "--fixture", str(database)] if label == "Go" else [
                    sys.executable, "-P", str(h.HERE / "oracle.py"), "serve", str(sources[label]), str(database),
                    "--captain-clock"]
                actual, tokens = [], {}
                with h.server(command, directory) as url:
                    for case in cases():
                        if "stored_sessions" in case:
                            with closing(sqlite3.connect(database)) as db:
                                db.execute("UPDATE captains SET sessions_json=? WHERE id=?",
                                           (json.dumps(case["stored_sessions"]), f"{1:032x}"))
                                db.commit()
                        before = h.snapshot(database)
                        response = h.wire(url, case["method"], case["path"], case["headers"], case["body"])
                        after = h.snapshot(database)
                        if response["status"] >= 400 and before != after:
                            raise AssertionError("Captain refusal changed durable data: " + case["id"])
                        token = after["tables"]["operator_change"]["rows"][0][0]["value"]
                        tokens.setdefault(token, len(tokens))
                        actual.append({"request": case, "response": response, "durable": after,
                                       "change_token_ordinal": tokens[token]})
                (output / f"captains-{shape}-{label}.json").write_text(
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
            print(f"{shape}: {len(expected)} Captain lifecycle/snapshot vectors x P0,D0,Go PASS")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    differential(args.binary.resolve(), args.output.resolve())


if __name__ == "__main__":
    main()

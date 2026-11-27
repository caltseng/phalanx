"""Synthetic display-only usage writes preserve attribution, counters and lifecycle."""

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys

import harness as h


START = "2026-01-02T03:04:05.123456Z"
LATER = "2026-01-03T03:04:05.123456Z"


def snapshot(**edits):
    return {"runtime": "synthetic-runtime", "provider": "synthetic-provider",
            "account": "synthetic-account", "origin": "synthetic-metadata",
            "session_id": "synthetic-writer-session", "source": "synthetic-source",
            "source_version": "v1", "epoch": "synthetic-epoch", "model": None,
            "sequence": 1, "window_start": START, "cutoff": START, "observed_at": START,
            "scope": "item_exclusive", "status": "pending_final", "reason": "Synthetic source coverage",
            "tokens": {"input": 10, "output": 0}, **edits}


def cases():
    result = []
    base = f"/v1/items/{h.SEED}/executions/{h.SEED_EXECUTION}/writers/fixture-writer/usage"

    def add(name, body=None, path="/v1/writer/usage", token=h.WRITER, **fixture):
        headers = [("Content-Type", "application/json")]
        if token is not None:
            headers.append(("Authorization", "Bearer " + token))
        result.append({"id": name, "method": "POST", "path": path, "headers": headers,
                       "body": json.dumps(body) if body is not None else "", **fixture})

    add("usage-missing-body")
    add("usage-missing-fields", {})
    add("usage-unconfirmed", snapshot())
    add("usage", snapshot(), confirm=True)
    add("usage-identical", snapshot())
    add("usage-stale", snapshot(sequence=0))
    add("usage-conflicting", snapshot(tokens={"input": 11}))
    add("usage-counter-decreased", snapshot(sequence=2, tokens={"input": 9, "output": 0}))
    add("usage-counter-disappeared", snapshot(sequence=2, tokens={"input": 11}))
    add("usage-unavailable-retains", snapshot(sequence=2, tokens={}, status="unavailable", reason="Synthetic acquisition gap"))
    add("usage-unavailable-replay", snapshot(sequence=2, tokens={}, status="unavailable", reason="Synthetic acquisition gap"))
    add("usage-restored", snapshot(sequence=3, tokens={"input": 20, "output": 0}))
    add("usage-version-changed-allowed", snapshot(sequence=4, source_version="v2", tokens={"input": 20, "output": 0}))
    for field, value in (("runtime", "other-runtime"), ("session_id", "other-session"),
                         ("provider", "other-provider"), ("account", "other-account"),
                         ("origin", "other-origin"), ("source", "other-source"),
                         ("epoch", "other-epoch"), ("model", "other-model"),
                         ("scope", "session_lifetime"), ("window_start", "2026-01-02T03:04:05.123455Z")):
        add("usage-series-" + field, snapshot(sequence=5, **{field: value}))
    add("usage-observation-order", snapshot(sequence=5, window_start="2026-01-01T00:00:00Z",
                                           cutoff="2026-01-01T00:00:00Z", observed_at="2026-01-01T00:00:00Z",
                                           scope="session_lifetime"))
    for index, tokens in enumerate(({"input": True}, {"input": -1}, {"input": 1.5},
                                    {"input": 9007199254740992}, {"extra": 2}, None, [])):
        add("usage-invalid-tokens-" + str(index), snapshot(sequence=5, tokens=tokens))
    add("usage-unavailable-not-zero", snapshot(sequence=5, tokens={"input": 0}, status="unavailable"))
    add("usage-unmeasured-not-observed", snapshot(sequence=5, tokens={}))
    add("usage-window-reversed", snapshot(sequence=5, cutoff="2026-01-01T00:00:00Z"))
    add("usage-future", snapshot(sequence=5, observed_at="9999-01-01T00:00:00Z"))
    add("usage-extra-fields", {**snapshot(), "prompt": "Synthetic forbidden extra"})
    add("usage-nested-errors", snapshot(sequence=True, window_start=None, cutoff="bad", observed_at=True,
        tokens={"input": True, "output": -1, "extra": None},
        credits={"amount": "01", "unit": "", "basis": "invalid", "source": None, "extra": 1}))
    for token in (None, h.OWNER, h.RUNNER):
        add("usage-own-role-" + str(token), snapshot(), token=token)
    for token in (None, h.WRITER):
        add("usage-captain-role-" + str(token), {"captain_id": "fixture-captain", "snapshot": snapshot()},
            path=base, token=token)
    add("usage-captain-empty-scope", {"captain_id": "fixture-captain", "snapshot": snapshot(sequence=5)}, path=base, token=h.RUNNER)
    add("usage-captain-scope", {"captain_id": "fixture-captain", "snapshot": snapshot(sequence=5, tokens={"input": 20, "output": 0})},
        path=base, token=h.RUNNER, scope=True)
    add("usage-captain-wrong-writer", {"captain_id": "fixture-captain", "snapshot": snapshot()},
        path=base.replace("fixture-writer", "other"), token=h.RUNNER)
    add("usage-captain-wrong-item", {"captain_id": "fixture-captain", "snapshot": snapshot()},
        path=base.replace(h.SEED, "other"), token=h.RUNNER)
    add("usage-initial-unavailable", snapshot(tokens={}, status="unavailable"), clear_usage=True)
    add("usage-initial-unavailable-replay", snapshot(tokens={}, status="unavailable"))
    quantity = {"amount": "0.01", "unit": "synthetic-credit", "basis": "provider_reported", "source": "synthetic-provider"}
    add("usage-provider-quantities", snapshot(credits=quantity), clear_usage=True)
    add("usage-provider-quantities-increase", snapshot(sequence=2, credits={**quantity, "amount": "0.010000001"}))
    add("usage-provider-quantities-decrease", snapshot(sequence=3, credits=quantity))
    add("usage-provider-quantities-units", snapshot(sequence=3, credits={**quantity, "amount": "1", "unit": "different"}))
    add("usage-provider-quantities-disappear", snapshot(sequence=3))
    for index, amount in enumerate(("00", "-1", "1e2", "0.0000000001", "1000000000000000", "", 0, None)):
        add("usage-amount-invalid-" + str(index), snapshot(sequence=3, credits={**quantity, "amount": amount}))
    add("usage-session-lifetime", snapshot(scope="session_lifetime", window_start="2026-01-01T00:00:00Z"), clear_usage=True)
    add("usage-cutoff-backwards", snapshot(sequence=2, scope="session_lifetime", window_start="2026-01-01T00:00:00Z",
                                         cutoff="2026-01-02T00:00:00Z"))
    add("usage-full-zero", snapshot(tokens={name: 0 for name in ("input", "output", "cache_read", "cache_write", "reasoning", "total")}),
        clear_usage=True)
    add("usage-max-counter", snapshot(sequence=2, tokens={name: 9007199254740991 for name in
                                                       ("input", "output", "cache_read", "cache_write", "reasoning", "total")}))
    add("usage-late-own-token-ended", snapshot(sequence=3, observed_at=LATER), ended=True)
    add("usage-late-captain", {"captain_id": "fixture-captain", "snapshot": snapshot(sequence=3, observed_at=LATER,
        tokens={name: 9007199254740991 for name in ("input", "output", "cache_read", "cache_write", "reasoning", "total")})},
        path=base, token=h.RUNNER)
    add("usage-late-cutoff-outside", {"captain_id": "fixture-captain", "snapshot": snapshot(sequence=4, observed_at=LATER, cutoff=LATER)},
        path=base, token=h.RUNNER)
    add("usage-captain-ended", {"captain_id": "fixture-captain", "snapshot": snapshot(sequence=4)}, path=base,
        token=h.RUNNER, captain_ended=True)
    add("usage-invalid-path", {"captain_id": "!", "snapshot": {}},
        path="/v1/items/!/executions/!/writers/!/usage", token=h.RUNNER)
    add("usage-trailing-slash", snapshot(), path="/v1/writer/usage///", token=h.OWNER)
    return result


def fixture(database, case):
    with closing(sqlite3.connect(database)) as db:
        if case.get("confirm"):
            db.execute("UPDATE writers SET confirmed_at=created_at,session_id='synthetic-writer-session' WHERE id='fixture-writer'")
        if case.get("scope"):
            db.execute("UPDATE captains SET pickup_json=? WHERE id='fixture-captain'",
                       (json.dumps({"repositories": {"fixture": None}}),))
        if case.get("clear_usage"):
            db.execute("DELETE FROM writer_usage")
        if case.get("ended"):
            db.execute("UPDATE writers SET ended_at=created_at WHERE id='fixture-writer'")
        if case.get("captain_ended"):
            db.execute("UPDATE captains SET state='ended' WHERE id='fixture-captain'")
        db.commit()


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
                directory = root / f"usage-{shape}-{label}"
                database = h.fresh(directory, sources, retained)
                command = [str(binary), "--fixture", str(database), "--clock-offset=24h"] if label == "Go" else [
                    sys.executable, "-P", str(h.HERE / "oracle.py"), "serve", str(sources[label]), str(database)]
                actual, tokens = [], {}
                with h.server(command, directory) as url:
                    for case in cases():
                        fixture(database, case)
                        before = h.snapshot(database)
                        response = h.wire(url, case["method"], case["path"], case["headers"], case["body"])
                        after = h.snapshot(database)
                        if response["status"] >= 400 and before != after:
                            raise AssertionError("Usage refusal changed durable data: " + case["id"])
                        for table in before["tables"].keys() - {"writer_usage", "operator_change"}:
                            if before["tables"][table] != after["tables"][table]:
                                raise AssertionError(f"Usage changed {table}: {case['id']}")
                        token = after["tables"]["operator_change"]["rows"][0][0]["value"]
                        tokens.setdefault(token, len(tokens))
                        actual.append({"request": case, "response": response, "durable": after,
                                       "change_token_ordinal": tokens[token]})
                (output / f"usage-{shape}-{label}.json").write_text(json.dumps(actual, ensure_ascii=True), encoding="utf-8")
                if expected is None:
                    expected = actual
                    continue
                for first, second in zip(expected, actual, strict=True):
                    if h.comparable(first["response"]) != h.comparable(second["response"]):
                        raise AssertionError(f"{shape}/{label}/{first['request']['id']}: wire mismatch\n"
                                             f"{h.comparable(first['response'])!r}\n{h.comparable(second['response'])!r}")
                    if h.comparable_durable(first) != h.comparable_durable(second):
                        raise AssertionError(f"{shape}/{label}/{first['request']['id']}: durable mismatch")
            print(f"{shape}: {len(expected)} usage/attribution/no-lifecycle vectors x P0,D0,Go PASS")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    differential(args.binary.resolve(), args.output.resolve())


if __name__ == "__main__":
    main()

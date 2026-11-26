"""Operator query/summary/cursor/validator/timeline and usage projections on loopback."""

import argparse
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import sys

import harness as h


STAMP = "2026-01-02T03:04:05.123456+00:00"
ITEM2, EXEC2, ITEM3, EXEC3 = (f"{value:032x}" for value in (0x1100, 0x1101, 0x1102, 0x1103))


def cases():
    result = []

    def add(name, path="/v1/operator/executions", method="GET", body=None, token=h.OWNER, headers=(), **extra):
        fields = [("Content-Type", "application/json")]
        if token is not None:
            fields.append(("Authorization", "Bearer " + token))
        fields.extend(headers)
        result.append({"id": name, "path": path, "method": method, "headers": fields,
                       "body": body, **extra})

    add("operator-open", save_tag="OPEN_TAG")
    add("operator-open-304", conditional="OPEN_TAG")
    add("operator-open-strong-304", conditional="OPEN_TAG", strong=True)
    add("operator-open-star-304", headers=[("If-None-Match", "*")])
    add("operator-open-page-one", path="/v1/operator/executions?limit=1", save_cursor="OPEN_CURSOR")
    add("operator-open-page-next", path="/v1/operator/executions?limit=1&cursor=$OPEN_CURSOR")
    add("operator-open-cursor-filter-mismatch", path="/v1/operator/executions?repository=fixture&cursor=$OPEN_CURSOR")
    for name, query in (
            ("kind", "kind=other"), ("limit", "limit=0"), ("limit-null", "limit="), ("cursor", "cursor=invalid"),
            ("repository", "repository=!"), ("label", "label=!"), ("status", "status=other"), ("stage", "stage=other"),
            ("outcome", "outcome=other"), ("q-empty", "q="), ("date", "closed_after=invalid"),
            ("closed-on-open", "outcome=failed"), ("status-kind", "kind=closed&status=queued"),
            ("duplicate-last", "limit=0&limit=1"), ("unicode-search", "q=%E9%9B%AA"),
            ("no-match", "q=not-a-match"), ("exact-id", "q=" + h.SEED), ("number", "q=%237"),
            ("closed-after-open", "closed_after=2026-01-01")):
        add("operator-query-" + name, "/v1/operator/executions?" + query)
    add("operator-overview", "/v1/operator/overview", save_tag="OVERVIEW_TAG")
    add("operator-overview-304", "/v1/operator/overview", conditional="OVERVIEW_TAG")
    add("operator-current-item", "/v1/operator/items/" + h.SEED, save_tag="ITEM_TAG")
    add("operator-current-item-304", "/v1/operator/items/" + h.SEED, conditional="ITEM_TAG")
    add("operator-item-missing", "/v1/operator/items/missing")
    add("operator-item-path-invalid", "/v1/operator/items/!")
    add("operator-current-item-absent-publication-mode", "/v1/operator/items/" + h.SEED,
        missing_mode=True, semantic_case="operator-current-item/absent-publication-mode")
    add("operator-current-item-explicit-ready", "/v1/operator/items/" + h.SEED, explicit_mode="ready")
    add("operator-current-item-explicit-draft", "/v1/operator/items/" + h.SEED, explicit_mode="draft")
    add("operator-current-item-mode-restored", "/v1/operator/items/" + h.SEED, explicit_mode="ready")
    add("operator-group-missing", "/v1/operator/groups/campaign")
    add("operator-group-labelled", "/v1/operator/groups/campaign?limit=1", labels=True, save_cursor="GROUP_CURSOR")
    add("operator-group-next", "/v1/operator/groups/campaign?limit=1&cursor=$GROUP_CURSOR")
    add("operator-group-path-query-errors", "/v1/operator/groups/!?limit=0")
    add("operator-group-cursor-scope", "/v1/operator/groups/campaign?cursor=$OPEN_CURSOR")
    add("operator-title-independent", f"/v1/items/{h.SEED}/title", "PUT",
        {"expected_revision": 0, "title": "Independent display title"}, headers=[("Idempotency-Key", "operator-title")])
    add("operator-title-visible", "/v1/operator/items/" + h.SEED)
    add("operator-filter-title", "/v1/operator/executions?q=Independent")
    add("operator-receipt-goal-preserved", "/v1/operator/items/" + ITEM2)
    add("operator-closed-fixture", "/v1/operator/executions?kind=closed&limit=1", closures=True, save_cursor="CLOSED_CURSOR")
    add("operator-closed-next", "/v1/operator/executions?kind=closed&limit=1&cursor=$CLOSED_CURSOR")
    add("operator-closed-filter", "/v1/operator/executions?kind=closed&outcome=checked_in")
    add("operator-closed-date", "/v1/operator/executions?kind=closed&closed_after=2026-01-01")
    add("operator-closed-scope-mismatch", "/v1/operator/executions?kind=closed&cursor=$OPEN_CURSOR")
    add("operator-group-rollup", "/v1/operator/groups/campaign")
    add("operator-overview-closures", "/v1/operator/overview")
    timeline = "/v1/operator/executions/" + h.SEED_EXECUTION + "/timeline"
    add("operator-timeline-initial", timeline)
    add("operator-timeline-missing", "/v1/operator/executions/missing/timeline")
    add("operator-timeline-path-query-errors", "/v1/operator/executions/!/timeline?limit=0")
    add("operator-timeline-cursor-scope", timeline + "?cursor=$OPEN_CURSOR")
    add("operator-writer-claim", "/v1/writer/attempt", "POST", token=h.WRITER, save_binding=True)
    add("operator-timeline-running", timeline)
    result_path = f"/v1/items/{h.SEED}/executions/{h.SEED_EXECUTION}/result"
    add("operator-writer-draft", result_path, "POST", {"expected_version": "$VERSION", "attempt_id": "$ATTEMPT",
        "stage": "$STAGE", "outcome": "succeeded", "report_markdown": "# Synthetic report \u96ea\n"},
        token=h.WRITER, save_binding=True)
    add("operator-writer-question", result_path, "POST", {"expected_version": "$VERSION", "attempt_id": "$ATTEMPT",
        "stage": "$STAGE", "outcome": "needs_input",
        "question": {"prompt": "Synthetic operator question", "options": ["Continue"], "handoff": "Retain report"}},
        token=h.WRITER, save_binding=True)
    add("operator-timeline-question", timeline + "?limit=1", save_cursor="TIMELINE_CURSOR")
    add("operator-timeline-next", timeline + "?limit=1&cursor=$TIMELINE_CURSOR")
    add("operator-owner-reply", f"/v1/items/{h.SEED}/executions/{h.SEED_EXECUTION}/reply", "POST",
        {"expected_version": "$VERSION", "answer": "Continue"}, headers=[("Idempotency-Key", "operator-owner-reply")])
    add("operator-timeline-reply", timeline)
    add("operator-timeline-redacted-cause", timeline, result_cause=True)
    add("operator-muster", "/v1/muster/status", home=True)
    add("operator-muster-runner", "/v1/muster/status", token=h.RUNNER, home=True)
    add("operator-usage-observation", "/v1/writer/usage", "POST", {
        "runtime": "synthetic-runtime", "provider": "synthetic-provider", "account": "synthetic-account",
        "origin": "synthetic-origin", "session_id": "synthetic-session-id", "source": "synthetic-source",
        "source_version": "v1", "epoch": "synthetic-epoch", "sequence": 1, "window_start": STAMP,
        "cutoff": STAMP, "observed_at": STAMP, "scope": "item_exclusive", "status": "observed",
        "reason": "Synthetic coverage", "tokens": {"input": 10, "output": 0}}, token=h.WRITER, confirm=True)
    add("operator-timeline-usage-projection", timeline)
    for token in (None, h.WRITER):
        add("operator-list-role-" + str(token), token=token)
        add("operator-timeline-role-" + str(token), timeline, token=token)
        add("operator-muster-role-" + str(token), "/v1/muster/status", token=token)
    add("operator-item-own-writer", "/v1/operator/items/" + h.SEED, token=h.WRITER)
    add("operator-item-other-writer", "/v1/operator/items/" + ITEM2, token=h.WRITER)
    add("operator-invalid-method", method="POST")
    add("operator-trailing-slashes", path="/v1/operator/overview///")
    return result


def resolve(value, variables):
    if isinstance(value, str):
        if value in variables:
            return variables[value]
        for name in sorted(variables, key=len, reverse=True):
            value = value.replace(name, str(variables[name]))
        return value
    if isinstance(value, dict):
        return {name: resolve(entry, variables) for name, entry in value.items()}
    if isinstance(value, list):
        return [resolve(entry, variables) for entry in value]
    return value


def fixture(database, case):
    changed = False
    with closing(sqlite3.connect(database)) as db:
        if case.get("missing_mode") or "explicit_mode" in case:
            state, = db.execute("SELECT state_json FROM executions WHERE id=?", (h.SEED_EXECUTION,)).fetchone()
            state = json.loads(state)
            if case.get("missing_mode"):
                state.pop("publication_mode")
            else:
                state["publication_mode"] = case["explicit_mode"]
            db.execute("UPDATE executions SET state_json=? WHERE id=?", (json.dumps(state), h.SEED_EXECUTION))
            changed = True
        if case.get("labels"):
            db.execute("UPDATE work_items SET request_json=json_set(request_json,'$.label','campaign')")
            changed = True
        if case.get("closures"):
            for execution, number, merged in ((EXEC2, 7, False), (EXEC3, 8, True)):
                state, = db.execute("SELECT state_json FROM executions WHERE id=?", (execution,)).fetchone()
                state = json.loads(state)
                url = f"https://github.com/example/fixture/pull/{number}"
                state.update(stage="delivery", status="completed", target="checked_in" if merged else "ready_for_check_in",
                    candidate="a"*40, primary_pr=url, ready_revision={"head": "a"*40, "base": "b"*40},
                    wait=None, final_output={"url": url, "revision": {"head": "a"*40, "base": "b"*40}, "draft": False,
                        "merge_commit": "c"*40 if merged else None, "closed": merged, "merge_method": "merge" if merged else None,
                        "merged_at": STAMP if merged else None})
                db.execute("UPDATE executions SET state_json=?,updated_at=? WHERE id=?", (json.dumps(state), STAMP, execution))
                db.execute("INSERT INTO operator_pr VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (execution, url, number, "closed" if merged else "open", 0, int(merged), "a"*40, 12, 2, "satisfied", "a"*40, 1))
            changed = True
        if case.get("result_cause"):
            cause = {"outcome": "failed", "local_checks": [{"name": "Synthetic check", "outcome": "failed",
                "reason": "bearer synthetic-value token='synthetic-value'"}], "failure_reason": "Synthetic failure",
                "candidate": "a"*40}
            db.execute("UPDATE transition_history SET details_json=? WHERE execution_id=? AND event='result'",
                       (json.dumps(cause), h.SEED_EXECUTION))
            changed = True
        if case.get("confirm"):
            db.execute("UPDATE writers SET confirmed_at=created_at,session_id='synthetic-session-id' WHERE id='fixture-writer'")
            changed = True
        db.commit()
    return changed


def comparable(response, case):
    response = deepcopy(response)
    if case.get("home") and response["json"]:
        response["json"]["identity"]["home"] = "$DISPOSABLE_HOME"
    return h.comparable(response)


def sequence(url, database, label):
    actual, variables, tokens, epoch = [], {}, {}, 1
    for case in cases():
        if fixture(database, case):
            epoch += 1
        with closing(sqlite3.connect(database)) as db:
            db.execute("UPDATE operator_change SET token=?", (f"{epoch:032X}",))
            db.commit()
        request = deepcopy(case)
        request["path"] = resolve(case["path"], variables)
        request["body"] = json.dumps(resolve(case["body"], variables)) if case["body"] is not None else ""
        if "conditional" in case:
            value = variables["$" + case["conditional"]]
            if case.get("strong"):
                value = value.removeprefix("W/")
            request["headers"].append(("If-None-Match", value))
        before = h.snapshot(database)
        response = h.wire(url, request["method"], request["path"], request["headers"], request["body"])
        after = h.snapshot(database)
        if (request["method"] == "GET" or response["status"] >= 400) and before != after:
            raise AssertionError("Operator read/refusal changed durable data: " + case["id"])
        changed = before["tables"]["operator_change"] != after["tables"]["operator_change"]
        token = after["tables"]["operator_change"]["rows"][0][0]["value"]
        tokens.setdefault(token, len(tokens))
        actual.append({"request": request, "response": response, "durable": after, "change_token_ordinal": tokens[token]})
        if changed:
            epoch += 1
        if "save_tag" in case:
            tags = [value for name, value in response["headers"] if name == "etag"]
            if len(tags) != 1:
                raise AssertionError("Missing conditional validator: " + case["id"] + "\n" + response["text"])
            variables["$" + case["save_tag"]] = tags[0]
        if "save_cursor" in case:
            cursor = response["json"]["next_cursor"]
            if cursor is None:
                raise AssertionError("Expected a real continuation cursor: " + case["id"])
            variables["$" + case["save_cursor"]] = cursor
        if case.get("save_binding"):
            parsed = response["json"]
            variables["$VERSION"], variables["$ATTEMPT"], variables["$STAGE"] = (
                parsed["version"], parsed["writer"]["attempt_id"], parsed["state"]["stage"])
        if "semantic_case" in case:
            expected = 500 if label == "P0" else 200
            if response["status"] != expected:
                raise AssertionError("Named v4 classification changed: " + case["id"])
            if expected == 200 and response["json"]["item"]["publication_mode"] != "ready":
                raise AssertionError("Omitted publication mode did not retain ready default")
    return actual


def differential(binary, output):
    h.verify_manifest()
    h.verify_vectors()
    output.mkdir(parents=True, exist_ok=True)
    with h.disposable_root() as root:
        sources = {label: h.export(label, root) for label in h.PINS}
        for retained in (False, True):
            shape, expected = "retained" if retained else "fresh", None
            for label in ("P0", "D0", "Go"):
                directory = root / f"operator-{shape}-{label}"
                database = h.fresh(directory, sources, retained)
                command = [str(binary), "--fixture", str(database)] if label == "Go" else [
                    sys.executable, "-P", str(h.HERE / "oracle.py"), "serve", str(sources[label]), str(database), "--operator-clock"]
                with h.server(command, directory) as url:
                    actual = sequence(url, database, label)
                (output / f"operator-{shape}-{label}.json").write_text(json.dumps(actual, ensure_ascii=True), encoding="utf-8")
                if expected is None:
                    expected = actual
                    continue
                for first, second in zip(expected, actual, strict=True):
                    case = first["request"]
                    if "semantic_case" not in case and comparable(first["response"], case) != comparable(second["response"], case):
                        raise AssertionError(f"{shape}/{label}/{case['id']}: wire mismatch\n"
                                             f"{comparable(first['response'],case)!r}\n{comparable(second['response'],case)!r}")
                    if h.comparable_durable(first) != h.comparable_durable(second):
                        raise AssertionError(f"{shape}/{label}/{case['id']}: durable mismatch")
            print(f"{shape}: {len(expected)} operator/cursor/validator/timeline vectors x P0,D0,Go PASS (one named v4 discrepancy)")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    differential(args.binary.resolve(), args.output.resolve())


if __name__ == "__main__":
    main()

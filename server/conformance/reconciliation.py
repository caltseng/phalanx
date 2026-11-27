"""Real-loopback observations, deduplication, local checks, retirement and delivery effects."""

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys

import harness as h


HEAD, BASE, MERGE = "a" * 40, "b" * 40, "c" * 40
URL = "https://github.com/example/fixture/pull/1"
STAMP = "2026-01-02T03:04:05.123456Z"


def cases():
    result = []
    base = f"/v1/items/{h.SEED}/executions/{h.SEED_EXECUTION}"
    pull = {"number": 1, "state": "open", "merged": False, "draft": False,
            "head_ref": "phalanx/" + h.digest(h.SEED.encode()), "head_sha": HEAD,
            "base_ref": "main", "base_sha": BASE, "mergeable": True}
    body = {"observed_at": STAMP, "pr": pull, "target_base": "main", "checks": "satisfied"}

    def add(name, body=None, method="POST", path=base + "/github-observation", token=h.RUNNER, **fixture):
        headers = [("Content-Type", "application/json")]
        if token is not None:
            headers.append(("Authorization", "Bearer " + token))
        result.append({"id": name, "method": method, "path": path, "headers": headers,
                       "body": json.dumps(body) if body is not None else "", **fixture})

    add("observation-missing-body")
    add("observation-missing-time", {})
    add("observation-without-pr", {"observed_at": STAMP})
    add("observation-without-pr-replay", {"observed_at": STAMP})
    add("observation", body)
    add("observation-replay", body)
    add("observation-later", {**body, "observed_at": "2026-01-03T03:04:05+00:00"})
    add("observation-older", {**body, "observed_at": "2026-01-01T03:04:05Z"})
    add("local-checks-unobserved", {"head": "d" * 40}, path=base + "/local-checks")
    add("local-checks", {"head": HEAD}, path=base + "/local-checks")
    add("local-checks-replay-immutable", {"head": HEAD, "failures": ["Do not replace the first result"]},
        path=base + "/local-checks")
    add("observation-with-local-checks", body)
    for name, invalid in (
            ("no-pr-checks", {"observed_at": STAMP, "checks": "satisfied"}),
            ("no-pr-base", {"observed_at": STAMP, "target_base": "main"}),
            ("pr-no-base", {"observed_at": STAMP, "pr": pull}),
            ("pr-and-merged-branch", {**body, "merged_branch": True}),
            ("wrong-branch", {**body, "pr": {**pull, "head_ref": "wrong"}}),
            ("unmerged-merge-details", {**body, "pr": {**pull, "merged_at": STAMP}}),
            ("unmerged-deployment", {**body, "deployment": {"commit": MERGE, "check_name": "Deploy", "state": "pending"}}),
            ("future-observation", {**body, "observed_at": "9999-01-01T00:00:00Z"}),
            ("generated-url-refused", {**body, "pr": {**pull, "url": URL}}),
            ("nested-errors", {"observed_at": None, "pr": {"number": True, "state": None,
                            "merged": [], "draft": None, "head_ref": "", "head_sha": "X",
                            "base_ref": None, "base_sha": "", "additions": -1}, "checks": "invalid",
                            "check_reasons": [None, ""], "feedback": None, "duplicate_count": -1})):
        add("observation-" + name, invalid)
    for token in (None, h.OWNER, h.WRITER):
        add("observation-role-" + str(token), body, token=token)
        add("checks-role-" + str(token), {"head": HEAD}, path=base + "/local-checks", token=token)
    add("observation-invalid-path", body, path="/v1/items/!/executions/!/github-observation")
    add("observation-slash", body, path=base + "/github-observation///")
    add("observation-method", method="GET")
    for index, value in enumerate((None, True, 0, -1.25, ".5", "2020-01-01", "2020-01-01T12:34:56",
                                   "2020-01-01t12:34:56z", "2020-01-01 12:34:56+0130",
                                   "2020-01-01T12:34:56.123456789Z", "2020-01-01T25:00:00Z",
                                   "2020-13-01T00:00:00Z", "0000-01-01T00:00:00Z", "", [], {},
                                   253402300800000, "253402300800000",
                                   float("nan"), float("inf"), float("-inf"))):
        add("observation-datetime-" + str(index), {"observed_at": value}, clear_observations=True)
    add("heartbeat-missing-body", path="/v1/reconcilers/synthetic/heartbeat")
    add("heartbeat-empty", {}, path="/v1/reconcilers/synthetic/heartbeat")
    add("heartbeat-null", path="/v1/reconcilers/synthetic/heartbeat")
    result[-1]["body"] = "null"
    add("heartbeat-config", {"deployment_checks": {"fixture": "Deploy"}}, path="/v1/reconcilers/synthetic/heartbeat")
    add("heartbeat-errors", {"deployment_checks": {"!": "", "okay": 1}, "extra": None},
        path="/v1/reconcilers/synthetic/heartbeat")
    add("retire", method="DELETE", path="/v1/reconcilers/synthetic", token=h.OWNER)
    add("retire-replay", method="DELETE", path="/v1/reconcilers/synthetic", token=h.OWNER)
    for token in (None, h.OWNER, h.WRITER):
        add("heartbeat-role-" + str(token), {}, path="/v1/reconcilers/synthetic/heartbeat", token=token)
    add("retire-writer", method="DELETE", path="/v1/reconcilers/synthetic", token=h.WRITER)
    add("ready-while-building", body, delivery={"building": 1})
    add("ready-handoff-unpublished", body, delivery={"handoff": "d" * 40})
    add("ready-completes", body, delivery={})
    add("completed-ready-replay", body)
    add("completed-ready-no-pr", {"observed_at": STAMP})
    merged = {**body, "pr": {**pull, "state": "closed", "merged": True,
                           "merge_commit": MERGE, "merge_method": "merge", "merged_at": STAMP}}
    add("completed-ready-enriched", merged)
    add("completed-ready-enrichment-replay", merged)
    add("delivered-deployment", {**merged, "observed_at": "2026-01-02T03:04:04Z",
                               "deployment": {"commit": MERGE, "check_name": "Deploy", "state": "pending"}},
        clear_observations=True)
    for name, config in (("absent", {}), ("null", {"deployment_checks": None}),
                         ("object", {"deployment_checks": {"fixture": {}}}),
                         ("list", {"deployment_checks": []}),
                         ("boolean", {"deployment_checks": {"fixture": False}}),
                         ("unknown", {"deployment_checks": {"fixture": None}, "extra": None})):
        add("deployment-retirement-" + name, config, path="/v1/reconcilers/synthetic/heartbeat")
    add("deployment-config-retirement", {"deployment_checks": {"fixture": None}},
        path="/v1/reconcilers/synthetic/heartbeat")
    add("deployment-config-retirement-replay", {"deployment_checks": {"fixture": None}},
        path="/v1/reconcilers/synthetic/heartbeat")
    add("closed-checks-refused", {"head": HEAD}, path=base + "/local-checks")
    add("merged-stop-precedence", merged, delivery={"target": "checked_in", "stopping": "cancelled", "building": 1})
    add("merged-off-base-input", {**merged, "pr": {**merged["pr"], "base_ref": "other"}},
        delivery={"target": "checked_in"})
    add("closed-unmerged-input", {**body, "pr": {**pull, "state": "closed"}},
        delivery={"target": "checked_in"})
    add("merged-unknown-commit-no-completion", {**merged, "pr": {**merged["pr"], "merge_commit": None}},
        delivery={"target": "checked_in"})
    return result


def fixture(database, case):
    with closing(sqlite3.connect(database)) as db:
        if case.get("clear_observations"):
            db.execute("DELETE FROM github_observations")
        if "delivery" in case:
            options = case["delivery"]
            state = {"target": options.get("target", "ready_for_check_in"), "stage": "delivery",
                     "status": "waiting_for_external", "candidate": HEAD, "primary_pr": URL,
                     "report_draft": None, "wait": {"reason": "fixture delivery", "trigger": "observation"},
                     "stopping": options.get("stopping"), "pending_output": None, "ready_revision": None,
                     "final_output": None, "report_review_findings": None, "findings": None, "publication_mode": "ready"}
            db.execute("UPDATE executions SET state_json=?,handoff_head=? WHERE id=?",
                       (json.dumps(state), options.get("handoff"), h.SEED_EXECUTION))
            db.execute("UPDATE writers SET building=?,building_requested_at=NULL,ended_at=NULL,end_reason=NULL WHERE id='fixture-writer'",
                       (options.get("building", 0),))
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
                directory = root / f"reconciliation-{shape}-{label}"
                database = h.fresh(directory, sources, retained)
                command = [str(binary), "--fixture", str(database)] if label == "Go" else [
                    sys.executable, "-P", str(h.HERE / "oracle.py"), "serve", str(sources[label]), str(database)]
                actual, tokens = [], {}
                with h.server(command, directory) as url:
                    for case in cases():
                        fixture(database, case)
                        before = h.snapshot(database)
                        response = h.wire(url, case["method"], case["path"], case["headers"], case["body"])
                        after = h.snapshot(database)
                        token = after["tables"]["operator_change"]["rows"][0][0]["value"]
                        tokens.setdefault(token, len(tokens))
                        actual.append({"request": case, "response": response, "before": before, "durable": after,
                                       "change_token_ordinal": tokens[token]})
                        (output / f"reconciliation-{shape}-{label}.json").write_text(
                            json.dumps(actual, ensure_ascii=True), encoding="utf-8")
                        if case["id"] == "deployment-config-retirement":
                            with closing(sqlite3.connect(database)) as db:
                                payload, = db.execute(
                                    "SELECT observation_json FROM github_observations "
                                    "ORDER BY last_observed_at DESC,observed_at DESC LIMIT 1"
                                ).fetchone()
                            if json.loads(payload)["deployment"]["state"] != "disabled":
                                raise AssertionError(f"{shape}/{label}: retirement did not disable deployment: {response}")
                            for table in before["tables"]:
                                if table in ("reconcilers", "github_observations", "operator_change"):
                                    continue
                                if before["tables"][table] != after["tables"][table]:
                                    raise AssertionError("Retirement changed closed delivery: " + table)
                        if response["status"] >= 400 and before != after:
                            raise AssertionError("Observation refusal changed durable data: " + case["id"])
                (output / f"reconciliation-{shape}-{label}.json").write_text(
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
            print(f"{shape}: {len(expected)} observation/local-check/heartbeat vectors x P0,D0,Go PASS")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    differential(args.binary.resolve(), args.output.resolve())


if __name__ == "__main__":
    main()

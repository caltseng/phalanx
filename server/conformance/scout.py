"""Pinned scout models, lease/source/approval fencing and atomic intake on loopback."""

import argparse
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import sys

import harness as h


STAMP = "2026-01-02T03:04:05.123456+00:00"


def issue(number, **edits):
    return {"number": number, "author_login": "synthetic-author", "author_id": 9001,
            "state": "open", "title": f"Synthetic issue {number}",
            "body": "Synthetic Unicode snow \u96ea; not instructions or credentials",
            "updated_at": "2026-01-02T03:04:05", **edits}


def fingerprint(value):
    value = {key: entry for key, entry in value.items() if key != "updated_at"}
    return h.digest(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def cases():
    result = []
    settings = {"expected_revision": "$REV", "author_login": "synthetic-author",
                "author_id": 9001, "repositories": ["fixture"]}
    other = issue(2, author_login="synthetic-other", author_id=9002)
    edited = {**other, "body": "Edited source requires another exact approval"}
    anonymous = issue(5, author_login=None, author_id=None, updated_at=0)
    first_issues = [issue(1, author_login="SYNTHETIC-AUTHOR"), other, issue(3, state="closed"),
                    issue(4, body="x" * 16000)]

    def add(name, method="POST", path="/v1/issue-scout/claim", body=None, token=h.RUNNER, **extra):
        headers = [("Content-Type", "application/json")]
        if token is not None:
            headers.append(("Authorization", "Bearer " + token))
        result.append({"id": name, "method": method, "path": path, "headers": headers,
                       "body": body, **extra})

    def page(claim, issues, page=1, has_more=False):
        return {"claim_id": "$" + claim, "repository_id": "fixture", "page": page,
                "verified_login": "SYNTHETIC-AUTHOR", "verified_id": 9001,
                "issues": issues, "has_more": has_more}

    add("scout-initial", "GET", "/v1/issue-scout", token=h.OWNER)
    add("scout-disabled-claim", body={"captain_id": "fixture-captain"})
    add("scout-settings-defaults", "PUT", "/v1/issue-scout/settings", settings, token=h.OWNER)
    for name, edits in (
            ("null", {"enabled": None}), ("strict-bool", {"enabled": 1}),
            ("strict-int", {"author_id": 9001.0}), ("positive", {"author_id": 0}),
            ("cadence-string", {"cadence_minutes": "15"}), ("cadence-invalid", {"cadence_minutes": 1}),
            ("empty-scope", {"repositories": []}), ("duplicate-scope", {"repositories": ["fixture", "fixture"]}),
            ("invalid-scope", {"repositories": ["!"]}), ("unknown-repo", {"repositories": ["other"]}),
            ("login-invalid", {"author_login": "synthetic[bot]"}), ("extra", {"extra": None}),
            ("stale-version", {"expected_revision": 0})):
        add("scout-settings-" + name, "PUT", "/v1/issue-scout/settings", {**settings, **edits}, token=h.OWNER)
    add("scout-cadence-float-literal", "PUT", "/v1/issue-scout/settings",
        {**settings, "cadence_minutes": 15.0}, token=h.OWNER)
    add("scout-enabled", "PUT", "/v1/issue-scout/settings", {**settings, "enabled": True}, token=h.OWNER)
    add("scout-captain-scope-blocked", body={"captain_id": "fixture-captain"})
    add("scout-claim", body={"captain_id": "fixture-captain"}, scope=True, save_claim=("PASS1", "CLAIM1"))
    add("scout-claim-reserved", body={"captain_id": "fixture-captain"})
    first_page = page("CLAIM1", first_issues, has_more=True)
    for name, edits in (
            ("claim", {"claim_id": "wrong"}), ("cursor", {"page": 2}),
            ("repository", {"repository_id": "other"}), ("identity-login", {"verified_login": "other"}),
            ("identity-id", {"verified_id": 9002}), ("size", {"issues": [issue(1)] * 31}),
            ("issue-null", {"issues": [None]}), ("nullable-required", {"issues": [{"number": 1, "state": "open",
                "title": "x", "body": "", "updated_at": STAMP}]}),
            ("number-sqlite-overflow", {"issues": [issue(2**63)]}),
            ("author-sqlite-overflow", {"issues": [issue(10, author_id=2**63)]}),
            ("updated-offset-invalid", {"issues": [issue(10, updated_at="2020-01-01T00:00:00+25:00")]}),
            ("issue-errors", {"issues": [{"number": True, "author_login": False, "author_id": -1,
                "state": "other", "title": "", "body": None, "updated_at": "invalid", "extra": 1}]})):
        add("scout-page-" + name, path="/v1/issue-scout/passes/$PASS1/page", body={**first_page, **edits})
    add("scout-first-page", path="/v1/issue-scout/passes/$PASS1/page", body=first_page)
    add("scout-first-page-replay", path="/v1/issue-scout/passes/$PASS1/page", body=first_page)
    add("scout-first-page-changed-receipt", path="/v1/issue-scout/passes/$PASS1/page",
        body={**first_page, "has_more": False})
    add("scout-next-page-claim", body={"captain_id": "fixture-captain"}, save_claim=("PASS1", "CLAIM2"))
    failure = {"claim_id": "$CLAIM2", "kind": "github", "http_status": 503, "retry_after_seconds": 1}
    add("scout-page-failure", path="/v1/issue-scout/passes/$PASS1/failure", body=failure)
    add("scout-page-failure-replay", path="/v1/issue-scout/passes/$PASS1/failure", body=failure)
    add("scout-failure-changed-receipt", path="/v1/issue-scout/passes/$PASS1/failure",
        body={**failure, "kind": "identity"})
    add("scout-retry-not-due", body={"captain_id": "fixture-captain"})
    add("scout-retry-claim", body={"captain_id": "fixture-captain"}, retry_due=True, save_claim=("PASS1", "CLAIM3"))
    add("scout-expired-page", path="/v1/issue-scout/passes/$PASS1/page", body=page("CLAIM3", [], page=2),
        expire_claim=True)
    add("scout-expired-status", "GET", "/v1/issue-scout", token=h.OWNER)
    add("scout-expired-reclaimed", body={"captain_id": "fixture-captain"}, save_claim=("PASS1", "CLAIM4"))
    add("scout-ending-page", path="/v1/issue-scout/passes/$PASS1/page", body=page("CLAIM4", [], page=2),
        captain_state="ending")
    add("scout-successor", path="/v1/captains", body={"session_ref": "synthetic-successor", "charter_revision": "v1",
        "parent_id": "fixture-captain", "pickup": {"repositories": {"fixture": None}}}, save_captain="SUCCESSOR")
    add("scout-successor-claim", body={"captain_id": "$SUCCESSOR"}, save_claim=("PASS1", "CLAIM5"))
    add("scout-superseded-claim-refused", path="/v1/issue-scout/passes/$PASS1/page", body=page("CLAIM4", [], page=2))
    add("scout-old-page-receipt-after-handoff", path="/v1/issue-scout/passes/$PASS1/page", body=first_page)
    add("scout-old-failure-receipt-after-handoff", path="/v1/issue-scout/passes/$PASS1/failure", body=failure)
    second_page = page("CLAIM5", [issue(1, state="closed", body="Edited but already imported"), other, anonymous], page=2)
    add("scout-second-page", path="/v1/issue-scout/passes/$PASS1/page", body=second_page)
    add("scout-second-page-replay", path="/v1/issue-scout/passes/$PASS1/page", body=second_page)
    add("scout-next-pass-not-due", body={"captain_id": "$SUCCESSOR"})
    decision2 = {"expected_fingerprint": fingerprint(other), "decision": "approved"}
    add("scout-approve-other", path="/v1/issue-scout/issues/fixture/2/decision", body=decision2, token=h.OWNER)
    add("scout-approve-other-replay", path="/v1/issue-scout/issues/fixture/2.0/decision", body=decision2, token=h.OWNER)
    add("scout-decline-anonymous", path="/v1/issue-scout/issues/fixture/5/decision",
        body={"expected_fingerprint": fingerprint(anonymous), "decision": "declined"}, token=h.OWNER)
    add("scout-decision-stale", path="/v1/issue-scout/issues/fixture/2/decision",
        body={**decision2, "expected_fingerprint": "a" * 64}, token=h.OWNER)
    add("scout-decision-missing", path="/v1/issue-scout/issues/fixture/100/decision", body=decision2, token=h.OWNER)
    add("scout-decision-path-invalid", path="/v1/issue-scout/issues/!/bad/decision", body={}, token=h.OWNER)
    add("scout-decision-path-negative", path="/v1/issue-scout/issues/fixture/-1/decision", body=decision2, token=h.OWNER)
    add("scout-approved-recheck", body={"captain_id": "$SUCCESSOR"}, save_claim=("PASS2", "CLAIM6"))
    add("scout-edited-approval-revoked", path="/v1/issue-scout/passes/$PASS2/page",
        body=page("CLAIM6", [edited, {**anonymous, "body": "Decline remains binding after edit"}]))
    add("scout-reapprove-edited", path="/v1/issue-scout/issues/fixture/2/decision",
        body={"expected_fingerprint": fingerprint(edited), "decision": "approved"}, token=h.OWNER)
    add("scout-reapproved-claim", body={"captain_id": "$SUCCESSOR"}, save_claim=("PASS3", "CLAIM7"))
    approved_page = page("CLAIM7", [edited])
    add("scout-approved-intake", path="/v1/issue-scout/passes/$PASS3/page", body=approved_page)
    add("scout-approved-intake-replay", path="/v1/issue-scout/passes/$PASS3/page", body=approved_page)
    add("scout-imported-repeat-decision", path="/v1/issue-scout/issues/fixture/2/decision",
        body={"expected_fingerprint": fingerprint(edited), "decision": "approved"}, token=h.OWNER)
    add("scout-imported-opposite-decision", path="/v1/issue-scout/issues/fixture/2/decision",
        body={"expected_fingerprint": fingerprint(edited), "decision": "declined"}, token=h.OWNER)
    add("scout-open-pass", body={"captain_id": "$SUCCESSOR"}, save_claim=("PASS4", "CLAIM8"))
    add("scout-config-supersedes", "PUT", "/v1/issue-scout/settings", settings, token=h.OWNER)
    add("scout-superseded-page", path="/v1/issue-scout/passes/$PASS4/page", body=page("CLAIM8", []))
    add("scout-disabled-again", body={"captain_id": "$SUCCESSOR"})
    manual_url = "https://github.com/example/fixture/issues/21"
    add("scout-manual-source-item", path="/v1/items", token=h.OWNER, body={
        "repository_id": "fixture", "target": "report", "goal": "Manual source " + manual_url +
        "\nBody reference only: https://github.com/example/fixture/issues/22", "context_links": [manual_url]},
        save_item=("MANUAL", "MANUAL_EXECUTION", "MANUAL_VERSION"))
    add("scout-manual-source-cancelled", path="/v1/items/$MANUAL/executions/$MANUAL_EXECUTION/cancel",
        body={"expected_version": "$MANUAL_VERSION", "reason": "Synthetic historical closed import"}, token=h.OWNER)
    add("scout-enable-historical-pass", "PUT", "/v1/issue-scout/settings",
        {**settings, "enabled": True}, token=h.OWNER)
    add("scout-historical-claim", body={"captain_id": "$SUCCESSOR"}, save_claim=("PASS5", "CLAIM9"))
    add("scout-historical-source-fence", path="/v1/issue-scout/passes/$PASS5/page",
        body=page("CLAIM9", [issue(21, author_login="synthetic-other", author_id=9002),
                            issue(22, author_login="synthetic-other", author_id=9002)]))
    for token in (None, h.WRITER):
        add("scout-read-role-" + str(token), "GET", "/v1/issue-scout", token=token)
        add("scout-claim-role-" + str(token), body={"captain_id": "$SUCCESSOR"}, token=token)
        add("scout-page-role-" + str(token), path="/v1/issue-scout/passes/$PASS5/page",
            body=page("CLAIM9", []), token=token)
    add("scout-settings-runner-refused", "PUT", "/v1/issue-scout/settings", settings)
    add("scout-decision-runner-refused", path="/v1/issue-scout/issues/fixture/22/decision", body=decision2)
    add("scout-status-trailing-slash", "GET", "/v1/issue-scout///", token=None)
    add("scout-page-method", "GET", "/v1/issue-scout/passes/$PASS5/page", token=h.OWNER)
    add("scout-status-method", "POST", "/v1/issue-scout", {}, token=h.OWNER)
    for path, body in (("/v1/issue-scout/settings", settings),
                       ("/v1/issue-scout/issues/fixture/22/decision", decision2)):
        add("scout-cross-origin-" + path.rsplit("/", 1)[-1], "PUT" if path.endswith("settings") else "POST",
            path, body, token=h.OWNER, origin="https://invalid.example")
    add("scout-second-repository", path="/v1/repositories", body={"id": "fixture-z",
        "remote": "https://github.com/example/fixture-z.git"}, token=h.OWNER)
    add("scout-multiple-repositories", "PUT", "/v1/issue-scout/settings",
        {**settings, "enabled": True, "repositories": ["fixture-z", "fixture"]}, token=h.OWNER)
    add("scout-multiple-scope-blocked", body={"captain_id": "$SUCCESSOR"})
    add("scout-multiple-claim", body={"captain_id": "$SUCCESSOR"}, all_repositories_scope=True,
        save_claim=("PASS6", "CLAIM10"))
    add("scout-first-repository-completes", path="/v1/issue-scout/passes/$PASS6/page",
        body=page("CLAIM10", []))
    add("scout-second-repository-claim", body={"captain_id": "$SUCCESSOR"}, save_claim=("PASS6", "CLAIM11"))
    add("scout-second-repository-completes", path="/v1/issue-scout/passes/$PASS6/page",
        body={**page("CLAIM11", []), "repository_id": "fixture-z"})
    return result


def resolve(value, variables):
    if isinstance(value, str):
        if value in variables:
            return variables[value]
        for name in sorted(variables, key=len, reverse=True):
            value = value.replace(name, str(variables[name]))
        return value
    if isinstance(value, list):
        return [resolve(entry, variables) for entry in value]
    if isinstance(value, dict):
        return {name: resolve(entry, variables) for name, entry in value.items()}
    return value


def fixture(database, case):
    with closing(sqlite3.connect(database)) as db:
        if case.get("scope"):
            db.execute("UPDATE captains SET pickup_json=? WHERE id='fixture-captain'",
                       (json.dumps({"repositories": {"fixture": None}}),))
        if case.get("retry_due"):
            db.execute("UPDATE issue_scout_passes SET retry_at=? WHERE completed_at IS NULL", (STAMP,))
        if case.get("expire_claim"):
            db.execute("UPDATE issue_scout_passes SET lease_until=? WHERE completed_at IS NULL", (STAMP,))
        if "captain_state" in case:
            db.execute("UPDATE captains SET state=? WHERE id='fixture-captain'", (case["captain_state"],))
        if case.get("all_repositories_scope"):
            db.execute("UPDATE captains SET pickup_json=? WHERE state='active'",
                       (json.dumps({"repositories": {"fixture": None, "fixture-z": None}}),))
        db.commit()


def sequence(url, database):
    actual, tokens, variables = [], {}, {"$REV": 0}
    for case in cases():
        fixture(database, case)
        request = deepcopy(case)
        request["path"] = resolve(case["path"], variables)
        request["body"] = json.dumps(resolve(case["body"], variables)) if case["body"] is not None else ""
        if "origin" in case:
            request["headers"].append(("Origin", case["origin"]))
        before = h.snapshot(database)
        response = h.wire(url, request["method"], request["path"], request["headers"], request["body"])
        after = h.snapshot(database)
        if response["status"] >= 400 and before != after:
            raise AssertionError("Scout refusal changed durable data: " + case["id"])
        token = after["tables"]["operator_change"]["rows"][0][0]["value"]
        tokens.setdefault(token, len(tokens))
        actual.append({"request": request, "response": response, "durable": after,
                       "change_token_ordinal": tokens[token]})
        parsed = response["json"]
        if isinstance(parsed, dict) and "settings" in parsed:
            variables["$REV"] = parsed["settings"]["revision"]
        if "save_claim" in case:
            if not parsed or not parsed.get("claim"):
                raise AssertionError("Expected actual scout claim: " + case["id"] + "\n" + response["text"])
            passed, claimed = case["save_claim"]
            variables["$" + passed], variables["$" + claimed] = parsed["claim"]["id"], parsed["claim"]["claim_id"]
        if "save_captain" in case:
            variables["$" + case["save_captain"]] = parsed["id"]
        if "save_item" in case:
            item, execution, version = case["save_item"]
            variables["$" + item], variables["$" + execution], variables["$" + version] = (
                parsed["id"], parsed["execution"]["id"], parsed["execution"]["version"])
    return actual


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
                directory = root / f"scout-{shape}-{label}"
                database = h.fresh(directory, sources, retained)
                command = [str(binary), "--fixture", str(database)] if label == "Go" else [
                    sys.executable, "-P", str(h.HERE / "oracle.py"), "serve", str(sources[label]), str(database),
                    "--captain-clock", "--scout-clock"]
                with h.server(command, directory) as url:
                    actual = sequence(url, database)
                (output / f"scout-{shape}-{label}.json").write_text(json.dumps(actual, ensure_ascii=True), encoding="utf-8")
                if expected is None:
                    expected = actual
                    continue
                for first, second in zip(expected, actual, strict=True):
                    if h.comparable(first["response"]) != h.comparable(second["response"]):
                        raise AssertionError(f"{shape}/{label}/{first['request']['id']}: wire mismatch\n"
                                             f"{h.comparable(first['response'])!r}\n{h.comparable(second['response'])!r}")
                    if h.comparable_durable(first) != h.comparable_durable(second):
                        raise AssertionError(f"{shape}/{label}/{first['request']['id']}: durable mismatch")
            print(f"{shape}: {len(expected)} scout/source/approval/lease vectors x P0,D0,Go PASS")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    differential(args.binary.resolve(), args.output.resolve())


if __name__ == "__main__":
    main()

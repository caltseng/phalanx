"""Real HTTP stage continuation and immutable receipts across both pinned packages."""

import argparse
import json
from pathlib import Path
import sys

import harness as h
import writers


def cases():
    result = []
    report = f"/v1/items/{h.SEED}/executions/{h.SEED_EXECUTION}"
    pr = f"/v1/items/{writers.ITEM2}/executions/{writers.EXECUTION2}"
    markdown = "\n# Native report \u96ea\n\nOriginal draft.\n"
    revised = "# Revised native report \u96ea\n\nReviewed final.\n"
    revision, final_revision = h.digest(markdown.encode()), h.digest(revised.encode())

    def add(name, method="POST", path="/v1/writer/attempt", body=None, token=h.WRITER, key=None):
        headers = [] if token is None else [("Authorization", "Bearer " + token)]
        headers.append(("Content-Type", "application/json"))
        if key is not None:
            headers.append(("Idempotency-Key", key))
        result.append({"id": name, "method": method, "path": path, "headers": headers,
                       "body": json.dumps(body) if body is not None else ""})

    def stage(name, base, version, attempt, phase, outcome="succeeded", token=h.WRITER, key=None, **fields):
        add(name, path=base + "/result", token=token, key=key, body={
            "expected_version": version, "attempt_id": f"{attempt:032x}", "stage": phase,
            "outcome": outcome, **fields})

    add("report-claim")
    stage("report-draft-required", report, 2, 1, "report_drafting")
    stage("report-question-required", report, 2, 1, "report_drafting", "needs_input",
          wait={"reason": "Question absent", "trigger": "owner reply"})
    stage("report-drafting", report, 2, 1, "report_drafting", key="draft", report_markdown=markdown)
    stage("report-drafting-replay", report, 2, 1, "report_drafting", key="draft", report_markdown=markdown)
    stage("report-drafting-key-conflict", report, 2, 1, "report_drafting", key="draft", report_markdown=revised)
    add("report-draft-exact-utf8", "GET", report + "/drafts/" + revision)
    stage("report-review-wrong-revision", report, 4, 2, "report_review", report_revision="0" * 64)
    stage("report-review-changes", report, 4, 2, "report_review", "changes_required",
          report_revision=revision, report_review_notes="Revise the final section")
    stage("report-redraft", report, 6, 3, "report_drafting", report_markdown=revised)
    stage("report-review", report, 8, 4, "report_review", report_revision=final_revision)
    stage("report-storage-wrong-revision", report, 10, 5, "report_storage", report_revision=revision)
    stage("report-storage", report, 10, 5, "report_storage", key="store", report_revision=final_revision)
    add("report-stored-exact-utf8", "GET", report + "/report", token=h.OWNER)
    add("report-writer-ended", token=h.WRITER)
    add("report-final-history", "GET", report + "/timeline", token=h.OWNER)
    add("pr-claim", token=writers.TOKEN2)
    head = "a" * 40
    stage("pr-development", pr, 2, 6, "development", token=writers.TOKEN2, key="develop", candidate=head)
    stage("pr-development-replay", pr, 2, 6, "development", token=writers.TOKEN2, key="develop", candidate=head)
    stage("pr-review-wrong-head", pr, 4, 7, "self_review", token=writers.TOKEN2, reviewed_candidate="b" * 40)
    stage("pr-review", pr, 4, 7, "self_review", token=writers.TOKEN2,
          reviewed_candidate=head, review_notes="Current candidate checked")
    stage("pr-publication-wrong-repository", pr, 6, 8, "publication", token=writers.TOKEN2,
          pr_url="https://github.com/other/repository/pull/10")
    stage("pr-publication", pr, 6, 8, "publication", token=writers.TOKEN2,
          pr_url="https://github.com/EXAMPLE/FIXTURE/pull/10")
    add("pr-attempt-free-question", path=pr + "/result", token=writers.TOKEN2, key="question", body={
        "expected_version": 7, "stage": "delivery", "outcome": "needs_input",
        "question": {"prompt": "Continue delivery?", "options": ["Retry", "Stop"], "handoff": "Keep the same writer"}})
    add("pr-owner-inbox", "GET", "/v1/owner/inbox", token=h.OWNER)
    add("pr-reply-required-key", path=pr + "/reply", token=h.OWNER,
        body={"expected_version": 8, "answer": "Retry"})
    add("pr-owner-reply", path=pr + "/reply", token=h.OWNER, key="reply",
        body={"expected_version": 8, "answer": "Retry"})
    add("pr-owner-reply-replay", path=pr + "/reply", token=h.OWNER, key="reply",
        body={"expected_version": 8, "answer": "Retry"})
    add("pr-resume-retained-writer", token=writers.TOKEN2)
    stage("pr-delivery-after-reply", pr, 10, 9, "delivery", token=writers.TOKEN2)
    add("pr-final-history", "GET", pr + "/timeline", token=h.OWNER)
    add("pr-no-stored-report", "GET", pr + "/report", token=h.OWNER)
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
                directory = root / f"results-{shape}-{label}"
                database = h.fresh(directory, sources, retained)
                writers.seed(database)
                command = [str(binary), "--fixture", str(database)] if label == "Go" else [
                    sys.executable, "-P", str(h.HERE / "oracle.py"), "serve", str(sources[label]), str(database)]
                actual, tokens = [], {}
                with h.server(command, directory) as url:
                    for case in cases():
                        before = h.snapshot(database)
                        response = h.wire(url, case["method"], case["path"], case["headers"], case["body"])
                        after = h.snapshot(database)
                        if response["status"] >= 400 and before != after:
                            raise AssertionError("Refused result changed durable data: " + case["id"])
                        token = after["tables"]["operator_change"]["rows"][0][0]["value"]
                        tokens.setdefault(token, len(tokens))
                        actual.append({"request": case, "response": response, "durable": after,
                                       "change_token_ordinal": tokens[token]})
                (output / f"results-{shape}-{label}.json").write_text(
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
            print(f"{shape}: {len(expected)} full-stage result/reply vectors x P0,D0,Go PASS")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    differential(args.binary.resolve(), args.output.resolve())


if __name__ == "__main__":
    main()

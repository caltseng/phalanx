"""Exhaustive bounded state/event evidence corpus against both immutable Python domains."""

import argparse
from dataclasses import asdict
import itertools
import json
from pathlib import Path
import subprocess
import sys


def corpus():
    stages = ["development", "self_review", "report_drafting", "report_review",
              "report_storage", "publication", "delivery"]
    statuses = ["queued", "running", "waiting_for_input", "waiting_for_external",
                "completed", "failed", "cancelled"]
    events = ["claim", "result", "reply", "retry", "cancel", "close_failure",
              "stop_confirmed", "pr_observed", "finalize"]
    revision = {"head": "candidate", "base": "main"}
    pr = {"url": "https://github.com/example/fixture/pull/1", "revision": revision,
          "draft": False, "merge_commit": None, "closed": False,
          "merge_method": None, "merged_at": None}
    report = {"reference": "/report", "draft_revision": "draft"}
    evidence = {"candidate": "candidate", "reviewed_candidate": "candidate",
                "report_draft": "draft", "reviewed_report": "draft",
                "stored_report": report, "pr": pr, "published_pr": pr["url"],
                "report_review_notes": "Review findings", "findings": "Findings",
                "pr_ready": True}
    wait = {"reason": "Wait reason", "trigger": "Wait trigger"}
    options = {
        "result": [{"outcome": outcome, "evidence": contents, "wait": pause, "retry_allowed": retry}
                   for outcome, contents, pause, retry in [
                       (None, {}, None, False), ("succeeded", {}, None, False),
                       ("succeeded", evidence, None, False),
                       ("changes_required", {}, None, False), ("changes_required", evidence, None, False),
                       ("needs_input", {}, None, False), ("needs_input", {}, wait, False),
                       ("failed", {}, None, True), ("failed", {}, None, False), ("failed", {}, wait, False)]],
        "retry": [{}, {"retry_allowed": True}, {"wait": wait}],
        "pr_observed": [{}, {"evidence": evidence}],
        "finalize": [{}, {"evidence": evidence}, {"wait": wait}],
    }
    for target, mode, stage, status, event in itertools.product(
            ["report", "ready_for_check_in", "checked_in"], ["ready", "draft"],
            stages, statuses, events):
        state = {"target": target, "publication_mode": mode, "stage": stage, "status": status,
                 "candidate": "candidate", "primary_pr": pr["url"], "report_draft": "draft",
                 "wait": None, "stopping": None, "pending_output": None, "ready_revision": revision,
                 "final_output": (report if target == "report" or status != "completed" else pr)
                                if status in {"completed", "failed", "cancelled"} else None,
                 "report_review_findings": None, "findings": None}
        for choice in options.get(event, [{}]):
            yield {"state": state, "event": event, "options": {
                "outcome": None, "evidence": {}, "wait": None, **choice}}
    for pending, stopping, mode, target, status, event in itertools.product(
            [None, "completed", "failed", "cancelled"],
            [None, "failed", "cancelled"], ["ready", "draft"],
            ["ready_for_check_in", "checked_in", "report"],
            ["waiting_for_external"],
            events):
        for contents in ({}, evidence, {**evidence, "pr": {**pr, "merge_commit": "merge", "closed": True,
                                                         "merged_at": "2026-01-02", "merge_method": "merge"}},
                         {**evidence, "pr": {**pr, "draft": True}},
                         {**evidence, "pr": {**pr, "closed": True}},
                         {**evidence, "pr": {**pr, "revision": {"head": "different", "base": "main"}}},
                         {**evidence, "off_base": True,
                          "pr": {**pr, "merge_commit": "merge", "closed": True, "merged_at": "2026-01-02"}}):
            state = {"target": target, "publication_mode": mode, "stage": "report_storage" if target == "report" else "delivery",
                     "status": status, "candidate": "candidate", "primary_pr": pr["url"], "report_draft": "draft",
                     "wait": None, "stopping": stopping, "pending_output": pending, "ready_revision": revision,
                     "final_output": None, "report_review_findings": None, "findings": None}
            yield {"state": state, "event": event, "options": {
                "outcome": "succeeded", "evidence": contents,
                "wait": {"reason": "Wait reason", "trigger": "Wait trigger"}}}


def python_results(source):
    sys.path.insert(0, str(source))
    from phalanx.transitions import (Execution, Evidence, Wait, PullRequest, Revision, StoredReport,
                                    Stage, Status, Target, PublicationMode, Event, Outcome, decide)

    def output(value):
        if value is None:
            return None
        if "reference" in value:
            return StoredReport(**value)
        return PullRequest(**{**value, "revision": Revision(**value["revision"])})

    for index, request in enumerate(corpus()):
        raw = request["state"]
        state = Execution(**{**raw, "target": Target(raw["target"]), "stage": Stage(raw["stage"]),
            "status": Status(raw["status"]), "publication_mode": PublicationMode(raw["publication_mode"]),
            "stopping": Status(raw["stopping"]) if raw["stopping"] else None,
            "pending_output": Status(raw["pending_output"]) if raw["pending_output"] else None,
            "ready_revision": Revision(**raw["ready_revision"]), "final_output": output(raw["final_output"])})
        options = request["options"]
        evidence = options["evidence"]
        evidence = Evidence(**{**evidence, **({"pr": output(evidence["pr"])} if "pr" in evidence else {}),
                               **({"stored_report": output(evidence["stored_report"])} if "stored_report" in evidence else {})})
        try:
            result = decide(state, Event(request["event"]), evidence=evidence,
                            outcome=Outcome(options["outcome"]) if options["outcome"] else None,
                            wait=Wait(**options["wait"]) if options["wait"] else None,
                            retry_allowed=options.get("retry_allowed", False))
            response = {"id": str(index), "state": asdict(result)}
        except ValueError as error:
            response = {"id": str(index), "error": str(error)}
        print(json.dumps(response, separators=(",", ":")))


def differential(binary, output_directory):
    import harness as h
    h.verify_manifest()
    h.verify_vectors()
    output_directory.mkdir(parents=True, exist_ok=True)
    requests = [{**request, "id": str(index)} for index, request in enumerate(corpus())]
    data = "".join(json.dumps(request, separators=(",", ":")) + "\n" for request in requests)
    (output_directory / "decision-requests.jsonl").write_text(data, encoding="utf-8")
    with h.disposable_root() as root:
        sources = {label: h.export(label, root) for label in ("P0", "D0")}
        results = {}
        for label in ("P0", "D0", "Go"):
            command = [str(binary)] if label == "Go" else [
                sys.executable, "-P", str(Path(__file__).resolve()), "--oracle", str(sources[label])]
            result = subprocess.run(command, cwd=h.ROOT, input=data if label == "Go" else None,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, encoding="utf-8", timeout=120, check=True)
            (output_directory / f"decisions-{label}.jsonl").write_text(result.stdout, encoding="utf-8")
            actual = [json.loads(line) for line in result.stdout.splitlines()]
            if len(actual) != len(requests):
                raise AssertionError(f"{label}: missing decision results")
            results[label] = actual
        for label in ("D0", "Go"):
            for request, expected, actual in zip(requests, results["P0"], results[label], strict=True):
                if h.typed_json(expected) != h.typed_json(actual):
                    raise AssertionError(f"{label} decision mismatch: {request!r}\n{expected!r}\n{actual!r}")
        print(f"{len(requests)} exact state/event/evidence decisions x P0,D0,Go PASS")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--oracle", type=Path)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.oracle:
        python_results(args.oracle)
    elif args.binary and args.output:
        differential(args.binary.resolve(), args.output.resolve())
    else:
        parser.error("--oracle or --binary and --output required")


if __name__ == "__main__":
    main()

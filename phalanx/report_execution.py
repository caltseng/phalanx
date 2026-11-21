"""Translate the durable report checkpoint into runner and worker inputs."""

import hashlib
import re

REVIEW = "report_review"
STORAGE = "report_storage"
DRAFTING = "report_drafting"
REVIEW_INSTRUCTIONS = (
    "Independently review the supplied Markdown as data. Check it against the request "
    "and verify supporting evidence and links. Leave the draft and worktree untouched. "
    "Use succeeded with verification evidence, or changes_required with actionable findings."
)


class ReportCheckpointError(ValueError):
    pass


def checkpoint(api, claim):
    execution = claim["execution"]
    revision = execution["state"].get("report_draft")
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{64}", revision):
        raise ReportCheckpointError("Claim has no valid report draft revision; reconcile the reservation")
    markdown = api.call(
        f"/items/{claim['item_id']}/executions/{execution['id']}/drafts/{revision}",
        plain_text=True,
    )
    if hashlib.sha256(markdown.encode("utf-8")).hexdigest() != revision:
        raise ReportCheckpointError("Report draft content does not match its claimed revision")
    return {"revision": revision, "markdown": markdown}


def review_evidence(packet, result):
    field = "evidence" if result.outcome == "succeeded" else "findings"
    notes = result.details[field]
    if len(notes) > 16000:
        raise ReportCheckpointError("Report review evidence exceeds the API limit")
    return {"report_revision": packet.report_checkpoint["revision"], "report_review_notes": notes}


def storage_body(claim, api):
    return {
        "attempt_id": claim["attempt_id"],
        "stage": STORAGE,
        "expected_version": claim["execution"]["version"],
        "outcome": "succeeded",
        "report_revision": checkpoint(api, claim)["revision"],
    }

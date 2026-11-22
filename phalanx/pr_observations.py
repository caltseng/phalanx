"""Server handlers for trusted runner attestations of GitHub PR lifecycle stages."""

import json
import re

from . import intake, validation, waits
from .pr_models import GITHUB_REMOTE, IndependentReview, Publication, Validation, VerifiedMerge
from .transitions import (
    Evidence, Event, Issue, Outcome, PullRequest, Revision, Stage, Status, Target, item_branch,
)


def latest_observation(db, execution_id, stage):
    row = db.execute(
        """SELECT details_json FROM transition_history
           WHERE execution_id=? AND json_extract(details_json, '$.stage')=?
             AND json_type(details_json, '$.pr_url')='text'
           ORDER BY version DESC LIMIT 1""",
        (execution_id, stage),
    ).fetchone()
    return json.loads(row["details_json"]) if row else None


def reviewed_revision(db, execution_id, url, expected, revision):
    review = latest_observation(db, execution_id, Stage.ADVERSARIAL_REVIEW)
    if (review and review.get("outcome") == Outcome.SUCCEEDED
            and review["pr_url"] == url and review["expected"] == expected.model_dump()
            and review["pull"]["head_sha"] == revision.head):
        return revision
    return None


def check_publication_claim(db, item_id, execution_id, body, lease_digest):
    try:
        _, state = intake.check_current(db, item_id, execution_id, body.expected_version)
        attempt = intake.check_attempt(db, execution_id, state, body, lease_digest)
    except (intake.Conflict, intake.Missing):
        return {"current": False}
    if (
        attempt["runner_id"] != body.runner_id or state.status != Status.RUNNING
        or state.stopping is not None or state.primary_pr is not None
        or not re.fullmatch(r"[0-9a-f]{40}", state.candidate or "")
    ):
        return {"current": False}

    item = intake.item_row(db, item_id)
    remote, = db.execute("SELECT remote FROM repositories WHERE id=?",
                         (item["repository_id"],)).fetchone()
    match = GITHUB_REMOTE.fullmatch(remote)
    if match is None:
        raise intake.Conflict("Draft publication requires a registered github.com remote")
    return {
        "current": True,
        "candidate": state.candidate,
        "head_ref": item_branch(item_id),
        "target_branch": json.loads(item["request_json"])["target_branch"],
        "repository": match[1].lower(),
        "remote": remote,
    }


def check_published_validation_claim(db, item_id, execution_id, body, lease_digest):
    try:
        _, state = intake.check_current(db, item_id, execution_id, body.expected_version)
        attempt = intake.check_attempt(db, execution_id, state, body, lease_digest)
    except (intake.Conflict, intake.Missing):
        return {"current": False}
    if (attempt["runner_id"] != body.runner_id or state.status != Status.RUNNING
            or state.stopping is not None):
        return {"current": False}
    promotion = latest_observation(db, execution_id, Stage.PR_PROMOTION)
    pull = None
    if (promotion and promotion["outcome"] == Outcome.SUCCEEDED
            and promotion["pr_url"] == state.primary_pr
            and promotion["pull"]["head_sha"] == state.candidate):
        pull = promotion["pull"]
    return {"current": True, "pr_url": state.primary_pr, "promotion": pull}


def _check_refs(db, item_id, execution_id, body, lease_digest):
    row, state = intake.check_current(db, item_id, execution_id, body.expected_version)
    intake.check_attempt(db, execution_id, state, body, lease_digest)
    item = intake.item_row(db, item_id)
    remote, = db.execute("SELECT remote FROM repositories WHERE id=?",
                         (item["repository_id"],)).fetchone()
    match = GITHUB_REMOTE.fullmatch(remote)
    pull = body.pull
    if match is None:
        raise intake.Conflict("PR observations require a registered github.com remote")
    repository = match[1].lower()
    if pull is not None and (repository != pull.base_repository.lower()
                             or repository != pull.head_repository.lower()):
        raise intake.Conflict("PR repositories must match the registered GitHub repository")

    expected = body.expected
    if expected.head_ref != item_branch(item_id):
        raise intake.Conflict("Expected head branch must match the deterministic item branch")
    target_branch = json.loads(item["request_json"])["target_branch"]
    if target_branch is not None and expected.base_ref != target_branch:
        raise intake.Conflict("Expected base branch must match the work item target")
    return row, state, repository


def submit_blocker(db, item_id, execution_id, body, lease_digest):
    """Route verified external movement found before the first draft is recorded."""
    row, state, repository = _check_refs(db, item_id, execution_id, body, lease_digest)
    if state.primary_pr is not None:
        raise intake.Conflict("A primary PR is already recorded")
    expected, pull = body.expected, body.pull
    details = body.model_dump(mode="json")
    details["outcome"] = Outcome.CHANGES_REQUIRED
    if pull is None:
        if body.observed_base_sha == expected.base_sha:
            raise intake.Conflict("Observed base must differ from the expected base revision")
        evidence = Evidence(issue=Issue.BASE_CHANGED_BEFORE_PUBLICATION)
    else:
        if pull.head_ref != expected.head_ref:
            raise intake.Conflict("Blocking PR must use the deterministic item branch")
        if (pull.head_sha == state.candidate and pull.base_ref == expected.base_ref
                and pull.base_sha == expected.base_sha and pull.state == "open"
                and pull.draft and not pull.merged):
            raise intake.Conflict("Record a matching draft as the published PR instead")
        url = f"https://github.com/{repository}/pull/{pull.number}"
        details["pr_url"] = url
        evidence = Evidence(
            issue=Issue.PR_BLOCKER, matching_publication_pr=url,
            pr=PullRequest(url, Revision(pull.head_sha, pull.base_sha), draft=pull.draft,
                           closed=pull.state == "closed"),
        )
    return intake.apply(db, row, state, Event.RESULT, details=details, attempt_id=body.attempt_id,
                        outcome=Outcome.CHANGES_REQUIRED, evidence=evidence)


def submit(db, item_id, execution_id, body, lease_digest):
    row, state, repository = _check_refs(db, item_id, execution_id, body, lease_digest)
    pull, expected = body.pull, body.expected
    if (pull.head_ref != expected.head_ref or pull.base_ref != expected.base_ref
            or pull.head_sha != state.candidate):
        raise intake.Conflict("Observed PR must match the expected refs and candidate head")
    url = f"https://github.com/{repository}/pull/{pull.number}"
    if state.primary_pr != url and (state.primary_pr or not isinstance(body, Publication)):
        raise intake.Conflict("Observation must match the recorded primary PR")
    if not isinstance(body, Publication):
        publication = latest_observation(db, execution_id, Stage.PR_CREATION)
        if not publication or publication["expected"]["base_ref"] != expected.base_ref:
            raise intake.Conflict("Expected base branch must match the published primary PR")

    revision = Revision(pull.head_sha, expected.base_sha)
    details = body.model_dump(mode="json")
    details["pr_url"] = url
    if isinstance(body, VerifiedMerge):
        ready = latest_observation(db, execution_id, Stage.READY_FOR_CHECK_IN)
        intent = ready.get("merge_intent") if ready else None
        if (not intent or intent["operation_id"] != body.operation_id
                or ready["pr_url"] != url or ready["expected"] != expected.model_dump()
                or ready["pull"]["head_sha"] != revision.head):
            raise intake.Conflict("Verified merge must match the recorded authorized merge intent")
        # The base may have advanced before the merge; the candidate must be the merged head parent.
        if body.merge_parent_shas[1] != revision.head:
            raise intake.Conflict("The merge commit's head parent must be the authorized candidate head")
        pr = PullRequest(url, revision, merge_commit=pull.merge_commit, closed=True)
        return intake.apply(db, row, state, Event.MERGE_VERIFIED, details=details,
                            attempt_id=body.attempt_id, evidence=Evidence(pr=pr))

    if pull.base_sha != expected.base_sha or pull.state != "open" or pull.merged:
        raise intake.Conflict("Expected an open, unmerged PR at the observed base revision")
    pr = PullRequest(url, revision, draft=pull.draft)
    outcome = Outcome.SUCCEEDED
    if isinstance(body, Publication):
        if pull.number in body.duplicate_numbers or len(set(body.duplicate_numbers)) != len(body.duplicate_numbers):
            raise intake.Conflict("Duplicate PR numbers must be distinct and exclude the primary PR")
        evidence = Evidence(pr=pr)
    elif isinstance(body, IndependentReview):
        check_review_assignment(db, execution_id, expected, pull)
        if body.outcome == Outcome.CHANGES_REQUIRED:
            outcome = Outcome.CHANGES_REQUIRED
            findings = "\n".join(f"- {finding}" for finding in body.review.findings)
            evidence = Evidence(pr=pr, development_findings=f"{body.review.summary}\n\n{findings}"[:16000])
        else:
            evidence = Evidence(pr=pr, independent_review=revision)
    else:
        if body.stage != Stage.PR_PROMOTION and pull.draft:
            raise intake.Conflict("Expected an open, ready primary PR")
        schedule_recheck = isinstance(body, Validation) and body.schedule_recheck
        if schedule_recheck and body.stage != Stage.PR_VALIDATION:
            raise intake.Conflict("Scheduled ready checks require the published-validation stage")
        gates = evaluate_gates(body, url, revision)
        details["gate_result"] = {"state": gates.state, "reasons": gates.reasons}
        if gates.skipped:
            details["gate_result"].update(skipped=gates.skipped, skip_reason=validation.DRAFT_SKIP_REASON)
        if gates.state in {validation.GateState.SATISFIED, validation.GateState.NOT_CONFIGURED}:
            evidence = gate_evidence(db, execution_id, body, state, pr)
        elif gates.state is validation.GateState.FAILED:
            # Failed checks go back to development, which fixes them and revalidates.
            phase = "Draft" if body.stage == Stage.PR_PROMOTION else "Required"
            findings = f"{phase} checks did not pass on {url}: " + "; ".join(gates.reasons)
            evidence = Evidence(pr=pr, development_findings=findings[:16000])
            outcome = Outcome.CHANGES_REQUIRED
        elif gates.state is validation.GateState.WAITING and schedule_recheck:
            return _wait_for_published_gates(db, row, state, body, gates, details)
        else:
            raise intake.Conflict("PR gates did not pass: " + "; ".join(gates.reasons))
    details["outcome"] = outcome
    return intake.apply(db, row, state, Event.RESULT, details=details,
                        attempt_id=body.attempt_id, outcome=outcome, evidence=evidence)


def submit_rework(db, item_id, execution_id, body, lease_digest):
    """Return routine PR conflicts and feedback to development for revalidation."""
    row, state, repository = _check_refs(db, item_id, execution_id, body, lease_digest)
    pull, expected = body.pull, body.expected
    url = f"https://github.com/{repository}/pull/{pull.number}"
    if state.primary_pr != url:
        raise intake.Conflict("Rework must observe the recorded primary PR")
    if ((pull.head_ref, pull.base_ref, pull.base_sha) != (expected.head_ref, expected.base_ref, expected.base_sha)
            or pull.state != "open" or pull.merged):
        raise intake.Conflict("Rework requires an open, unmerged PR at the expected refs")
    if (pull.head_sha == state.candidate) == (body.issue == "unexpected_head"):
        raise intake.Conflict("Only an unexpected head may differ from the candidate")
    if body.issue == "merge_conflict":
        findings = (f"The PR conflicts with {expected.base_ref} at {expected.base_sha}. Fetch that commit, "
                    "rebase the candidate onto it, resolve the conflicts and rerun the checks.")
    elif body.issue == "unexpected_head":
        findings = (f"The PR branch {expected.head_ref} has commits Phalanx did not publish (head "
                    f"{pull.head_sha}). Fetch the branch, integrate its commits so its head is an ancestor "
                    "of the new candidate, and rerun the checks.")
    else:
        findings = ("Address this GitHub feedback on the PR:\n\n"
                    + "\n\n".join(f"- {text}" for text in body.findings))
    details = body.model_dump(mode="json")
    details.update(pr_url=url, outcome=Outcome.CHANGES_REQUIRED)
    pr = PullRequest(url, Revision(pull.head_sha, expected.base_sha), draft=pull.draft)
    return intake.apply(db, row, state, Event.RESULT, details=details, attempt_id=body.attempt_id,
                        outcome=Outcome.CHANGES_REQUIRED,
                        evidence=Evidence(pr=pr, development_findings=findings[:16000]))


def check_review_assignment(db, execution_id, expected, pull):
    row = db.execute(
        """SELECT details_json FROM transition_history
           WHERE execution_id=? AND json_extract(state_json, '$.stage')=?
             AND json_extract(state_json, '$.status')='queued'
             AND json_type(details_json, '$.gate_result')='object'
           ORDER BY version DESC LIMIT 1""",
        (execution_id, Stage.ADVERSARIAL_REVIEW),
    ).fetchone()
    assignment = json.loads(row["details_json"]) if row else None
    if (not assignment or assignment["expected"] != expected.model_dump()
            or assignment["pull"]["head_sha"] != pull.head_sha
            or assignment["pull"]["number"] != pull.number):
        raise intake.Conflict("Review must match the revision queued by passing PR gates")


def gate_evidence(db, execution_id, body, state, pr):
    revision = pr.revision
    if body.stage == Stage.PR_PROMOTION:
        return Evidence(pr=pr, draft_checks=revision)
    review = reviewed_revision(db, execution_id, pr.url, body.expected, revision)
    if body.stage == Stage.PR_VALIDATION:
        return Evidence(pr=pr, ci_and_previews=revision, independent_review=review)

    needs_intent = (body.stage == Stage.READY_FOR_CHECK_IN and state.target == Target.MERGED
                    and state.ready_revision == revision and review == revision)
    # Without an intent the runner leaves the merge to the owner and the ready PR is delivered.
    if body.merge_intent is not None and not needs_intent:
        raise intake.Conflict("Merge intent requires a merge target and the current ready revision")
    return Evidence(pr=pr, ci_and_previews=revision, independent_review=review,
                    actionable_feedback_cleared=revision, approvals_and_mergeability=revision,
                    merge_authorized=body.merge_intent is not None)


def evaluate_gates(body, url, revision):
    gates = body.gates
    phase = validation.Phase.DRAFT if body.stage == Stage.PR_PROMOTION else validation.Phase.READY
    if (gates.phase != phase or gates.number != body.pull.number
            or Revision(gates.head_sha, gates.base_sha) != revision):
        raise intake.Conflict("Gate evidence must match the phase and exact PR revision")
    required = tuple(validation.RequiredCheck(**check.model_dump()) for check in gates.required)
    if len({(check.kind, check.name, check.app_id) for check in required}) != len(required):
        raise intake.Conflict("Gate policy contains duplicate requirements")
    if any(check.tested_sha not in (revision.head, gates.test_merge_sha)
           for check in gates.observations):
        raise intake.Conflict("Check observations must name the head or correlated test merge SHA")
    observations = [
        validation.PublishedEvidence(url, revision, phase=phase, **check.model_dump())
        for check in gates.observations
    ]
    return validation.evaluate_published_checks(
        validation.PublishedCandidate(url, revision, body.pull.draft, revision.head,
                                      gates.test_merge_sha),
        phase, required, observations,
    )


def _wait_for_published_gates(db, row, state, body, gates, details):
    if state.status != Status.RUNNING or state.stopping is not None:
        raise intake.Conflict("Scheduling a PR recheck requires a running attempt")
    details.update(gate="pr_validation", revision={"head": body.pull.head_sha, "base": body.pull.base_sha})
    return waits.wait_or_ask(
        db, row, state, body.attempt_id, "; ".join(gates.reasons)[:16000],
        body.retry_after_seconds if body.retry_after_seconds is not None
        else validation.CHECK_RECHECK_SECONDS,
        body.wait_limit_seconds if body.wait_limit_seconds is not None
        else validation.CHECK_WAIT_LIMIT_SECONDS,
        details, "Resolve the stalled ready-phase checks, then reply to resume validation.",
    )

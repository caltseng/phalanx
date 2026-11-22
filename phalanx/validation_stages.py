"""Runner-owned validation stages for attended reservations.

Each handler journals its request before sending it, so an interrupted
submission replays the same body and idempotency key.
"""

from dataclasses import asdict, replace
import os
from pathlib import Path

from .attended import RunnerError
from .attended_drafts import github_repository
from .candidate_local import CandidateLocal
from .git_publication import GitPublication, PublicationError
from .github import GitHubClient, GitHubError, UncertainGitHubWrite
from .github_checks import collect_github_checks
from .pr_models import ExpectedRefs, GateObservation, GitHubPull, Reconciliation, Validation
from .pr_notes import PRNotes
from .promotion import DraftChecks, promote
from .published_validation_stage import PublishedValidationAssignment, PublishedValidationRunner
from .transitions import Outcome, Revision, Stage, Target
from .validation import CHECK_RECHECK_SECONDS, CHECK_WAIT_LIMIT_SECONDS, GateState, Phase
from .validation_policy import ValidationPolicyError, load_validation_policy
from .workspaces import WorkspaceError, _item_branch

# GitHub usually reflects a ready mutation or new base within seconds.
_PROVIDER_RETRY_SECONDS = 30


def _check_timing(runner):
    """Return the pending-CI recheck interval and wait limit from the runner config."""
    return (runner.config.get("check_recheck_seconds", CHECK_RECHECK_SECONDS),
            runner.config.get("check_wait_limit_seconds", CHECK_WAIT_LIMIT_SECONDS))


def run(runner, snapshot):
    stage = snapshot["claim"]["execution"]["state"]["stage"]
    submission = snapshot.get("submission")
    if stage == Stage.PR_VALIDATION and submission is None:
        execution = _published_validation(runner, snapshot)
        if execution is not None:
            return {"status": "published_validation_submitted", "execution": execution}
        submission = snapshot["submission"]
    if submission is None:
        handler = {Stage.PRE_PR_VALIDATION: _pre_validation, Stage.PR_PROMOTION: _promotion}.get(
            stage, _disposition)
        submission = _save(runner, snapshot, handler(runner, snapshot))
    claim = snapshot["claim"]
    key = f"{stage}:{claim['attempt_id']}:{claim['execution']['version']}"
    if stage == Stage.PRE_PR_VALIDATION and submission["body"]["outcome"] == Outcome.SUCCEEDED:
        _candidate(runner, snapshot)._inspect()
        _note_bypass(runner, snapshot, submission["body"])
    execution = runner._send_submission(snapshot, submission, key)
    return {"status": f"{stage}_submitted", "execution": execution,
            **({"reason": submission["reason"]} if "reason" in submission else {})}


def _note_bypass(runner, snapshot, body):
    """List bypassed local checks as Known gaps on the PR, or clear gaps the candidate fixed."""
    url = snapshot["claim"]["execution"]["state"].get("primary_pr")
    if not url:
        return
    skipped = [check for check in body.get("local_checks", ()) if check["outcome"] != "passed"]
    notes = PRNotes(runner.root)
    if not skipped:
        notes.clear_gaps(url)
        return
    notes.record(url, body["prevalidated_candidate"], "Pre-validation bypassed", "\n".join(
        f"- Local check {check['name']} {check['outcome']}: {check.get('reason') or 'no reason'} "
        f"(authorized by {check.get('authorization') or 'policy'})" for check in skipped
    ))


def _save(runner, snapshot, submission):
    def save(record, db):
        runner._ensure_reservation(record, snapshot["lease"])
        record.setdefault("submission", submission)
        return record["submission"]

    snapshot["submission"] = runner._transaction(save)
    return snapshot["submission"]


def _event(snapshot):
    claim = snapshot["claim"]
    return {"attempt_id": claim["attempt_id"], "stage": claim["execution"]["state"]["stage"],
            "expected_version": claim["execution"]["version"]}


def _question(snapshot, reason, handoff):
    state = snapshot["claim"]["execution"]["state"]
    body = {**_event(snapshot), "outcome": Outcome.NEEDS_INPUT,
            "question": {"prompt": reason[:16000], "handoff": handoff[:16000]}}
    if state["stage"] == Stage.PRE_PR_VALIDATION:
        body["candidate"] = state["candidate"]
    return {"body": body, "reason": reason}


def _retry(snapshot, reason, delay=None):
    return {"body": {**_event(snapshot), "outcome": Outcome.FAILED, "failure_reason": reason[:16000],
                     "retry_after_seconds": delay or _PROVIDER_RETRY_SECONDS}, "reason": reason}


def _policy(runner, snapshot):
    path = runner.config.get("validation_policy")
    if not isinstance(path, str) or not path:
        raise ValidationPolicyError("Set validation_policy in the runner configuration")
    return load_validation_policy(Path(path), snapshot["claim"]["request"]["repository_id"])


def _candidate(runner, snapshot):
    claim = snapshot["claim"]
    repo = claim["request"]["repository_id"]
    manager = runner._manager(runner.config["repositories"][repo], snapshot["repositories"][repo])
    return CandidateLocal(runner.api, manager, claim, runner_id=snapshot["runner_id"], lease=snapshot["lease"])


def _github(snapshot):
    token = os.environ.get("PHALANX_GITHUB_TOKEN")
    if not token:
        raise RunnerError("Set PHALANX_GITHUB_TOKEN in the runner environment and retry")
    repository = github_repository(snapshot["repositories"][snapshot["claim"]["request"]["repository_id"]])
    if repository is None:
        raise RunnerError("PR validation requires a registered github.com remote")
    owner, name = repository.split("/")
    return GitHubClient(owner, name, token=token)


def _pre_validation(runner, snapshot):
    local = _candidate(runner, snapshot)
    try:
        checks = _policy(runner, snapshot).local_checks
    except ValidationPolicyError as error:
        return _question(snapshot, str(error), "Repair the runner's trusted validation policy, then reply.")
    return {"body": local.pre_validate(checks)}


def _promotion(runner, snapshot):
    claim = snapshot["claim"]
    state = claim["execution"]["state"]
    candidate = state["candidate"]
    url = state.get("primary_pr") or ""
    handoff = f"{url}, candidate {candidate}"
    try:
        draft_checks = _policy(runner, snapshot).draft_checks
    except ValidationPolicyError as error:
        return _question(snapshot, str(error), "Repair the runner's trusted validation policy, then reply.")
    number = url.rsplit("/", 1)[-1]
    if not number.isdigit():
        return _question(snapshot, "Restore the recorded primary PR before promotion.", handoff)
    github = _github(snapshot)
    mapping = runner.config["repositories"][claim["request"]["repository_id"]]
    base_ref = claim["request"].get("target_branch") or mapping["base_branch"]
    local = _candidate(runner, snapshot)
    blocked = []
    changed = []

    def claim_current():
        try:
            local._authorize()
        except RunnerError:
            return False
        return True

    def collect(pull):
        if not draft_checks:
            return DraftChecks(draft_checks)
        checks = collect_github_checks(github, pull, Phase.DRAFT, draft_checks)
        if checks.retry_after_seconds is not None:
            changed.append(checks)
        elif checks.gate.state is GateState.BLOCKED:
            blocked.extend(checks.gate.reasons)
        return DraftChecks(draft_checks, checks.observations,
                           checks.candidate.merge_sha if checks.candidate else None)

    try:
        pull = github.get_pull_request(int(number))
    except GitHubError as error:
        return _retry(snapshot, str(error), error.retry_after_seconds)
    if pull.head_sha != candidate:
        update = _publish_update(snapshot, local, pull, base_ref, _verified_head(runner, snapshot, url))
        if update is not None:
            return update
        try:
            pull = github.get_pull_request(int(number))
        except GitHubError as error:
            return _retry(snapshot, str(error), error.retry_after_seconds)
        if pull.head_sha != candidate:
            return _retry(snapshot, "GitHub has not shown the pushed candidate on the PR yet")
    result = promote(
        github, item_id=claim["item_id"], number=int(number), base_ref=base_ref,
        revision=Revision(candidate, pull.base_sha), collect=collect,
        read_base=lambda: github.get_branch_tip(base_ref), claim_current=claim_current,
    )
    if result.status == "claim_lost":
        raise RunnerError("Promotion claim changed. Reconcile the reservation before continuing")
    if result.status == "provider_pending":
        return _retry(snapshot, result.reason)
    if result.status == "revision_changed":
        observed = result.pull
        if (observed is None or observed.head_sha != candidate or observed.state != "open"
                or observed.merged or observed.head_ref != _item_branch(claim["item_id"])
                or observed.base_ref != base_ref):
            return _question(snapshot, result.reason, handoff)
        # Only the target moved. A later attempt observes the new base revision.
        return _retry(snapshot, "The target branch moved during promotion; observing the new base")
    gates = result.gates
    if changed:
        return _retry(snapshot, "; ".join(changed[-1].gate.reasons), changed[-1].retry_after_seconds)
    if blocked or (result.status == "gates_unresolved" and gates.state is not GateState.FAILED):
        observed = result.pull or pull
        if not blocked and gates.state is GateState.WAITING:
            return _draft_wait(runner, snapshot, ("; ".join(gates.reasons) or "Draft checks are pending")[:16000],
                               observed.base_sha)
        reason = "; ".join(blocked or gates.reasons) or result.reason
        if not blocked and not observed.draft and gates.state is GateState.BLOCKED:
            return _convert_to_draft(runner, snapshot, github, observed, reason, handoff)
        return _question(snapshot, reason, handoff)
    # Verified evidence advances; failed draft checks return the item to development.
    return {"route": "pr-observation", "body": _validation(
        snapshot, result.pull, base_ref, draft_checks, result.checks,
    ), **({"reason": result.reason} if result.status != "verified" else {})}


def _draft_wait(runner, snapshot, reason, base_sha):
    candidate = snapshot["claim"]["execution"]["state"]["candidate"]
    interval, limit = _check_timing(runner)
    return {"route": "external-wait", "reason": reason, "body": {
        **_event(snapshot), "reason": reason, "gate": "draft_checks",
        "revision": {"head": candidate, "base": base_sha},
        "retry_after_seconds": interval, "wait_limit_seconds": limit,
    }}


def _convert_to_draft(runner, snapshot, github, pull, reason, handoff):
    """Return a ready PR to draft so its draft-only checks can run, then wait for them."""
    try:
        github.convert_to_draft(pull.node_id)
    except UncertainGitHubWrite as error:
        return _retry(snapshot, str(error))
    except GitHubError as error:
        if error.retry_after_seconds is not None or error.status in {408, 429}:
            return _retry(snapshot, str(error), error.retry_after_seconds)
        return _question(snapshot, f"{reason}. GitHub refused to convert the PR back to draft ({error}). "
                         "Convert it to draft or remove its draft-only checks from the runner's "
                         "validation policy, then reply.", handoff)
    candidate = snapshot["claim"]["execution"]["state"]["candidate"]
    PRNotes(runner.root).comment(
        snapshot["claim"]["execution"]["state"]["primary_pr"], f"converted-to-draft:{candidate}",
        f"Phalanx converted this PR back to draft at `{candidate}` so its draft-only checks can run: "
        f"{reason}. It will mark the PR ready again once they pass.",
    )
    return _draft_wait(runner, snapshot, f"Converted the PR back to draft; waiting for draft checks: {reason}"[:16000],
                       pull.base_sha)


def _verified_head(runner, snapshot, url):
    """Return the latest primary PR head the server verified for this execution."""
    claim = snapshot["claim"]
    timeline = runner.api.call(f"/items/{claim['item_id']}/executions/{claim['execution']['id']}/timeline")
    for entry in reversed(timeline):
        details = entry.get("details") or {}
        pull = details.get("pull")
        if details.get("pr_url") == url and isinstance(pull, dict) and pull.get("head_sha"):
            return pull["head_sha"]
    return None


def _publish_update(snapshot, local, pull, base_ref, verified_head):
    """Replace the primary PR's head with the reviewed candidate, or explain why not.

    Only a head the server last verified for this PR is replaced, leased on that
    head so a concurrent push is never overwritten. A rebased candidate therefore
    publishes like a fast-forward. Returns None once the remote branch holds the
    candidate.
    """
    claim = snapshot["claim"]
    candidate = claim["execution"]["state"]["candidate"]
    handoff = f"{claim['execution']['state'].get('primary_pr')}, candidate {candidate}"
    if (pull.state != "open" or pull.merged or pull.head_ref != _item_branch(claim["item_id"])
            or pull.base_ref != base_ref):
        return _question(snapshot, "The primary PR is no longer an open PR from the item branch "
                         "to the target branch; reconcile it before publishing the reviewed candidate.",
                         handoff)
    try:
        workspace = local._inspect()
        git = GitPublication(workspace.path, snapshot["repositories"][claim["request"]["repository_id"]],
                             claim["item_id"])
        if git.read_head(authorize=local._authorize) == candidate:
            return None
        if pull.head_sha != verified_head:
            return _question(snapshot, f"The primary PR head {pull.head_sha} is not the head Phalanx last "
                             "verified; reconcile it before publishing the reviewed candidate.", handoff)
        outcome = git.publish(candidate, expected=pull.head_sha, authorize=local._authorize, rewrite=True)
    except (PublicationError, WorkspaceError) as error:
        return _retry(snapshot, f"Could not publish the reviewed candidate: {error}")
    if outcome.status == "uncertain":
        return _retry(snapshot, outcome.reason)
    if outcome.status == "conflict":
        return _question(snapshot, f"Could not publish the reviewed candidate: {outcome.reason}", handoff)
    return None


def _disposition(runner, snapshot):
    """Attest current ready gates for reconciliation and the ready-only check-in."""
    claim = snapshot["claim"]
    state = claim["execution"]["state"]
    candidate, url = state["candidate"], state.get("primary_pr") or ""
    handoff = f"{url}, candidate {candidate}"
    if state["stage"] == Stage.READY_FOR_CHECK_IN and state["target"] != Target.READY:
        return _question(snapshot, "Merge check-in is not automated yet. Merge the ready PR or change "
                         "the target to ready_for_check_in, then reply.", handoff)
    try:
        policy = _policy(runner, snapshot).ready
    except ValidationPolicyError as error:
        return _question(snapshot, str(error), "Repair the runner's trusted validation policy, then reply.")
    number = url.rsplit("/", 1)[-1]
    if policy.required is None or not number.isdigit():
        return _question(snapshot, "Configure the complete ready-phase policy and restore the primary PR.",
                         handoff)
    github = _github(snapshot)
    mapping = runner.config["repositories"][claim["request"]["repository_id"]]
    base_ref = claim["request"].get("target_branch") or mapping["base_branch"]
    try:
        pull = github.get_pull_request(int(number))
    except GitHubError as error:
        return _retry(snapshot, str(error), error.retry_after_seconds)
    if (pull.url.casefold() != url.casefold() or pull.state != "open" or pull.merged or pull.draft
            or (pull.head_ref, pull.base_ref, pull.head_sha)
            != (_item_branch(claim["item_id"]), base_ref, candidate)):
        return _question(snapshot, "Reconcile the primary PR with the recorded branch, target and candidate.",
                         handoff)
    if pull.mergeable is None:
        return _retry(snapshot, "GitHub is still computing mergeability")
    if not pull.mergeable:
        if _conflicted_before(runner, snapshot, pull.base_sha):
            return _question(snapshot, f"A development attempt could not resolve the merge conflict with "
                             f"{base_ref} at {pull.base_sha}. Resolve it on the primary PR, then reply.", handoff)
        return {"route": "pr-observation", "reason": f"The PR conflicts with {base_ref}; returning it to "
                "development to rebase and revalidate", "body": Reconciliation(
                    **_event(snapshot), **_refs(snapshot, pull, base_ref), merge_conflict=True
                ).model_dump(mode="json")}
    checks = collect_github_checks(github, pull, Phase.READY, policy.required, read_preview=policy.read_preview)
    if checks.retry_after_seconds is not None:
        return _retry(snapshot, "; ".join(checks.gate.reasons), checks.retry_after_seconds)
    reason = "; ".join(checks.gate.reasons) or "Ready checks are not satisfied"
    if checks.gate.state is GateState.WAITING:
        interval, limit = _check_timing(runner)
        return {"route": "external-wait", "reason": reason[:16000], "body": {
            **_event(snapshot), "reason": reason[:16000], "gate": "ready_checks",
            "revision": {"head": candidate, "base": pull.base_sha},
            "retry_after_seconds": interval, "wait_limit_seconds": limit,
        }}
    if checks.gate.state not in {GateState.SATISFIED, GateState.NOT_CONFIGURED} or checks.candidate is None:
        return _question(snapshot, reason, handoff)
    return {"route": "pr-observation", "body": _observation(
        Reconciliation, snapshot, pull, base_ref, policy.required, checks.observations,
        checks.candidate.merge_sha, Phase.READY,
        actionable_feedback_cleared=True, approvals_and_mergeability=True,
    )}


def _validation(snapshot, pull, base_ref, required, checks):
    return _observation(Validation, snapshot, pull, base_ref, required, checks.observations,
                        checks.merge_sha, Phase.DRAFT)


def _refs(snapshot, pull, base_ref):
    return {
        "expected": ExpectedRefs(head_ref=_item_branch(snapshot["claim"]["item_id"]), base_ref=base_ref,
                                 base_sha=pull.base_sha),
        "pull": GitHubPull.model_validate({field: getattr(pull, field) for field in GitHubPull.model_fields}),
    }


def _conflicted_before(runner, snapshot, base_sha):
    """Whether a development attempt already tried to resolve a conflict with this base."""
    claim = snapshot["claim"]
    timeline = runner.api.call(f"/items/{claim['item_id']}/executions/{claim['execution']['id']}/timeline")
    return any((entry.get("details") or {}).get("merge_conflict") is True
               and entry["details"]["expected"]["base_sha"] == base_sha for entry in timeline)


def _observation(model, snapshot, pull, base_ref, required, observations, merge_sha, phase, **fields):
    required = required or ()
    return model(
        **_event(snapshot), **fields, **_refs(snapshot, pull, base_ref),
        gates=GateObservation(
            phase=phase, number=pull.number, head_sha=pull.head_sha, base_sha=pull.base_sha,
            test_merge_sha=merge_sha, required=[asdict(check) for check in required],
            observations=[
                {"name": evidence.name, "kind": evidence.kind, "tested_sha": evidence.tested_sha,
                 "outcome": evidence.outcome, "app_id": evidence.app_id}
                for evidence in observations
                if any(check.kind == evidence.kind and check.name == evidence.name
                       and (check.app_id is None or check.app_id == evidence.app_id) for check in required)
            ],
        ),
    ).model_dump(mode="json")


def _published_validation(runner, snapshot):
    claim = snapshot["claim"]
    try:
        policy = _policy(runner, snapshot).ready
    except ValidationPolicyError as error:
        _save(runner, snapshot, _question(
            snapshot, str(error), "Repair the runner's trusted validation policy, then reply."))
        return None
    interval, limit = _check_timing(runner)
    policy = replace(policy, recheck_seconds=interval, wait_limit_seconds=limit)
    execution = claim["execution"]
    assignment = PublishedValidationAssignment(
        claim["item_id"], execution["id"], snapshot["runner_id"], claim["attempt_id"],
        execution["version"], snapshot["lease"],
    )
    result = PublishedValidationRunner(runner.root / "publication", runner.api, _github(snapshot)).run(
        assignment, policy,
    )
    if result is None:
        raise RunnerError("Published validation claim changed. Reconcile the reservation before continuing")

    def finish(record, db):
        if record.get("lease") == snapshot["lease"]:
            record.clear()

    runner._transaction(finish)
    return result

"""Runner-owned validation stages for attended reservations.

Each handler journals its request before sending it, so an interrupted
submission replays the same body and idempotency key.
"""

from dataclasses import asdict, replace
import os
from pathlib import Path

from .attended import RunnerError
from .attended_drafts import github_repository
from .candidate_local import LOCAL_CHECK_CAPACITY, CandidateLocal
from .check_in import MergeTarget, check_in, merge_evidence, observe_merge
from .git_publication import GitPublication, PublicationError
from .github import GitHubClient, GitHubError
from .github_checks import collect_github_checks
from .pr_models import (ExpectedRefs, ExternalMerge, GateObservation, GitHubPull, MergeIntent, Reconciliation,
                        Revised, Rework, Validation, VerifiedMerge)
from .pr_notes import PRNotes
from .promotion import DraftChecks, promote
from .published_validation_stage import PublishedValidationAssignment, PublishedValidationRunner
from .transitions import Outcome, Revision, Stage, Target
from .validation import CHECK_RECHECK_SECONDS, CHECK_WAIT_LIMIT_SECONDS, DRAFT_SKIP_REASON, GateState, Phase
from .validation_policy import ValidationPolicyError, load_validation_policy
from .workspaces import WorkspaceError, _item_branch, any_file_lock

# GitHub usually reflects a ready mutation or new base within seconds.
_PROVIDER_RETRY_SECONDS = 30
# Waiting for a local check slot starts no check, so it never counts against a check timeout.
_LOCAL_CHECK_CAPACITY_RETRY_SECONDS = 30


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
        handler = {Stage.PRE_PR_VALIDATION: _pre_validation, Stage.PR_PROMOTION: _promotion,
                   Stage.CHECK_IN: _check_in}.get(stage, _disposition)
        submission = _save(runner, snapshot, handler(runner, snapshot))
    claim = snapshot["claim"]
    key = f"{stage}:{claim['attempt_id']}:{claim['execution']['version']}"
    if stage == Stage.PRE_PR_VALIDATION and submission["body"].get("outcome") == Outcome.SUCCEEDED:
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
    if not any(check.disposition is None for check in checks or ()):
        return {"body": local.pre_validate(checks)}
    # Concurrent suites on one host starve each other into timeouts, so cap local check runs.
    capacity = runner.config.get("local_check_capacity", LOCAL_CHECK_CAPACITY)
    with any_file_lock([runner.root / f"local-check-{index}.lock" for index in range(capacity)]) as slot:
        if slot is not None:
            return {"body": local.pre_validate(checks)}
    candidate = snapshot["claim"]["execution"]["state"]["candidate"]
    reason = (f"Waiting for local check capacity: all {capacity} local check "
              f"slot{'s' if capacity != 1 else ''} on this runner host are busy")
    return {"route": "external-wait", "reason": reason, "body": {
        **_event(snapshot), "reason": reason, "gate": "local_check_capacity",
        "revision": {"head": candidate, "base": candidate},
        "retry_after_seconds": _LOCAL_CHECK_CAPACITY_RETRY_SECONDS,
    }}


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
    if pull.merged:
        return _external_merge(github, snapshot, pull, base_ref)
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
    if pull.head_sha == candidate and pull.mergeable is False:
        return _conflict(github, snapshot, pull, base_ref)
    if pull.draft and not _may_mark_ready(runner, snapshot, url):
        return _question(snapshot, _REPROMOTE_PROMPT, handoff)
    result = promote(
        github, item_id=claim["item_id"], number=int(number), base_ref=base_ref,
        revision=Revision(candidate, pull.base_sha), collect=collect, claim_current=claim_current,
    )
    if result.status == "claim_lost":
        raise RunnerError("Promotion claim changed. Reconcile the reservation before continuing")
    if result.status == "provider_pending":
        return _retry(snapshot, result.reason)
    if result.status == "revision_changed":
        observed = result.pull
        if observed is not None and observed.merged:
            return _external_merge(github, snapshot, observed, base_ref)
        if (observed is None or observed.state != "open"
                or observed.merged or observed.head_ref != _item_branch(claim["item_id"])
                or observed.base_ref != base_ref):
            return _question(snapshot, result.reason, handoff)
        if observed.head_sha == candidate and observed.mergeable is False:
            return _conflict(github, snapshot, observed, base_ref)
        # A moved head is reobserved, and a foreign push is integrated by development.
        return _retry(snapshot, f"The primary PR changed during promotion; reobserving it. Observed head "
                                f"{observed.head_sha}, base {observed.base_sha}; expected head {candidate}, "
                                f"base {pull.base_sha}")
    gates = result.gates
    if changed:
        return _retry(snapshot, "; ".join(changed[-1].gate.reasons), changed[-1].retry_after_seconds)
    if blocked or (result.status == "gates_unresolved" and gates.state is not GateState.FAILED):
        observed = result.pull or pull
        if not blocked and gates.state is GateState.WAITING:
            return _draft_wait(runner, snapshot, ("; ".join(gates.reasons) or "Draft checks are pending")[:16000],
                               observed.base_sha)
        reason = "; ".join(blocked or gates.reasons) or result.reason
        return _question(snapshot, reason, handoff)
    if result.status == "verified" and gates.skipped:
        PRNotes(runner.root).comment(
            url, f"draft-checks-skipped:{candidate}",
            f"Phalanx skipped draft-only checks at `{candidate}` ({DRAFT_SKIP_REASON}): "
            f"{', '.join(gates.skipped)}.",
        )
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


_REPROMOTE_PROMPT = ("This PR was converted back to draft after it was marked ready, and Phalanx never "
                     "marks such a PR ready on its own. Mark it ready yourself, or reply as the owner to let "
                     "Phalanx mark it ready once its draft-only checks pass.")


def _may_mark_ready(runner, snapshot, url):
    """Whether Phalanx may mark this draft ready: it was never ready, or the owner allowed it since."""
    claim = snapshot["claim"]
    timeline = runner.api.call(f"/items/{claim['item_id']}/executions/{claim['execution']['id']}/timeline")
    allowed = True
    for entry in timeline:
        details = entry.get("details") or {}
        pull = details.get("pull")
        if details.get("pr_url") == url and isinstance(pull, dict) and pull.get("draft") is False:
            allowed = False
        elif (entry.get("event") == "reply" and details.get("answered_by", "owner") == "owner"
              and (details.get("question") or {}).get("prompt") == _REPROMOTE_PROMPT):
            allowed = True
    return allowed


def _verified_head(runner, snapshot, url):
    """Return the latest primary PR head the server verified for this execution."""
    claim = snapshot["claim"]
    timeline = runner.api.call(f"/items/{claim['item_id']}/executions/{claim['execution']['id']}/timeline")
    for entry in reversed(timeline):
        details = entry.get("details") or {}
        pull = details.get("pull")
        # Skip foreign heads Phalanx observed but never verified as its own.
        if details.get("issue") == "unexpected_head":
            continue
        if details.get("candidate") and details["candidate"] != (pull or {}).get("head_sha"):
            continue
        if details.get("pr_url") == url and isinstance(pull, dict) and pull.get("head_sha"):
            return pull["head_sha"]
    return None


def publish_revision(runner, snapshot, candidate):
    """Push a pr_revision commit to the open PR and return its Revised body.

    Lag and lost leases raise RunnerError so the Captain retries the submit with
    the reservation kept; a FAILED result would relaunch the worker. A push that
    loses to a foreign one is recorded, and reconciliation sends it back.
    """
    claim = snapshot["claim"]
    state = claim["execution"]["state"]
    url = state.get("primary_pr") or ""
    mapping = runner.config["repositories"][claim["request"]["repository_id"]]
    base_ref = claim["request"].get("target_branch") or mapping["base_branch"]
    local = _candidate(runner, snapshot)
    github = _github(snapshot)
    try:
        pull = github.get_pull_request(int(url.rsplit("/", 1)[-1]))
        if pull.state == "open" and pull.head_sha != candidate:
            if pull.head_ref != _item_branch(claim["item_id"]) or pull.base_ref != base_ref:
                raise RunnerError("The primary PR no longer targets the item branch and target branch")
            git = GitPublication(local.workspace.inspect(claim["item_id"]).path,
                                 snapshot["repositories"][claim["request"]["repository_id"]], claim["item_id"])
            remote = git.read_head(authorize=local._authorize)
            if remote != candidate:
                if remote != pull.head_sha:
                    raise RunnerError("GitHub has not shown the latest push to the PR branch yet; retry")
                rewrite = pull.head_sha == _verified_head(runner, snapshot, url)
                outcome = git.publish(candidate, expected=pull.head_sha, authorize=local._authorize,
                                      rewrite=rewrite)
                if outcome.status != "success" and (outcome.status != "conflict" or rewrite):
                    raise RunnerError(f"Could not push the revision: {outcome.reason}; retry")
                foreign = outcome.status == "conflict"
            else:
                foreign = False
            pull = github.get_pull_request(pull.number)
            if not foreign and pull.head_sha != candidate:
                raise RunnerError("GitHub has not shown the pushed revision on the PR yet; retry")
    except (GitHubError, PublicationError, WorkspaceError, ValueError) as error:
        raise RunnerError(f"Could not publish the revision: {error}; retry") from None
    return Revised(**_event(snapshot), **_refs(snapshot, pull, base_ref), candidate=candidate).model_dump(mode="json")


def _publish_update(snapshot, local, pull, base_ref, verified_head):
    """Replace the primary PR's head with the reviewed candidate, or explain why not.

    Only a head the server last verified for this PR is replaced, leased on that
    head so a concurrent push is never overwritten. A rebased candidate therefore
    publishes like a fast-forward. Any other head is only fast-forwarded; when
    that fails, development integrates the foreign commits and revalidates.
    Returns None once the remote branch holds the candidate.
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
        remote = git.read_head(authorize=local._authorize)
        if remote == candidate:
            return None
        if remote != pull.head_sha:
            return _retry(snapshot, "GitHub has not shown the latest push to the PR branch yet")
        rewrite = pull.head_sha == verified_head
        outcome = git.publish(candidate, expected=pull.head_sha, authorize=local._authorize, rewrite=rewrite)
    except (PublicationError, WorkspaceError) as error:
        return _retry(snapshot, f"Could not publish the reviewed candidate: {error}")
    if outcome.status == "conflict" and not rewrite:
        return _rework(snapshot, pull, base_ref, "unexpected_head")
    if outcome.status != "success":
        # A lost lease means the branch moved again; the retry reobserves it.
        return _retry(snapshot, outcome.reason)
    return None


def _disposition(runner, snapshot):
    """Observe the ready PR's gates for reconciliation, with a merge intent when authorized."""
    claim = snapshot["claim"]
    state = claim["execution"]["state"]
    candidate, url = state["candidate"], state.get("primary_pr") or ""
    handoff = f"{url}, candidate {candidate}"
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
    if pull.merged:
        return _external_merge(github, snapshot, pull, base_ref)
    if (pull.url.casefold() != url.casefold() or pull.state != "open" or pull.merged or pull.draft
            or (pull.head_ref, pull.base_ref) != (_item_branch(claim["item_id"]), base_ref)):
        return _question(snapshot, "Reconcile the primary PR with the recorded branch and target.", handoff)
    if pull.head_sha != candidate:
        return _rework(snapshot, pull, base_ref, "unexpected_head")
    if pull.mergeable is None:
        return _retry(snapshot, "GitHub is still computing mergeability")
    if not pull.mergeable:
        return _conflict(github, snapshot, pull, base_ref)
    try:
        feedback = github.get_actionable_feedback(pull.number)
    except GitHubError as error:
        return _retry(snapshot, str(error), error.retry_after_seconds)
    handed_off = _handed_off_feedback(runner, snapshot)
    if feedback := [(node, text) for node, text in feedback if node not in handed_off][:100]:
        return _rework(snapshot, pull, base_ref, "feedback", feedback)
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
    failed = checks.gate.state is GateState.FAILED
    if (checks.gate.state not in {GateState.SATISFIED, GateState.NOT_CONFIGURED} and not failed
            or checks.candidate is None):
        return _question(snapshot, reason, handoff)
    # Failed checks are observed as-is; the server sends them to pr_revision as findings.
    return {"route": "pr-observation", **({"reason": reason[:16000]} if failed else {}), "body": _observation(
        Reconciliation, snapshot, pull, base_ref, policy.required, checks.observations,
        checks.candidate.merge_sha, Phase.READY,
        merge_intent=None if failed else _merge_intent(runner, snapshot, base_ref),
    )}


def _merge_intent(runner, snapshot, base_ref):
    """Authorize one merge of the observed ready revision for a checked_in target.

    The item's target is the authorization; GitHub's rules for the base branch
    (required reviews and checks) still gate the merge, and Phalanx adds none.
    The server uses the intent only once every gate is current for this
    revision. With auto_merge off the runner sends no intent, so the ready PR is
    delivered and the owner merges it.
    """
    state = snapshot["claim"]["execution"]["state"]
    if state["target"] != Target.MERGED or not runner.config.get("auto_merge", True):
        return None
    return MergeIntent(operation_id=f"merge-{snapshot['claim']['attempt_id']}",
                       authorization=f"The work item targets {Target.MERGED}; GitHub's rules for "
                                     f"{base_ref} gate the merge")


def _check_in(runner, snapshot):
    """Merge the ready PR under the claim, or verify a merge an earlier attempt sent."""
    claim = snapshot["claim"]
    state = claim["execution"]["state"]
    candidate, url, ready = state["candidate"], state.get("primary_pr") or "", state.get("ready_revision")
    handoff = f"{url}, candidate {candidate}"
    intent = _recorded_intent(runner, snapshot, url)
    number = url.rsplit("/", 1)[-1]
    if intent is None or not number.isdigit() or not ready or ready["head"] != candidate:
        return _question(snapshot, "Check-in needs the recorded merge intent for the ready revision. "
                         "Return the item to reconciliation, then reply.", handoff)
    mapping = runner.config["repositories"][claim["request"]["repository_id"]]
    base_ref = claim["request"].get("target_branch") or mapping["base_branch"]
    target = MergeTarget(int(number), url, _item_branch(claim["item_id"]), base_ref,
                         Revision(ready["head"], ready["base"]))
    github = _github(snapshot)
    result = check_in(github, target, assert_claim=_candidate(runner, snapshot)._authorize)
    pull = result.pull
    if result.status == "merged":
        return {"route": "pr-observation", "body": merge_evidence(
            VerifiedMerge, _event(snapshot), result, ready["base"], operation_id=intent["operation_id"],
        )}
    if result.status == "pending":
        return _retry(snapshot, result.reason, result.retry_after_seconds)
    if result.status == "moved":
        return _rework(snapshot, pull, base_ref, "unexpected_head")
    if result.status == "conflict":
        return _conflict(github, snapshot, pull, base_ref)
    if result.status == "refused":
        interval, limit = _check_timing(runner)
        return {"route": "external-wait", "reason": result.reason[:16000], "body": {
            **_event(snapshot), "reason": result.reason[:16000], "gate": "merge",
            "revision": {"head": candidate, "base": pull.base_sha},
            "retry_after_seconds": interval, "wait_limit_seconds": limit,
        }}
    return _question(snapshot, result.reason, handoff)


def _external_merge(github, snapshot, pull, base_ref):
    """Deliver a primary PR the repository merged itself, once GitHub shows it merged the candidate."""
    state = snapshot["claim"]["execution"]["state"]
    candidate, url = state["candidate"], state.get("primary_pr") or ""
    outcome = observe_merge(github, MergeTarget(pull.number, url, _item_branch(snapshot["claim"]["item_id"]),
                                                base_ref, Revision(candidate, pull.base_sha)))
    if outcome.status == "merged":
        return {"route": "pr-merged", "reason": outcome.reason, "body": merge_evidence(
            ExternalMerge, _event(snapshot), outcome, outcome.pull.base_sha)}
    if outcome.status == "pending":
        return _retry(snapshot, outcome.reason, outcome.retry_after_seconds)
    return _question(snapshot, f"{url} was merged outside Phalanx, but not as the validated candidate: "
                     f"{outcome.reason}. Decide how this item should continue, then reply.",
                     f"{url}, candidate {candidate}")


def _recorded_intent(runner, snapshot, url):
    """Return the merge intent the server recorded with the latest reconciliation."""
    claim = snapshot["claim"]
    timeline = runner.api.call(f"/items/{claim['item_id']}/executions/{claim['execution']['id']}/timeline")
    for entry in reversed(timeline):
        details = entry.get("details") or {}
        if details.get("stage") == Stage.PR_RECONCILIATION and details.get("pr_url") == url:
            return details.get("merge_intent")
    return None


def _validation(snapshot, pull, base_ref, required, checks):
    return _observation(Validation, snapshot, pull, base_ref, required, checks.observations,
                        checks.merge_sha, Phase.DRAFT)


def _refs(snapshot, pull, base_ref):
    return {
        "expected": ExpectedRefs(head_ref=_item_branch(snapshot["claim"]["item_id"]), base_ref=base_ref,
                                 base_sha=pull.base_sha),
        "pull": GitHubPull.model_validate({field: getattr(pull, field) for field in GitHubPull.model_fields}),
    }


def _handed_off_feedback(runner, snapshot):
    """GitHub feedback IDs already handed to a fixing stage for this execution."""
    claim = snapshot["claim"]
    timeline = runner.api.call(f"/items/{claim['item_id']}/executions/{claim['execution']['id']}/timeline")
    return {node for entry in timeline for node in (entry.get("details") or {}).get("feedback_ids") or ()}


def _conflict(github, snapshot, pull, base_ref):
    """Send a merge conflict to rework against the target branch tip, which pull.base_sha can lag."""
    try:
        tip = github.get_branch_tip(base_ref)
    except GitHubError as error:
        return _retry(snapshot, str(error), error.retry_after_seconds)
    return _rework(snapshot, pull, base_ref, "merge_conflict", base_tip=tip)


def _rework(snapshot, pull, base_ref, issue, feedback=(), *, base_tip=None):
    reasons = {"merge_conflict": f"The PR conflicts with {base_ref}",
               "unexpected_head": f"The PR head {pull.head_sha} is not the candidate",
               "feedback": "The PR has new actionable GitHub feedback"}
    fixer = "returning it to development" if (
        issue != "merge_conflict" and snapshot["claim"]["execution"]["state"]["stage"] == Stage.PR_PROMOTION
    ) else "sending it to pr_revision"
    return {"route": "pr-rework", "reason": f"{reasons[issue]}; {fixer}",
            "body": Rework(
                **_event(snapshot), **_refs(snapshot, pull, base_ref), issue=issue, base_tip=base_tip,
                findings=[text[:16000] for _, text in feedback], feedback_ids=[node for node, _ in feedback],
            ).model_dump(mode="json")}


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
        execution["version"], snapshot["lease"], runner.offer(snapshot),
    )
    result = PublishedValidationRunner(runner.root / "publication", runner.api, _github(snapshot)).run(
        assignment, policy,
    )
    if result is None:
        raise RunnerError("Published validation claim changed. Reconcile the reservation before continuing")

    def finish(record, db):
        if record.get("lease") == snapshot["lease"]:
            runner._settle(record, result)

    runner._transaction(finish)
    return result

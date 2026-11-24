"""Authenticated HTTP routes for the durable work-item lifecycle."""

import re
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from . import dispatch, finalization, intake, pr_models, pr_observations, reconciliation, waits
from .auth import (
    authenticate, require_coordinator, require_owner, require_runner, require_same_origin, require_worker,
)
from .store import read_transaction, write_transaction
from .transitions import PublicationMode, Stage
from .intake_models import (
    AttemptEvent, Cancel, ExternalWait, Identifier, Intake, Registration, Reply, Rerun, Result,
    RevisionID, Steering, Versioned,
)


router = APIRouter(prefix="/v1", dependencies=[Depends(authenticate)])
Key = Annotated[str | None, Header(alias="Idempotency-Key", min_length=1, max_length=200)]
RequiredKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=200)]
# Runners send their lease; a writer's bearer token is its fence, so writers omit it.
Lease = Annotated[str | None, Header(alias="Phalanx-Lease", pattern=r"^[A-Za-z0-9_-]{32,200}$")]
BatchLimit = Annotated[int, Query(ge=1, le=100)]


def run(request, operation, *args, write=False, finish=False, scope=None, key=None, payload=None):
    try:
        transaction = write_transaction if write else read_transaction
        with transaction(request.app.state.database_path) as db:
            result = intake.replayable(db, scope, key, payload, lambda: operation(db, *args))
        if finish and result["state"]["pending_output"] is not None:
            return finalization.attempt(request.app.state.database_path, result["id"]) or result
        return result
    except intake.Missing as error:
        raise HTTPException(404, str(error)) from error
    except intake.Conflict as error:
        raise HTTPException(409, str(error)) from error


@router.post("/repositories", dependencies=[Depends(require_owner)])
def register(request: Request, body: Registration):
    return run(request, intake.register, body.id, body.remote, write=True)


@router.get("/repositories")
def repositories(request: Request):
    return run(request, intake.repositories)


@router.post("/items", status_code=201,
             dependencies=[Depends(require_owner), Depends(require_same_origin)])
def create(request: Request, body: Intake, key: Key = None):
    omitted = set()
    if body.runner_affinity is None:
        omitted.add("runner_affinity")
    if body.label is None:
        omitted.add("label")
    if not body.dependencies:
        omitted.add("dependencies")
    if body.publication_mode is PublicationMode.READY:
        omitted.add("publication_mode")
    payload = body.model_dump(mode="json", exclude=omitted)
    return run(request, intake.create, body, write=True,
               scope="intake", key=key, payload=payload)


@router.get("/items/{item_id}")
def inspect(request: Request, item_id: Identifier):
    return run(request, intake.inspect, item_id)


@router.patch("/items/{item_id}/executions/{execution_id}", dependencies=[Depends(require_owner)])
def steer(request: Request, item_id: Identifier, execution_id: Identifier,
          body: Steering, key: Key = None):
    return run(request, intake.steer, item_id, execution_id, body, write=True,
               scope=f"steer:{item_id}:{execution_id}", key=key,
               payload=body.model_dump(mode="json", exclude_unset=True))


@router.get("/items/{item_id}/executions")
def history(request: Request, item_id: Identifier):
    return run(request, intake.history, item_id)


@router.get("/items/{item_id}/executions/{execution_id}/timeline")
def timeline(request: Request, item_id: Identifier, execution_id: Identifier):
    return run(request, intake.timeline, item_id, execution_id)


@router.get("/items/{item_id}/executions/{execution_id}/drafts/{revision}",
            response_class=PlainTextResponse)
def draft(request: Request, item_id: Identifier, execution_id: Identifier, revision: RevisionID):
    return run(request, intake.draft, item_id, execution_id, revision)


@router.post("/items/{item_id}/executions/{execution_id}/cancel",
             dependencies=[Depends(require_owner)])
def cancel(request: Request, item_id: Identifier, execution_id: Identifier,
           body: Cancel, key: Key = None):
    return run(request, intake.cancel, item_id, execution_id, body, write=True, finish=True,
               scope=f"cancel:{item_id}:{execution_id}", key=key, payload=body.model_dump(mode="json"))


@router.post("/items/{item_id}/executions/{execution_id}/close-failure",
             dependencies=[Depends(require_owner)])
def close_failure(request: Request, item_id: Identifier, execution_id: Identifier,
                  body: Cancel, key: Key = None):
    return run(request, intake.close_failure, item_id, execution_id, body, write=True, finish=True,
               scope=f"close-failure:{item_id}:{execution_id}", key=key,
               payload=body.model_dump(mode="json"))


@router.post("/items/{item_id}/executions/{execution_id}/retry-finalization",
             dependencies=[Depends(require_owner)])
def retry_finalization(request: Request, item_id: Identifier, execution_id: Identifier,
                       body: Versioned, key: Key = None):
    return run(request, finalization.retry, item_id, execution_id, body, write=True, finish=True,
               scope=f"retry-finalization:{item_id}:{execution_id}", key=key,
               payload=body.model_dump(mode="json"))


@router.post("/items/{item_id}/executions/{execution_id}/rerun",
             dependencies=[Depends(require_owner)])
def rerun(request: Request, item_id: Identifier, execution_id: Identifier,
          body: Rerun, key: Key = None):
    return run(request, intake.rerun, item_id, execution_id, body, write=True,
               scope=f"rerun:{item_id}:{execution_id}", key=key, payload=body.model_dump(mode="json"))


def fence(request, lease):
    """The digest an attempt is fenced by: the runner's lease or the writer's bearer token."""
    writer = getattr(request.state, "writer", None)
    if writer is not None:
        return writer["token_digest"]
    if lease is None:  # step5-remove: runner leases
        raise HTTPException(422, "Runners must send the Phalanx-Lease header")
    return intake.digest(lease)  # step5-remove: runner leases


def attempt_event(request, operation, item_id, execution_id, body, lease, key):
    from . import writers  # writers' routes import this module

    lease_digest = fence(request, lease)
    payload = body.model_dump(mode="json")
    # Preserve receipt digests for results written before these optional fields existed.
    if isinstance(body, Result):
        for field in (
            "failure_reason", "question", "retry_after_seconds", "review_notes", "local_checks",
            "report_review_notes", "pr_url",
        ):
            if payload[field] is None:
                payload.pop(field)
    if isinstance(body, pr_models.Validation):
        if not body.schedule_recheck:
            payload.pop("schedule_recheck")
        for field in ("retry_after_seconds", "wait_limit_seconds"):
            if payload[field] is None:
                payload.pop(field)
    if isinstance(body, ExternalWait) and body.wait_limit_seconds is None:
        payload.pop("wait_limit_seconds")
    receipt = {"body": payload, "lease_digest": lease_digest}
    writer = getattr(request.state, "writer", None)
    continuation = None if writer else next_stage_offer(request)  # step5-remove: runner slots
    if continuation is not None:
        receipt["continuation"] = {"lease_digest": intake.digest(continuation["lease"]),
                                   "stages": continuation["stages"]}

    def perform(db, *args):
        view = operation(db, *args)
        if writer is not None:
            return writers.advance(db, writer["id"], body.attempt_id, view)
        return dispatch.hand_off(db, item_id, body.attempt_id, view, continuation)  # step5-remove

    return run(
        request, perform, item_id, execution_id, body, lease_digest, write=True, finish=True,
        scope=f"{operation.__name__}:{item_id}:{execution_id}", key=key, payload=receipt,
    )


def next_stage_offer(request):
    """The runner's optional offer to keep its slot for the next stage under a fresh lease."""
    lease = request.headers.get("Phalanx-Next-Lease")
    stages = request.headers.get("Phalanx-Next-Stages")
    if lease is None and stages is None:
        return None
    names = sorted({name.strip() for name in (stages or "").split(",") if name.strip()})
    if (lease is None or not re.fullmatch(r"[A-Za-z0-9_-]{32,200}", lease)
            or not names or any(name not in set(Stage) for name in names)):
        raise HTTPException(422, "A continuation needs a valid next lease and runnable stages")
    return {"lease": lease, "stages": names}


@router.post("/items/{item_id}/executions/{execution_id}/result",
             dependencies=[Depends(require_worker)])
def result(request: Request, item_id: Identifier, execution_id: Identifier,
           body: Result, lease: Lease = None, key: Key = None):
    return attempt_event(request, intake.result, item_id, execution_id, body, lease, key)


@router.post("/items/{item_id}/executions/{execution_id}/stop-confirmed",
             dependencies=[Depends(require_worker)])
def stop_confirmed(request: Request, item_id: Identifier, execution_id: Identifier,
                   body: AttemptEvent, lease: Lease = None, key: Key = None):
    return attempt_event(request, intake.stop_confirmed, item_id, execution_id, body, lease, key)


@router.get("/owner/inbox", dependencies=[Depends(require_owner)])
def inbox(request: Request, limit: BatchLimit = 50,
          after_time: Annotated[str, Query(max_length=40)] = "",
          after_id: Identifier | None = None):
    return run(request, waits.inbox, limit, after_time, after_id or "")


@router.post("/items/{item_id}/executions/{execution_id}/reply",
             dependencies=[Depends(require_owner)])
def reply(request: Request, item_id: Identifier, execution_id: Identifier,
          body: Reply, key: RequiredKey):
    return run(request, waits.reply, item_id, execution_id, body, write=True,
               scope=f"reply:{item_id}:{execution_id}", key=key, payload=body.model_dump(mode="json"))


@router.post("/items/{item_id}/executions/{execution_id}/external-wait",
             dependencies=[Depends(require_worker)])
def external_wait(request: Request, item_id: Identifier, execution_id: Identifier,
                  body: ExternalWait, key: RequiredKey, lease: Lease = None):
    return attempt_event(request, waits.external_wait, item_id, execution_id, body, lease, key)


@router.get("/runner/due-rechecks", dependencies=[Depends(require_runner)])
def due_rechecks(request: Request, limit: BatchLimit = 50):
    return run(request, waits.due_rechecks, limit)


@router.post("/items/{item_id}/executions/{execution_id}/recheck",
             dependencies=[Depends(require_runner)])
def recheck(request: Request, item_id: Identifier, execution_id: Identifier,
            body: Versioned, key: RequiredKey):
    return run(request, waits.recheck, item_id, execution_id, body, write=True,
               scope=f"recheck:{item_id}:{execution_id}", key=key, payload=body.model_dump(mode="json"))


@router.post("/items/{item_id}/executions/{execution_id}/github-observation",
             dependencies=[Depends(require_runner)])
def github_observation(request: Request, item_id: Identifier, execution_id: Identifier,
                       body: reconciliation.Observation):
    """Record what the reconciler saw on GitHub; no lease. It advances only a writer's PR stages."""
    return run(request, reconciliation.record, item_id, execution_id, body, write=True)


@router.post("/items/{item_id}/executions/{execution_id}/local-checks", dependencies=[Depends(require_runner)])
def local_checks(request: Request, item_id: Identifier, execution_id: Identifier,
                 body: reconciliation.LocalChecks):
    """Record trusted pre-PR check results for an observed PR head; a flag, never a gate."""
    return run(request, reconciliation.record_local_checks, item_id, execution_id, body, write=True)


@router.post("/reconcilers/{runner_id}/heartbeat", dependencies=[Depends(require_runner)])
def reconciler_heartbeat(request: Request, runner_id: Identifier, body: reconciliation.Heartbeat | None = None):
    return run(request, reconciliation.heartbeat, runner_id, body, write=True)


@router.delete("/reconcilers/{runner_id}", dependencies=[Depends(require_coordinator)])
def retire_reconciler(request: Request, runner_id: Identifier):
    return run(request, reconciliation.retire, runner_id, write=True)


@router.post("/items/{item_id}/executions/{execution_id}/pr-publication-claim",
             dependencies=[Depends(require_worker)])
def pr_publication_claim(request: Request, item_id: Identifier, execution_id: Identifier,
                         body: pr_models.PublicationClaimCheck, lease: Lease = None):
    return run(request, pr_observations.check_publication_claim, item_id, execution_id, body,
               fence(request, lease))


@router.post("/items/{item_id}/executions/{execution_id}/pr-observation",
             dependencies=[Depends(require_worker)])
def pr_observation(request: Request, item_id: Identifier, execution_id: Identifier,
                   body: pr_models.Observation,
                   lease: Lease = None, key: Key = None):
    return attempt_event(request, pr_observations.submit, item_id, execution_id, body, lease, key)


@router.post("/items/{item_id}/executions/{execution_id}/pr-publication-blocker",
             dependencies=[Depends(require_worker)])
def pr_publication_blocker(request: Request, item_id: Identifier, execution_id: Identifier,
                           body: pr_models.PublicationBlocker,
                           lease: Lease = None, key: Key = None):
    return attempt_event(request, pr_observations.submit_blocker, item_id, execution_id, body, lease, key)


@router.post("/items/{item_id}/executions/{execution_id}/pr-rework",
             dependencies=[Depends(require_worker)])
def pr_rework(request: Request, item_id: Identifier, execution_id: Identifier, body: pr_models.Rework,
              lease: Lease = None, key: Key = None):
    return attempt_event(request, pr_observations.submit_rework, item_id, execution_id, body, lease, key)


@router.post("/items/{item_id}/executions/{execution_id}/pr-merged",
             dependencies=[Depends(require_runner)])
def pr_merged(request: Request, item_id: Identifier, execution_id: Identifier, body: pr_models.ExternalMerge,
              lease: Lease, key: Key = None):
    return attempt_event(request, pr_observations.submit_merged, item_id, execution_id, body, lease, key)


@router.post("/items/{item_id}/executions/{execution_id}/pr-validation-claim",
             dependencies=[Depends(require_worker)])
def pr_validation_claim(request: Request, item_id: Identifier, execution_id: Identifier,
                        body: pr_models.PublishedValidationClaimCheck, lease: Lease = None):
    return run(request, pr_observations.check_published_validation_claim,
               item_id, execution_id, body, fence(request, lease))

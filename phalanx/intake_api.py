"""Authenticated HTTP routes for the durable work-item lifecycle."""

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from . import finalization, intake, reconciliation, waits
from .auth import (
    authenticate, require_coordinator, require_owner, require_runner, require_same_origin, require_writer,
)
from .store import read_transaction, write_transaction
from .transitions import PublicationMode
from .intake_models import (
    AttemptEvent, Cancel, DisplayTitle, ExternalWait, Identifier, Intake, Registration, Reply, Rerun, Result,
    RevisionID, Steering, Versioned,
)


router = APIRouter(prefix="/v1", dependencies=[Depends(authenticate)])
Key = Annotated[str | None, Header(alias="Idempotency-Key", min_length=1, max_length=200)]
RequiredKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=200)]
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


@router.get("/items/{item_id}/title", dependencies=[Depends(require_owner)])
def display_title(request: Request, item_id: Identifier):
    return run(request, intake.display_title, item_id)


@router.put("/items/{item_id}/title",
            dependencies=[Depends(require_owner), Depends(require_same_origin)])
def set_display_title(request: Request, item_id: Identifier, body: DisplayTitle, key: RequiredKey):
    return run(request, intake.set_display_title, item_id, body, write=True,
               scope=f"display-title:{item_id}", key=key, payload=body.model_dump(mode="json"))


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


def attempt_event(request, operation, item_id, execution_id, body, key):
    from . import writers  # writers' routes import this module

    writer = request.state.writer
    lease_digest = writer["token_digest"]
    payload = body.model_dump(mode="json")
    # Preserve receipt digests for results written before these optional fields existed.
    if isinstance(body, Result):
        for field in (
            "failure_reason", "question", "retry_after_seconds", "review_notes",
            "report_review_notes", "pr_url",
        ):
            if payload[field] is None:
                payload.pop(field)
    if isinstance(body, ExternalWait) and body.wait_limit_seconds is None:
        payload.pop("wait_limit_seconds")
    receipt = {"body": payload, "lease_digest": lease_digest}
    def perform(db, *args):
        view = operation(db, *args)
        return writers.advance(db, writer["id"], body.attempt_id, view)

    return run(
        request, perform, item_id, execution_id, body, lease_digest, write=True, finish=True,
        scope=f"{operation.__name__}:{item_id}:{execution_id}", key=key, payload=receipt,
    )


@router.post("/items/{item_id}/executions/{execution_id}/result",
             dependencies=[Depends(require_writer)])
def result(request: Request, item_id: Identifier, execution_id: Identifier,
           body: Result, key: Key = None):
    return attempt_event(request, intake.result, item_id, execution_id, body, key)


@router.post("/items/{item_id}/executions/{execution_id}/stop-confirmed",
             dependencies=[Depends(require_writer)])
def stop_confirmed(request: Request, item_id: Identifier, execution_id: Identifier,
                   body: AttemptEvent, key: Key = None):
    return attempt_event(request, intake.stop_confirmed, item_id, execution_id, body, key)


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
             dependencies=[Depends(require_writer)])
def external_wait(request: Request, item_id: Identifier, execution_id: Identifier,
                  body: ExternalWait, key: RequiredKey):
    return attempt_event(request, waits.external_wait, item_id, execution_id, body, key)


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

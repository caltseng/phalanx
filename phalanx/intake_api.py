"""Authenticated HTTP routes for the durable work-item lifecycle."""

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from . import intake, waits
from .auth import authenticate, require_owner, require_runner
from .store import write_transaction
from .intake_models import (
    AttemptEvent, Cancel, ExternalWait, Identifier, Intake, Registration, Reply, Rerun, Result,
    RevisionID, Versioned,
)


router = APIRouter(prefix="/v1", dependencies=[Depends(authenticate)])
Key = Annotated[str | None, Header(alias="Idempotency-Key", min_length=1, max_length=200)]
RequiredKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=200)]
Lease = Annotated[str, Header(alias="Phalanx-Lease", pattern=r"^[A-Za-z0-9_-]{32,200}$")]
BatchLimit = Annotated[int, Query(ge=1, le=100)]


def run(request, operation, *args, write=False, scope=None, key=None, payload=None):
    try:
        transaction = write_transaction if write else intake.read_transaction
        with transaction(request.app.state.database_path) as db:
            return intake.replayable(db, scope, key, payload, lambda: operation(db, *args))
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


@router.post("/items", status_code=201, dependencies=[Depends(require_owner)])
def create(request: Request, body: Intake, key: Key = None):
    payload = body.model_dump(mode="json", exclude={"runner_affinity"} if body.runner_affinity is None else None)
    return run(request, intake.create, body, write=True,
               scope="intake", key=key, payload=payload)


@router.get("/items/{item_id}")
def inspect(request: Request, item_id: Identifier):
    return run(request, intake.inspect, item_id)


@router.get("/items/{item_id}/executions")
def history(request: Request, item_id: Identifier):
    return run(request, intake.history, item_id)


@router.get("/items/{item_id}/executions/{execution_id}/timeline")
def timeline(request: Request, item_id: Identifier, execution_id: Identifier):
    return run(request, intake.timeline, item_id, execution_id)


@router.get("/items/{item_id}/executions/{execution_id}/report", response_class=PlainTextResponse)
def report(request: Request, item_id: Identifier, execution_id: Identifier):
    return run(request, intake.report, item_id, execution_id)


@router.get("/items/{item_id}/executions/{execution_id}/drafts/{revision}",
            response_class=PlainTextResponse)
def draft(request: Request, item_id: Identifier, execution_id: Identifier, revision: RevisionID):
    return run(request, intake.draft, item_id, execution_id, revision)


@router.post("/items/{item_id}/executions/{execution_id}/cancel",
             dependencies=[Depends(require_owner)])
def cancel(request: Request, item_id: Identifier, execution_id: Identifier,
           body: Cancel, key: Key = None):
    return run(request, intake.cancel, item_id, execution_id, body, write=True,
               scope=f"cancel:{item_id}:{execution_id}", key=key, payload=body.model_dump(mode="json"))


@router.post("/items/{item_id}/executions/{execution_id}/rerun",
             dependencies=[Depends(require_owner)])
def rerun(request: Request, item_id: Identifier, execution_id: Identifier,
          body: Rerun, key: Key = None):
    return run(request, intake.rerun, item_id, execution_id, body, write=True,
               scope=f"rerun:{item_id}:{execution_id}", key=key, payload=body.model_dump(mode="json"))


def attempt_event(request, operation, item_id, execution_id, body, lease, key):
    lease_digest = intake.digest(lease)
    return run(
        request, operation, item_id, execution_id, body, lease_digest, write=True,
        scope=f"{operation.__name__}:{item_id}:{execution_id}", key=key,
        payload={"body": body.model_dump(mode="json"), "lease_digest": lease_digest},
    )


@router.post("/items/{item_id}/executions/{execution_id}/result",
             dependencies=[Depends(require_runner)])
def result(request: Request, item_id: Identifier, execution_id: Identifier,
           body: Result, lease: Lease, key: Key = None):
    return attempt_event(request, intake.result, item_id, execution_id, body, lease, key)


@router.post("/items/{item_id}/executions/{execution_id}/stop-confirmed",
             dependencies=[Depends(require_runner)])
def stop_confirmed(request: Request, item_id: Identifier, execution_id: Identifier,
                   body: AttemptEvent, lease: Lease, key: Key = None):
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
             dependencies=[Depends(require_runner)])
def external_wait(request: Request, item_id: Identifier, execution_id: Identifier,
                  body: ExternalWait, lease: Lease, key: RequiredKey):
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

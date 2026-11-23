"""Owner configuration and authenticated runner dispatch routes."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import Field, StringConstraints

from . import dispatch, intake
from .auth import require_owner, require_runner
from .intake_api import Key, Lease, run
from .intake_models import Identifier, Payload, Text, Versioned
from .transitions import Stage


router = APIRouter(prefix="/v1")
Slots = Annotated[int, Field(strict=True, ge=1, le=256)]


class DispatchSettings(Payload):
    paused: Annotated[bool, Field(strict=True)]
    aging_seconds: Annotated[int, Field(strict=True, ge=1, le=86400)] = 300


class Capacity(Payload):
    capacity: Slots


class Claim(Payload):
    runner_id: Identifier
    repository_ids: list[Identifier] = Field(min_length=1, max_length=100)
    slots: Slots | None = None
    stages: list[Stage] | None = Field(default=None, min_length=1, max_length=len(Stage))


class ReconciledStop(Versioned):
    item_id: Identifier
    reconciliation: Text


class WorkerReference(Payload):
    reference: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    url: Annotated[str, StringConstraints(max_length=2000, pattern=r"^https://[^\s/@?#]+(/\S*)?$")] | None = None


class Renewal(Payload):
    worker: WorkerReference | None = None
    blocker: Text | None = None


@router.put("/dispatch/settings", dependencies=[Depends(require_owner)])
def settings(request: Request, body: DispatchSettings):
    return run(request, dispatch.set_dispatch, body, write=True)


@router.put("/dispatch/runners/{runner_id}/capacity", dependencies=[Depends(require_owner)])
def capacity(request: Request, runner_id: Identifier, body: Capacity):
    return run(request, dispatch.set_capacity, runner_id, body.capacity, write=True)


@router.get("/dispatch/runners/{runner_id}", dependencies=[Depends(require_runner)])
def runner_view(request: Request, runner_id: Identifier):
    return run(request, dispatch.runner_view, runner_id)


@router.post("/dispatch/claim", dependencies=[Depends(require_runner)])
def claim(request: Request, body: Claim, lease: Lease):
    return run(request, dispatch.claim, body, intake.digest(lease), write=True)


@router.post("/dispatch/runners/{runner_id}/attempts/{attempt_id}/renew",
             dependencies=[Depends(require_runner)])
def renew(request: Request, runner_id: Identifier, attempt_id: Identifier, lease: Lease,
          body: Renewal | None = None):
    worker = body.worker if body else None
    blocker = body.blocker if body else None
    return run(request, dispatch.renew, runner_id, attempt_id, intake.digest(lease), worker, blocker,
               write=True)


@router.get("/dispatch/runners/{runner_id}/recovery", dependencies=[Depends(require_runner)])
def recovery(request: Request, runner_id: Identifier):
    return run(request, dispatch.recovery_candidates, runner_id)


@router.post("/dispatch/runners/{runner_id}/attempts/{attempt_id}/recover-stopped",
             dependencies=[Depends(require_runner)])
def recover_stopped(request: Request, runner_id: Identifier, attempt_id: Identifier,
                    body: ReconciledStop, key: Key = None):
    return run(request, dispatch.recover_stopped, runner_id, attempt_id, body, write=True, finish=True,
               scope=f"recover-stopped:{runner_id}:{attempt_id}", key=key,
               payload=body.model_dump(mode="json"))


@router.post("/dispatch/runners/{runner_id}/attempts/{attempt_id}/recover-claim",
             dependencies=[Depends(require_runner)])
def recover_claim(request: Request, runner_id: Identifier, attempt_id: Identifier,
                  body: ReconciledStop, lease: Lease, key: Key = None):
    digest = intake.digest(lease)
    return run(request, dispatch.recover_claim, runner_id, attempt_id, digest, body, write=True,
               scope=f"recover-claim:{runner_id}:{attempt_id}", key=key,
               payload={**body.model_dump(mode="json"), "lease_digest": digest})

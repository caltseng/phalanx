"""Dispatch settings and renewal for remaining runner-owned stages."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import Field, StringConstraints

from . import dispatch, intake
from .auth import require_owner, require_runner
from .intake_api import Lease, run
from .intake_models import Identifier, Payload, Text


router = APIRouter(prefix="/v1")


class DispatchSettings(Payload):
    paused: Annotated[bool, Field(strict=True)]
    aging_seconds: Annotated[int, Field(strict=True, ge=1, le=86400)] = 300


class WorkerReference(Payload):
    reference: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    url: Annotated[str, StringConstraints(max_length=2000, pattern=r"^https://[^\s/@?#]+(/\S*)?$")] | None = None


class Renewal(Payload):
    worker: WorkerReference | None = None
    blocker: Text | None = None


@router.put("/dispatch/settings", dependencies=[Depends(require_owner)])
def settings(request: Request, body: DispatchSettings):
    return run(request, dispatch.set_dispatch, body, write=True)


@router.post("/dispatch/runners/{runner_id}/attempts/{attempt_id}/renew",
             dependencies=[Depends(require_runner)])
def renew(request: Request, runner_id: Identifier, attempt_id: Identifier, lease: Lease,
          body: Renewal | None = None):
    if lease is None:
        from fastapi import HTTPException

        raise HTTPException(422, "Runners must send the Phalanx-Lease header")
    return run(request, dispatch.renew, runner_id, attempt_id, intake.digest(lease),
               body.worker if body else None, body.blocker if body else None, write=True)

"""Owner-controlled direct dispatch settings."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import Field

from . import dispatch
from .auth import require_owner
from .intake_api import run
from .intake_models import Payload


router = APIRouter(prefix="/v1")


class DispatchSettings(Payload):
    paused: Annotated[bool, Field(strict=True)]
    aging_seconds: Annotated[int, Field(strict=True, ge=1, le=86400)] = 300


@router.put("/dispatch/settings", dependencies=[Depends(require_owner)])
def settings(request: Request, body: DispatchSettings):
    return run(request, dispatch.set_dispatch, body, write=True)

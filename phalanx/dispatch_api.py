"""Owner-controlled direct dispatch settings."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import Field, model_validator

from . import dispatch
from .auth import authenticate_board_read, require_owner, require_same_origin
from .intake_api import run
from .intake_models import Payload


router = APIRouter(prefix="/v1")


class DispatchSettings(Payload):
    paused: Annotated[bool, Field(strict=True)] | None = None
    aging_seconds: Annotated[int, Field(strict=True, ge=1, le=86400)] | None = None
    max_writers: Annotated[int, Field(strict=True, gt=0, le=2147483647)] | None = None
    max_building_writers: Annotated[int, Field(strict=True, gt=0, le=2147483647)] | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_null(cls, values):
        if not isinstance(values, dict):
            return values
        if any(value is None for value in values.values()):
            raise ValueError("Dispatch settings cannot be null; omit unchanged fields")
        return values


@router.get("/dispatch/settings", dependencies=[Depends(authenticate_board_read)])
def read_settings(request: Request):
    return run(request, dispatch.settings)


@router.put("/dispatch/settings",
            dependencies=[Depends(require_owner), Depends(require_same_origin)])
def settings(request: Request, body: DispatchSettings):
    return run(request, dispatch.set_dispatch, body, write=True)

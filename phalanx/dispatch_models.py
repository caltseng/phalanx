"""Owner-controlled direct dispatch settings."""

from typing import Annotated

from pydantic import Field, model_validator

from .intake_models import Payload




class DispatchSettings(Payload):
    paused: Annotated[bool, Field(strict=True)] | None = None
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

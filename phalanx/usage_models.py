"""Allowlisted cumulative usage contract shared by native clients and the API."""

from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, StringConstraints, model_validator

from .intake_models import Payload


Label = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
MAX_COUNTER = 9007199254740991
Counter = Annotated[int, Field(strict=True, ge=0, le=MAX_COUNTER)]
Amount = Annotated[str, StringConstraints(pattern=r"^(0|[1-9][0-9]{0,14})(\.[0-9]{1,9})?$")]
TOKEN_FIELDS = ("input", "output", "cache_read", "cache_write", "reasoning", "total")


class Tokens(Payload):
    input: Counter | None = None
    output: Counter | None = None
    cache_read: Counter | None = None
    cache_write: Counter | None = None
    reasoning: Counter | None = None
    total: Counter | None = None


class Quantity(Payload):
    amount: Amount
    unit: Label
    basis: Literal["provider_reported", "estimated"]
    source: Label


class Snapshot(Payload):
    runtime: Label
    provider: Label
    account: Label
    origin: Label
    session_id: Label
    source: Label
    source_version: Label
    epoch: Label
    model: Label | None = None
    sequence: Counter
    window_start: AwareDatetime
    cutoff: AwareDatetime
    observed_at: AwareDatetime
    scope: Literal["item_exclusive", "session_lifetime"]
    status: Literal["observed", "partial", "unavailable", "pending_final"]
    reason: Reason
    tokens: Tokens = Field(default_factory=Tokens)
    credits: Quantity | None = None
    money: Quantity | None = None

    @model_validator(mode="after")
    def valid_window(self):
        if not self.window_start <= self.cutoff <= self.observed_at:
            raise ValueError("Usage window must precede observation")
        if self.observed_at > datetime.now(UTC):
            raise ValueError("Usage observation cannot be in the future")
        known = any(value is not None for value in self.tokens.model_dump().values())
        if self.status == "unavailable" and (known or self.credits or self.money):
            raise ValueError("Unavailable acquisition must not invent or erase numeric counters")
        if self.status != "unavailable" and not (known or self.credits or self.money):
            raise ValueError("Numeric usage status requires a supplied measurement; otherwise use unavailable")
        return self

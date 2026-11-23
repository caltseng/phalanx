"""Strict validation policy loaded only from an operator-owned runner file."""

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, TypeAdapter, ValidationError, field_validator, model_validator

from .github_checks import PreviewReader
from .local_checks import LocalCheck
from .intake_models import Identifier, Payload, Text
from .pr_models import RequiredCheck as CheckConfig
from .validation import CheckKind, PreCheck, PreOutcome, RequiredCheck

_IDENTIFIER = TypeAdapter(Identifier)
_MAX_BYTES = 1024 * 1024
_NAME = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=255)]
_ARGUMENT = Annotated[str, StringConstraints(strict=True, pattern=r"^[^\x00]*$")]


class ValidationPolicyError(ValueError):
    """Repair the trusted configuration before dispatching validation."""


@dataclass(frozen=True)
class PublishedValidationPolicy:
    """Complete ready-phase policy from trusted runner configuration."""

    required: tuple[RequiredCheck, ...] | None
    read_preview: PreviewReader | None = None
    # Pending ready checks; None leaves the server defaults.
    recheck_seconds: int | None = None
    wait_limit_seconds: int | None = None


@dataclass(frozen=True)
class RepositoryValidationPolicy:
    local_checks: tuple[LocalCheck, ...] | None
    draft_checks: tuple[RequiredCheck, ...] | None
    ready: PublishedValidationPolicy


class _Bypass(Payload):
    outcome: Literal["skipped", "inapplicable"]
    reason: Text
    authorization: Text


class _LocalConfig(Payload):
    name: _NAME
    argv: list[_ARGUMENT] | None = Field(default=None, min_length=1)
    timeout_seconds: Annotated[int, Field(strict=True, ge=1, le=86400)] = 60
    allow_skip: Annotated[bool, Field(strict=True)] = False
    allow_inapplicable: Annotated[bool, Field(strict=True)] = False
    bypass: _Bypass | None = None

    @field_validator("name")
    @classmethod
    def exact_name(cls, value):
        return _exact_name(value)

    @model_validator(mode="after")
    def authorized_check(self):
        if self.bypass is not None:
            if "argv" in self.model_fields_set or "timeout_seconds" in self.model_fields_set:
                raise ValueError("A bypass cannot configure a command or timeout")
            allowed = (self.allow_skip if self.bypass.outcome == "skipped"
                       else self.allow_inapplicable)
            if not allowed:
                raise ValueError("The bypass must be authorized for this check's disposition")
        self.to_check()
        return self

    def to_check(self) -> LocalCheck:
        policy = PreCheck(self.name, self.allow_inapplicable, self.allow_skip)
        if self.bypass is not None:
            return LocalCheck(policy, disposition=PreOutcome(self.bypass.outcome),
                              reason=self.bypass.reason, authorization=self.bypass.authorization)
        if self.argv is None:
            raise ValueError("A local check requires argv or an authorized bypass")
        return LocalCheck(policy, tuple(self.argv), self.timeout_seconds)


class _PublishedConfig(CheckConfig):
    @field_validator("name")
    @classmethod
    def exact_name(cls, value):
        return _exact_name(value)

    @model_validator(mode="after")
    def supported_source(self):
        if self.kind == CheckKind.STATUS and self.app_id is not None:
            raise ValueError("The GitHub status reader cannot verify a source app ID")
        return self

    def to_check(self) -> RequiredCheck:
        return RequiredCheck(**self.model_dump())


class _RepositoryConfig(Payload):
    local_checks: list[_LocalConfig] | None = Field(max_length=50)
    draft_checks: list[_PublishedConfig] | None = Field(max_length=100)
    ready_checks: list[_PublishedConfig] | None = Field(max_length=100)

    @model_validator(mode="after")
    def unique_checks(self):
        names = [check.name for check in self.local_checks or ()]
        if len(set(names)) != len(names):
            raise ValueError("Local check names must be unique")
        for checks in (self.draft_checks, self.ready_checks):
            sources = {}
            for check in checks or ():
                apps = sources.setdefault((check.kind, check.name), set())
                if check.app_id in apps or (apps and (None in apps or check.app_id is None)):
                    raise ValueError("Published check identities must not duplicate or overlap")
                apps.add(check.app_id)
        return self


class _Config(Payload):
    version: Annotated[int, Field(strict=True, ge=1, le=1)]
    repositories: dict[Identifier, _RepositoryConfig]


def _exact_name(value: str) -> str:
    if value != value.strip() or not value.strip() or "\x00" in value:
        raise ValueError("Check names must be nonblank exact identities without surrounding whitespace")
    return value


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValidationPolicyError(f"Duplicate configuration key: {key}")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValidationPolicyError(f"Non-JSON numeric constant: {value}")


def load_validation_policy(config_path: Path, repository_id: str) -> RepositoryValidationPolicy:
    """Load all schema entries, then select an exact registered repository ID.

    The caller must supply an absolute, trusted runner-local path outside all
    worker-writable checkouts. Never derive it from the work packet, repository
    contents, or worker result. Missing/malformed policy raises an actionable
    error; null phase lists remain unavailable, while [] is explicitly empty.
    """
    path = Path(config_path)
    if not path.is_absolute():
        raise ValidationPolicyError("Supply an absolute operator-owned validation policy path")
    try:
        _IDENTIFIER.validate_python(repository_id)
        with path.open("rb") as stream:
            raw = stream.read(_MAX_BYTES + 1)
        if len(raw) > _MAX_BYTES:
            raise ValidationPolicyError("Validation policy exceeds the 1 MiB limit")
        config = _Config.model_validate(json.loads(
            raw.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_invalid_constant,
        ))
    except (OSError, UnicodeError, ValueError, RecursionError) as error:
        if isinstance(error, ValidationPolicyError):
            raise
        if isinstance(error, ValidationError):
            # Avoid echoing configured commands or authority in diagnostics.
            details = "; ".join(
                f"{'.'.join(map(str, entry['loc']))}: {entry['msg']}"
                for entry in error.errors(include_input=False, include_context=False)
            )
        else:
            details = str(error)
        raise ValidationPolicyError(f"Repair the trusted validation policy at {path}: {details}") from None
    if repository_id not in config.repositories:
        raise ValidationPolicyError(f"No validation policy for registered repository {repository_id}")
    selected = config.repositories[repository_id]
    return RepositoryValidationPolicy(
        None if selected.local_checks is None else tuple(check.to_check() for check in selected.local_checks),
        None if selected.draft_checks is None else tuple(check.to_check() for check in selected.draft_checks),
        PublishedValidationPolicy(
            None if selected.ready_checks is None else tuple(check.to_check() for check in selected.ready_checks),
        ),
    )

"""Candidate-review values used by the remaining runner-owned stages."""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import UUID

from .transitions import Outcome


@dataclass(frozen=True, slots=True)
class WorkPacket:
    item_id: str
    attempt_id: UUID
    state_version: int
    stage: str
    repository: str
    worktree: Path
    branch: str
    expected_revision: str
    goal: str
    output_type: Literal["report", "pull_request"]
    evidence_requirements: str
    context: str = ""
    handoff: str = ""
    base_branch: str | None = None


@dataclass(frozen=True, slots=True)
class WorkerResult:
    packet: WorkPacket
    outcome: Outcome
    details: dict

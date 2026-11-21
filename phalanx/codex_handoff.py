"""Render an attended Codex handoff and read local attempt evidence."""

import json
import os
import stat
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal, TypedDict
from uuid import UUID

CONTRACT_VERSION = 1
MAX_RESULT_BYTES = 64 * 1024


class Outcome(StrEnum):
    SUCCEEDED = "succeeded"
    CHANGES_REQUIRED = "changes_required"
    NEEDS_INPUT = "needs_input"
    FAILED = "failed"


OUTCOME_FIELDS = {
    Outcome.SUCCEEDED: ("evidence",),
    Outcome.CHANGES_REQUIRED: ("findings",),
    Outcome.NEEDS_INPUT: ("question", "options", "handoff"),
    Outcome.FAILED: ("reason", "attempted", "retry_guidance"),
}


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
    report_checkpoint: dict[str, str] | None = None
    dependencies: tuple[dict, ...] = ()

    @property
    def marker(self) -> str:
        return f"phalanx-attempt-{self.attempt_id}"

    def result_identity(self) -> dict[str, str | int]:
        identity = {
            "contract_version": CONTRACT_VERSION,
            "item_id": self.item_id,
            "attempt_id": str(self.attempt_id),
            "state_version": self.state_version,
            "stage": self.stage,
            "repository": self.repository,
            "worktree": str(self.worktree),
            "branch": self.branch,
            "expected_revision": self.expected_revision,
        }

        if self.report_checkpoint and self.stage == "report_review":
            identity["report_revision"] = self.report_checkpoint["revision"]
        return identity


class LocalEnvironment(TypedDict):
    type: Literal["local"]


class ProjectTarget(TypedDict):
    type: Literal["project"]
    projectId: str
    environment: LocalEnvironment


class CreateThreadArguments(TypedDict):
    title: str
    prompt: str
    target: ProjectTarget


@dataclass(frozen=True, slots=True)
class WorkerResult:
    """Schema-valid evidence attributed to a packet by exact field matches."""

    packet: WorkPacket
    outcome: Outcome
    details: dict[str, str]


class InvalidResult(ValueError):
    """A result exists or could not be read, but is unusable as evidence."""


@dataclass(frozen=True, slots=True)
class CodexHandoff:
    """Use a runner-controlled result directory with trusted, stable ancestors.

    The runner supplies a verified worktree and assignment. The Captain resolves
    the project using list_projects and invokes create_thread after authorization
    and durable launch intent. This class performs neither operation.
    """

    packet: WorkPacket
    result_directory: Path

    def __post_init__(self) -> None:
        if not isinstance(self.packet.attempt_id, UUID):
            raise ValueError("Attempt ID must be a UUID")
        for path in (self.packet.worktree, self.result_directory):
            if not path.is_absolute() or ".." in path.parts:
                raise ValueError("Worktree and result directory must be absolute paths")

    @property
    def result_path(self) -> Path:
        return self.result_directory / f"{self.packet.marker}.json"

    @property
    def temporary_path(self) -> Path:
        return self.result_path.with_suffix(".json.tmp")

    def create_thread_arguments(self, project_id: str) -> CreateThreadArguments:
        """Render arguments for the Captain's explicit project-local invocation."""
        packet = self.packet
        assignment = json.dumps({
            "result_identity": packet.result_identity(),
            "output_type": packet.output_type,
            "base_branch": packet.base_branch,
            "goal": packet.goal,
            "evidence_requirements": packet.evidence_requirements,
            "context": packet.context,
            "handoff": packet.handoff,
            **({"report_checkpoint": packet.report_checkpoint} if packet.report_checkpoint else {}),
            "dependencies": packet.dependencies,
        }, indent=2, ensure_ascii=False)
        outcomes = "\n".join(
            f"- {outcome}: {', '.join(fields)}"
            for outcome, fields in OUTCOME_FIELDS.items()
        )
        return {
            "title": f"Phalanx worker [{packet.marker}]",
            "target": {
                "type": "project",
                "projectId": project_id,
                "environment": {"type": "local"},
            },
            "prompt": f"""Phalanx attempt marker: {packet.marker}

Execute only the assigned stage in the packet below. Scope is authorized
repository development, maintenance, documentation, testing, or defensive
review. Do not perform offensive or exploit workflows. Read the applicable
repository instructions, run relevant checks, and leave GitHub writes to the
runner.

The app may start in the project checkout. Use the absolute worktree path in
the packet as the explicit working directory for every repository command and
target all repository edits there. Before writing, verify the Git root,
current branch, and HEAD against the packet. If you cannot verify or target
the worktree, report the workspace blocker and leave repository files untouched.

Work packet:
{assignment}

Write one UTF-8 JSON object of at most {MAX_RESULT_BYTES} bytes. Copy every
result_identity field verbatim to the top level, including expected_revision
even if the stage creates a new commit. Add only "outcome" and "details".
Choose one outcome and provide its exact details fields as nonempty strings:
{outcomes}
Describe stage-specific evidence and any new candidate revision or checkpoint
in the details. Include references as text when useful.

Write the complete JSON to the temporary path, then rename it to the final
path in the same directory. Include the final path and outcome in your last
chat message.

Temporary path: {self.temporary_path}
Final path: {self.result_path}
""",
        }

    def read_result(self) -> WorkerResult | None:
        """Read evidence without confirming chat stop, admission, or persistence."""
        try:
            if self.result_directory.resolve(strict=True) != self.result_directory:
                raise InvalidResult("Result directory must use its canonical path")
            if not self.result_directory.is_dir():
                raise InvalidResult("Result directory must be a directory")
            raw = _read_regular_file(self.result_path)
        except OSError as exc:
            raise InvalidResult("Could not read the attempt result") from exc
        if raw is None:
            return None
        try:
            result = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        except (ValueError, RecursionError) as exc:
            raise InvalidResult("Result must be valid UTF-8 JSON with unique keys") from exc

        identity = self.packet.result_identity()
        if not isinstance(result, dict) or result.keys() != identity.keys() | {"outcome", "details"}:
            raise InvalidResult("Unexpected result fields")
        for key, expected in identity.items():
            value = result[key]
            if type(value) is not type(expected) or value != expected:
                raise InvalidResult(f"Result does not match the packet's {key}")
        try:
            outcome = Outcome(result["outcome"])
        except ValueError as exc:
            raise InvalidResult("Unknown worker outcome") from exc
        details = result["details"]
        if not isinstance(details, dict) or details.keys() != set(OUTCOME_FIELDS[outcome]):
            raise InvalidResult(f"Unexpected details fields for {outcome}")
        if any(not isinstance(value, str) or not value.strip() for value in details.values()):
            raise InvalidResult("Result details must be nonempty strings")
        return WorkerResult(self.packet, outcome, details)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _is_regular_result(info: os.stat_result) -> bool:
    return (
        stat.S_ISREG(info.st_mode)
        and info.st_nlink == 1
        and not getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def _read_regular_file(path: Path) -> bytes | None:
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    if not _is_regular_result(before):
        raise InvalidResult("Result must be a regular, unlinked file")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with open(path, "rb", opener=lambda name, _: os.open(name, flags)) as stream:
        opened = os.fstat(stream.fileno())
        current = path.lstat()
        if not all(_is_regular_result(info) for info in (opened, current)):
            raise InvalidResult("Result must be a regular, unlinked file")
        if not os.path.samestat(before, opened) or not os.path.samestat(opened, current):
            raise InvalidResult("Result file changed while opening")
        if opened.st_size > MAX_RESULT_BYTES:
            raise InvalidResult("Result exceeds the size limit")
        raw = stream.read(MAX_RESULT_BYTES + 1)
        if len(raw) > MAX_RESULT_BYTES:
            raise InvalidResult("Result exceeds the size limit")
        after = os.fstat(stream.fileno())
        if (after.st_size, after.st_mtime_ns) != (opened.st_size, opened.st_mtime_ns):
            raise InvalidResult("Result file changed while reading")
    return raw

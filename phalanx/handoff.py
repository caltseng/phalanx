"""Render an attended worker handoff and read local attempt evidence."""

import json
import os
import re
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
OPTIONAL_FIELDS = {Outcome.NEEDS_INPUT: ("proposed_default",)}
# A label such as "A)", "(b)", "1." or "2)" that starts one choice inside a string.
_CHOICE_LABEL = re.compile(r"(?:^|(?<=\s))\(?([A-Za-z]|\d{1,2})[).:](?=\s)")
_BULLET = re.compile(r"^\s*(?:[-*\u2022]\s+)")


def split_options(value: object) -> list[str]:
    """Return one string per choice, splitting a string only when its choices are unambiguous.

    A string that cannot be split stays one option, so the question still reaches the owner.
    Raise ValueError only for a missing, empty or non-string option.
    """
    if isinstance(value, list):
        options = value
    elif isinstance(value, str):
        options = [_BULLET.sub("", line) for line in value.splitlines() if line.strip()]
        if len(options) == 1:
            labels = list(_CHOICE_LABEL.finditer(options[0]))
            names = [label.group(1).lower() for label in labels]
            first = names[0] if names else ""
            sequence = "abcdefghijklmnopqrstuvwxyz" if first == "a" else [str(n) for n in range(1, 100)]
            if (len(labels) >= 2 and labels[0].start() == len(options[0]) - len(options[0].lstrip())
                    and names == list(sequence[:len(names)])):
                ends = [label.start() for label in labels[1:]] + [len(options[0])]
                options = [options[0][label.start():end] for label, end in zip(labels, ends)]
    else:
        raise ValueError("Options must be a list of strings")
    options = [option.strip() if isinstance(option, str) else option for option in options]
    if not options or any(not isinstance(option, str) or not option for option in options):
        raise ValueError("Each option must be a nonempty string")
    return options


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


class WorkerSession(TypedDict):
    project_id: str
    title: str
    prompt: str


@dataclass(frozen=True, slots=True)
class WorkerResult:
    """Schema-valid evidence attributed to a packet by exact field matches."""

    packet: WorkPacket
    outcome: Outcome
    details: dict[str, str]


class InvalidResult(ValueError):
    """A result exists or could not be read, but is unusable as evidence."""


@dataclass(frozen=True, slots=True)
class WorkerHandoff:
    """Use a runner-controlled result directory with trusted, stable ancestors.

    The runner supplies a verified worktree and assignment. The bridge resolves
    the project and launches the worker session after authorization and durable
    launch intent. This class performs neither operation.
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

    def launch_arguments(self, project_id: str) -> WorkerSession:
        """Render the runtime-neutral worker session for a project-local launch."""
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
            + (f" (optional: {', '.join(OPTIONAL_FIELDS[outcome])})" if outcome in OPTIONAL_FIELDS else "")
            for outcome, fields in OUTCOME_FIELDS.items()
        )
        return {
            "project_id": project_id,
            "title": f"Phalanx worker [{packet.marker}]",
            "prompt": f"""Phalanx attempt marker: {packet.marker}

Execute only the assigned stage in the packet below. Scope is authorized
repository development, maintenance, documentation, testing, or defensive
review. Do not perform offensive or exploit workflows. Read the applicable
repository instructions, run relevant checks, and leave GitHub writes to the
runner.

The session may start in the project checkout. Use the absolute worktree path in
the packet as the explicit working directory for every repository command and
target all repository edits there. Before writing, verify the Git root,
current branch, and HEAD against the packet. If you cannot verify or target
the worktree, report the workspace blocker and leave repository files untouched.

Work packet:
{assignment}

Write one UTF-8 JSON object of at most {MAX_RESULT_BYTES} bytes. Copy every
result_identity field verbatim to the top level, including expected_revision
even if the stage creates a new commit. Add only "outcome" and "details".
Choose one outcome and provide its exact details fields as nonempty strings,
except needs_input options: a JSON array with each choice as its own string
element, never several choices in one string:
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
        if not isinstance(result, dict):
            raise InvalidResult("Result must be a JSON object")
        # Extra fields are harmless and ignored; identity and required details must match.
        for key, expected in identity.items():
            value = result.get(key)
            if type(value) is not type(expected) or value != expected:
                raise InvalidResult(f"Result does not match the packet's {key}")
        try:
            outcome = Outcome(result.get("outcome"))
        except ValueError as exc:
            raise InvalidResult("Unknown worker outcome") from exc
        details = result.get("details")
        fields = OUTCOME_FIELDS[outcome]
        if not isinstance(details, dict) or any(
            not isinstance(details.get(field), str) or not details[field].strip()
            for field in fields if field != "options"
        ):
            raise InvalidResult(f"Details for {outcome} need nonempty strings: {', '.join(fields)}")
        optional = [field for field in OPTIONAL_FIELDS.get(outcome, ())
                    if isinstance(details.get(field), str) and details[field].strip()]
        kept = {field: details[field] for field in (*fields, *optional)}
        if "options" in kept:
            try:
                kept["options"] = split_options(kept["options"])
            except ValueError as exc:
                raise InvalidResult(f"needs_input options: {exc}") from exc
        return WorkerResult(self.packet, outcome, kept)


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

"""Runner-owned first draft publication and recovery on one device."""

from collections.abc import Callable
from contextlib import closing
from dataclasses import asdict, dataclass, replace
import json
from pathlib import Path
import re
import sqlite3
import time
from typing import Literal

from .git_publication import GitPublication, PublicationError
from .github import GitHubClient, GitHubError, PullRequest, UncertainGitHubWrite
from .workspaces import WorkspaceError, _file_lock, _item_branch


@dataclass(frozen=True)
class DraftIntent:
    item_id: str
    repository: str
    head: str
    candidate: str
    expected_head: str | None
    base: str
    expected_base: str
    title: str
    body: str


@dataclass(frozen=True)
class PublicationClaim:
    attempt_id: str
    state_version: int


@dataclass(frozen=True)
class DraftOutcome:
    status: Literal["verified", "conflict", "base_changed", "pending", "claim_lost"]
    reason: str
    primary: PullRequest | None = None
    duplicates: tuple[PullRequest, ...] = ()
    conflicts: tuple[PullRequest, ...] = ()
    observed_base: str | None = None


class _ClaimLost(Exception):
    pass


class DraftPublisher:
    """Drive first publication using verified inputs from the runner.

    All local attempts share a runner-owned journal directory. Give a successor
    the same immutable intent and its own claim. The caller records verified
    outcomes through the server's claim/version guard.
    """

    def __init__(
        self, journal: Path, git: GitPublication, github: GitHubClient, *,
        check_claim: Callable[[DraftIntent, PublicationClaim], bool],
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.journal = Path(journal).resolve()
        self.git = git
        self.github = github
        self.check_claim = check_claim
        self.sleep = sleep

    def publish(self, intent: DraftIntent, claim: PublicationClaim) -> DraftOutcome:
        self._validate(intent, claim)
        item = _item_branch(intent.item_id).rsplit("/", 1)[1]

        def authorize():
            if self.check_claim(intent, claim) is not True:
                raise _ClaimLost

        try:
            authorize()
            self.journal.mkdir(parents=True, exist_ok=True)
            with _file_lock(self.journal / f"{item}.lock"):
                with closing(sqlite3.connect(self.journal / "draft-publication.sqlite3")) as db:
                    return self._publish(db, intent, claim, authorize)
        except _ClaimLost:
            return DraftOutcome("claim_lost", "Publication needs a current runner claim")
        except (PublicationError, GitHubError, WorkspaceError) as exc:
            return DraftOutcome("pending", str(exc))
        except (OSError, sqlite3.Error):
            return DraftOutcome("pending", "Publication journal is unavailable. Retry with the same intent")

    def _validate(self, intent: DraftIntent, claim: PublicationClaim) -> None:
        repository = intent.repository.casefold()
        if (
            repository != self.github.repository.casefold()
            or self.git.expected_remote != ("github", repository)
            or intent.head != _item_branch(intent.item_id)
            or self.git.ref != f"refs/heads/{intent.head}"
        ):
            raise ValueError("Publication requires the registered GitHub repo and exact item branch")
        if (
            not intent.base or intent.base == intent.head or not intent.title.strip()
            or not claim.attempt_id or type(claim.state_version) is not int or claim.state_version < 0
        ):
            raise ValueError("Publication requires an explicit base, title, and claim")
        revisions = [intent.candidate, intent.expected_base]
        if intent.expected_head is not None:
            revisions.append(intent.expected_head)
        if any(not re.fullmatch(r"[0-9a-f]{40}", revision) for revision in revisions):
            raise ValueError("Publication requires full lowercase GitHub commit SHAs")

    def _publish(self, db, intent, claim, authorize) -> DraftOutcome:
        db.execute("PRAGMA synchronous=EXTRA")
        db.execute("""
            CREATE TABLE IF NOT EXISTS draft_publication (
                item_id TEXT PRIMARY KEY,
                intent TEXT NOT NULL,
                claim TEXT NOT NULL,
                phase TEXT NOT NULL,
                known_number INTEGER,
                verified_number INTEGER
            )
        """)
        row = db.execute(
            "SELECT intent, phase, known_number, verified_number "
            "FROM draft_publication WHERE item_id = ?", (intent.item_id,),
        ).fetchone()
        if row is not None and row[0] != json.dumps(asdict(intent), sort_keys=True):
            previous = DraftIntent(**json.loads(row[0]))
            if intent.expected_head is None and replace(
                intent, expected_head=previous.expected_head,
            ) == previous:
                # Retry a superseding intent under its journaled lease.
                intent = previous
            elif (
                row[1] == "verified" or row[2] is not None
                or intent.head != previous.head or intent.base != previous.base
                or intent.repository.casefold() != previous.repository.casefold()
            ):
                return DraftOutcome("conflict", "Reconcile the existing journaled publication intent first")
            else:
                # No draft is known, so a new candidate (for example after a
                # rebase) may replace the item ref this journal last wrote. The
                # first observation below still routes any late draft.
                remote = self.git.read_head(authorize=authorize)
                if remote not in (previous.candidate, previous.expected_head) or (
                    intent.expected_head not in (None, remote)
                ):
                    return DraftOutcome("conflict", "The remote item ref differs from the journaled publication")
                intent = replace(intent, expected_head=remote)
                row = None
        payload = json.dumps(asdict(intent), sort_keys=True)
        claim_payload = json.dumps(asdict(claim))
        phase, known, primary = row[1:] if row is not None else ("new", None, None)

        def record(next_phase, *, known_number=None, verified_number=None):
            with db:
                db.execute("""
                    INSERT INTO draft_publication VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(item_id) DO UPDATE SET
                        intent = excluded.intent, claim = excluded.claim, phase = excluded.phase,
                        known_number = excluded.known_number,
                        verified_number = excluded.verified_number
                """, (intent.item_id, payload, claim_payload, next_phase,
                      known_number, verified_number))

        def observe():
            return self._observe(intent, authorize, primary or known)

        def finish(observation):
            if observation.status == "verified" and observation.primary is not None:
                record("verified", verified_number=observation.primary.number)
            return observation

        # Recover a known/possible draft before any remote mutation. Once a push
        # was observed, a later deletion must go through reconciliation.
        if phase in ("draft", "verified"):
            observation = observe()
            if observation is not None:
                return finish(observation)
            for delay in (0.1, 0.25):
                self.sleep(delay)
                observation = observe()
                if observation is not None:
                    return finish(observation)
        else:
            observation = observe()
            if observation is not None:
                return finish(observation)

        head = self.git.read_head(authorize=authorize)
        if phase == "push" and head == intent.candidate:
            record("pushed")
            phase = "pushed"
        moved = self._check_base(intent, authorize)
        if moved is not None:
            if phase == "verified" or head not in (intent.candidate, intent.expected_head):
                return DraftOutcome("conflict", "The base moved and the remote item ref differs from the publication intent")
            return moved
        if head != intent.candidate:
            if phase in ("pushed", "draft", "verified") or head != intent.expected_head:
                return DraftOutcome("conflict", "The remote item ref differs from the publication intent")
            record("push")
            # The lease allows replacing only the journaled remote commit, so a
            # rebased candidate may be published without a fast-forward.
            result = self.git.publish(
                intent.candidate, expected=intent.expected_head, authorize=authorize, rewrite=True,
            )
            if result.status == "conflict":
                return DraftOutcome("conflict", result.reason)
            if result.status == "success":
                record("pushed")
                phase = "pushed"
            else:
                head = self.git.read_head(authorize=authorize)
                if head == intent.expected_head:
                    return DraftOutcome("pending", "Push is unconfirmed. Reconcile the same intent on retry")
                if head != intent.candidate:
                    return DraftOutcome("conflict", "The remote item ref moved during publication")
        if phase not in ("draft", "verified"):
            if phase != "pushed":
                record("pushed")
            observation = observe()
            if observation is not None:
                return finish(observation)

        # Each call can issue one POST and one bounded retry after positive
        # evidence that the lookup completed without finding a matching PR.
        for _ in range(2):
            moved = self._check_base(intent, authorize)
            if moved is not None:
                return moved
            if self.git.read_head(authorize=authorize) != intent.candidate:
                return DraftOutcome("conflict", "The remote item ref moved before draft creation")
            record("draft")
            phase = "draft"
            authorize()
            try:
                created = self.github.create_draft(
                    head=intent.head, base=intent.base, title=intent.title, body=intent.body,
                )
            except GitHubError as exc:
                if not isinstance(exc, UncertainGitHubWrite) and exc.status != 422:
                    return DraftOutcome("pending", str(exc))
            else:
                known = created.number
                record("draft", known_number=known)
            for delay in (0, 0.1, 0.25):
                if delay:
                    self.sleep(delay)
                observation = observe()
                if observation is not None:
                    return finish(observation)
        return DraftOutcome("pending", "Draft creation is unconfirmed. Reconcile the same intent on retry")

    def _check_base(self, intent, authorize) -> DraftOutcome | None:
        observed = self.git.read_branch(intent.base, authorize=authorize)
        if observed != intent.expected_base:
            return DraftOutcome("base_changed", "The remote base differs from the recorded revision",
                                observed_base=observed)
        return None

    def _observe(self, intent, authorize, preferred_number) -> DraftOutcome | None:
        options = {
            "head_owner": intent.repository.split("/")[0], "head": intent.head,
            "authorize": authorize, "page_limit": 10,
        }
        numbers = {preferred_number} if preferred_number is not None else set()
        for base in (intent.base, None):
            numbers.update(pull.number for pull in self.github.list_matching_pull_requests(
                base=base, **options,
            ))
        if not numbers:
            return None

        repository = intent.repository.casefold()
        matches, conflicts = [], []
        for number in sorted(numbers):
            authorize()
            pull = self.github.get_pull_request(number)
            if (
                (pull.head_repository or "").casefold() != repository
                or pull.base_repository.casefold() != repository
                or pull.head_ref != intent.head
                or pull.base_ref != intent.base
                or pull.head_sha.lower() != intent.candidate
                or pull.base_sha.lower() != intent.expected_base
                or pull.state != "open" or not pull.draft or pull.merged is not False
            ):
                conflicts.append(pull)
            else:
                matches.append(pull)
        primary = next(
            (pull for pull in matches if pull.number == preferred_number),
            matches[0] if matches else None,
        )
        duplicates = tuple(pull for pull in matches if pull != primary)
        if conflicts:
            return DraftOutcome("conflict", "Found a PR that differs from the draft intent",
                                primary, duplicates, tuple(conflicts))
        if self.git.read_branch(intent.base, authorize=authorize) != intent.expected_base:
            return DraftOutcome("conflict", "The remote base moved while verifying the draft",
                                primary, duplicates)
        if self.git.read_head(authorize=authorize) != intent.candidate:
            return DraftOutcome("conflict", "The remote item ref moved while verifying the draft",
                                primary, duplicates)
        authorize()
        return DraftOutcome("verified", "Verified the draft and its remote revisions", primary, duplicates)

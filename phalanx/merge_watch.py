"""Runner detection of a primary PR merged by the owner, GitHub or an earlier Phalanx write.

The runner reads the PR with the same verifier check-in uses and reports a
merge to the server, which closes the execution as delivered or surfaces a
blocker. It runs at two existing boundaries: just after a stage is claimed,
before its work starts, and on the Captain loop's control lane for every
current execution with a primary PR. Forge errors back off and never count as
a merge.
"""

import os
import re
from time import monotonic
from urllib.parse import quote

from .runner_errors import RunnerError
from .check_in import CheckInOutcome, observe_merge
from .github import GitHubClient
from .pr_models import GitHubPull, MergedPull, MergeObservation

PR_URL = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/([1-9][0-9]{0,18})")
# Check-in verifies its own merge, so only other stages are watched.
WATCHED_STAGES = frozenset({"development", "self_review", "pr_revision", "adversarial_review",
                            "pre_pr_validation", "pr_promotion", "pr_validation", "pr_reconciliation"})
# The stop dispositions a merge sets: delivered, or the owner's provenance blocker.
MERGE_STOPS = frozenset({"completed", "waiting_for_input"})
# Detection latency target: a merge is seen within one watch interval plus one
# Captain heartbeat (about three minutes by default) while GitHub answers.
WATCH_SECONDS = 120
MAX_BACKOFF_SECONDS = 900
PAGE = 50


def observe(token: str, url: str, head_ref: str, base_ref: str | None, heads=()) -> CheckInOutcome:
    """Read the PR with check-in's verifier. ``merged`` or ``mismatch`` means GitHub merged it."""
    match = PR_URL.fullmatch(url)
    if match is None:
        return CheckInOutcome("unresolved", f"{url} is not a github.com pull request URL")
    github = GitHubClient(match[1], match[2], token=token)
    return observe_merge(github, int(match[3]), url, head_ref, base_ref, heads=set(heads))


def report(api, item_id: str, execution_id: str, version: int, outcome: CheckInOutcome, detected_by: str):
    """Send one merge read; the key and server dedupe make repeats harmless."""
    pull = outcome.pull
    body = MergeObservation(
        expected_version=version, detected_by=detected_by, merged_at=pull.merged_at,
        pull=MergedPull.model_validate({**{field: getattr(pull, field) for field in GitHubPull.model_fields},
                                        "merge_commit": pull.merge_commit_sha}),
    ).model_dump(mode="json")
    route = f"/items/{quote(item_id, safe='')}/executions/{quote(execution_id, safe='')}/pr-merged"
    return api.call(route, body, key=f"merge:{execution_id}:{version}:{pull.merge_commit_sha}")


def merged(outcome: CheckInOutcome) -> bool:
    return outcome.status in {"merged", "mismatch"} and outcome.pull is not None


class MergeWatch:
    """Bounded per-execution reads with explicit backoff, run on the Captain's control lane."""

    def __init__(self, api, repositories, *, interval=WATCH_SECONDS, token=None, read=observe, clock=monotonic):
        self.api, self.repositories = api, set(repositories)
        self.interval, self.read, self.clock = interval, read, clock
        self.token = token or (lambda: os.environ.get("PHALANX_GITHUB_TOKEN"))
        self.due: dict[str, tuple[float, int]] = {}

    def sweep(self):
        """Read each watched PR whose turn has come; return merges reported, raise listed errors."""
        token = self.token()
        if not token:
            raise RunnerError("Set PHALANX_GITHUB_TOKEN so the runner can detect merged primary PRs")
        reported, errors, seen, after = [], [], set(), ""
        while True:
            rows = self.api.call(f"/runner/merge-watch?limit={PAGE}&after={quote(after, safe='')}")
            for row in rows:
                seen.add(row["execution_id"])
                if row["repository_id"] in self.repositories:
                    self._check(token, row, reported, errors)
            if len(rows) < PAGE:
                break
            after = rows[-1]["execution_id"]
        self.due = {key: value for key, value in self.due.items() if key in seen}
        if errors:
            raise RunnerError("; ".join(errors))
        return reported

    def _check(self, token, row, reported, errors):
        now, execution = self.clock(), row["execution_id"]
        due, failures = self.due.get(execution, (0, 0))
        if now < due:
            return
        outcome = None
        try:
            outcome = self.read(token, row["primary_pr"], row["head_ref"], row["target_branch"], row["heads"])
            if outcome.pull is None and outcome.status == "pending":
                raise RunnerError(outcome.reason)
            if merged(outcome):
                try:
                    report(self.api, row["item_id"], execution, row["version"], outcome, "watch")
                except RunnerError as error:
                    # The execution moved on; the next listing shows its current version.
                    if "HTTP 409" not in str(error):
                        raise
                reported.append({"item_id": row["item_id"], "execution_id": execution,
                                 "status": outcome.status, "reason": outcome.reason})
        except Exception as error:
            failures += 1
            retry = outcome.retry_after_seconds if outcome is not None else None
            delay = max(retry or 0, min(MAX_BACKOFF_SECONDS, self.interval * 2 ** (failures - 1)))
            self.due[execution] = (now + delay, failures)
            errors.append(f"{row['item_id']}: {error}; next merge read in {delay:.0f}s")
            return
        self.due[execution] = (now + self.interval, 0)

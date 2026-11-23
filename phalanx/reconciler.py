"""Background reconciler: read GitHub and git, post observations, and run trusted pre-PR checks.

It never changes an execution's state and never blocks work. The server derives
flags from what it posts. Local check results are reported, never gated on.
"""

from datetime import UTC, datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .github import GitHubClient, GitHubError
from .github_checks import collect_github_checks
from .local_checks import CheckCommandError, run_check
from .pr_models import GITHUB_REMOTE
from .transitions import item_branch
from .validation import Phase, PreOutcome
from .validation_policy import ValidationPolicyError, load_validation_policy

PASS_SECONDS = 120
HEARTBEAT_SECONDS = 60
# GitHub may rate-limit without saying for how long.
BACKOFF_SECONDS = 60
MAX_REASON = 4000
_HIDDEN = {"PHALANX_RUNNER_TOKEN", "PHALANX_OWNER_TOKEN", "PHALANX_GITHUB_TOKEN"}


class ReconcilerError(RuntimeError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class API:
    """Phalanx API calls with the runner token."""

    def __init__(self, base_url, token):
        url = urlsplit(base_url)
        if (not url.hostname or url.username or url.password or url.query or url.fragment
                or (url.scheme != "https" and not (
                    url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}))):
            raise ReconcilerError("Use HTTPS or a loopback HTTP API URL without credentials")
        if not token:
            raise ReconcilerError("Set PHALANX_RUNNER_TOKEN for the reconciler")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.opener = build_opener(_NoRedirect())

    def call(self, path, body=None):
        headers = {"Authorization": f"Bearer {self.token}"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        request = Request(self.base_url + "/v1" + path, data=data, headers=headers,
                          method="GET" if body is None else "POST")
        try:
            with self.opener.open(request, timeout=30) as response:
                return json.load(response)
        except HTTPError as error:
            with error:
                detail = error.read()[:500].decode("utf-8", errors="replace")
            raise ReconcilerError(f"Phalanx API returned HTTP {error.code} for {path}: {detail}") from None
        except (URLError, TimeoutError, ValueError) as error:
            raise ReconcilerError(f"Phalanx API unavailable for {path}: {error}") from None


def git(path, *args, check=True):
    # Inherited Git overrides can redirect operations away from the configured checkout.
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    try:
        result = subprocess.run(["git", "--no-optional-locks", "-c", "core.fsmonitor=false", *args],
                                cwd=path, env=env, stdin=subprocess.DEVNULL, capture_output=True)
    except OSError as error:
        raise ReconcilerError(f"Cannot run Git in {path}: {error}") from None
    if check and result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ReconcilerError(f"Git {args[0]} failed in {path}: {detail}")
    return result


def _clip(text):
    return text[:MAX_REASON].strip() or "No detail"


class Reconciler:
    def __init__(self, config, state, api, *, github_token, notify=None,
                 clock=time.monotonic, github_client=GitHubClient):
        self.config = config
        self.state = Path(state).resolve()
        self.api = api
        self.github_token = github_token
        self.notify = notify
        self.clock = clock
        self.github_client = github_client
        self.backoff = {}
        self.notified = set()
        self.last_heartbeat = None

    def run_pass(self):
        """Observe every open item this host maps, run any unrecorded local checks, and notify the owner."""
        errors, jobs = [], []
        self.heartbeat(force=True)
        remotes = {repo["id"]: repo["remote"] for repo in self.api.call("/repositories")}
        fetched, policies = {}, {}
        observed = 0
        for execution in self.open_executions():
            self.heartbeat()
            repo = execution["repository_id"]
            if repo not in self.config.get("repositories", {}) or self.backoff.get(repo, 0) > self.clock():
                continue
            try:
                posted, job = self.observe(execution, remotes.get(repo, ""), fetched, policies)
            except GitHubError as error:
                if error.retry_after_seconds is not None:
                    # A rate-limited repo waits; other repos keep going.
                    self.backoff[repo] = self.clock() + max(error.retry_after_seconds, BACKOFF_SECONDS)
                errors.append(f"{execution['item_id']}: {error}")
                continue
            except (ReconcilerError, ValidationPolicyError, ValueError) as error:
                errors.append(f"{execution['item_id']}: {error}")
                continue
            observed += posted
            if job is not None:
                jobs.append(job)
        # Checks run after observing, so a long suite never delays an observation in this pass.
        for path, body, checkout, checks in jobs:
            try:
                failures = self.run_checks(checkout, body["pr"]["head_sha"], checks)
                self.api.call(path, dict(body, observed_at=datetime.now(UTC).isoformat(), local_checks={
                    "head": body["pr"]["head_sha"], "failures": failures}))
            except ReconcilerError as error:
                errors.append(f"{path}: {error}")
        try:
            self.notify_owner()
        except ReconcilerError as error:
            errors.append(f"flags: {error}")
        return {"observed": observed, "checked": len(jobs), "errors": errors}

    def heartbeat(self, force=False):
        now = self.clock()
        if force or self.last_heartbeat is None or now - self.last_heartbeat >= HEARTBEAT_SECONDS:
            self.api.call(f"/reconcilers/{quote(self.config['runner_id'], safe='')}/heartbeat", {})
            self.last_heartbeat = now

    def open_executions(self):
        cursor = None
        while True:
            query = {"kind": "open", "limit": 100, **({"cursor": cursor} if cursor else {})}
            page = self.api.call("/operator/executions?" + urlencode(query))
            yield from page["items"]
            cursor = page["next_cursor"]
            if not cursor:
                return

    def observe(self, execution, remote, fetched, policies):
        """Post what GitHub and git show for one execution. Return (posted, local check job or None)."""
        item_id, repo = execution["item_id"], execution["repository_id"]
        item = self.api.call(f"/items/{quote(item_id, safe='')}")
        if item["target"] == "report":
            return 0, None
        match = GITHUB_REMOTE.fullmatch(remote)
        if match is None:
            raise ReconcilerError(f"{repo} has no registered github.com remote")
        owner, name = match[1].split("/")
        github = self.github_client(owner, name, token=self.github_token)
        branch = item_branch(item_id)
        pull = self.find_pull(github, owner, branch, execution.get("primary_pr_url"))
        body = {"observed_at": datetime.now(UTC).isoformat()}
        checks = ()
        if pull is None:
            checkout = self.checkout(repo, remote, fetched)
            if git(checkout, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}",
                   check=False).returncode:
                return 0, None
            base = item.get("target_branch") or self.config["repositories"][repo]["base_branch"]
            body["merged_branch"] = git(
                checkout, "merge-base", "--is-ancestor", f"refs/remotes/origin/{branch}",
                f"refs/remotes/origin/{base}", check=False,
            ).returncode == 0
        else:
            body["pr"] = {
                "number": pull.number, "state": pull.state, "merged": bool(pull.merged), "draft": pull.draft,
                "head_ref": pull.head_ref, "head_sha": pull.head_sha, "base_ref": pull.base_ref,
                "base_sha": pull.base_sha, "merge_commit": pull.merge_commit_sha, "mergeable": pull.mergeable,
            }
            if pull.state == "open":
                policy = self.policy(repo, policies)
                required = policy and (policy.draft_checks if pull.draft else policy.ready.required)
                # None means the policy doesn't establish this phase's checks, so there is nothing to report.
                if required is not None:
                    collected = collect_github_checks(
                        github, pull, Phase.DRAFT if pull.draft else Phase.READY, required,
                        read_preview=None if pull.draft else policy.ready.read_preview)
                    # A retry means the PR or base moved mid-read; the next pass reads it afresh.
                    if collected.retry_after_seconds is None:
                        body["checks"] = collected.gate.state
                        body["check_reasons"] = [_clip(reason) for reason in collected.gate.reasons[:20]]
                if policy is not None:
                    checks = tuple(check for check in policy.local_checks or () if check.disposition is None)
                body["feedback"] = [_clip(text) for _, text in github.get_actionable_feedback(pull.number)[:50]]
        path = f"/items/{quote(item_id, safe='')}/executions/{quote(execution['id'], safe='')}/github-observation"
        result = self.api.call(path, body)
        if not checks or result["local_checks_recorded"]:
            return 1, None
        return 1, (path, body, self.checkout(repo, remote, fetched), checks)

    def policy(self, repo, policies):
        if not self.config.get("validation_policy"):
            return None
        if repo not in policies:
            policies[repo] = load_validation_policy(Path(self.config["validation_policy"]), repo)
        return policies[repo]

    @staticmethod
    def find_pull(github, owner, branch, primary):
        if primary:
            return github.get_pull_request(int(primary.rstrip("/").rsplit("/", 1)[1]))
        pulls = github.list_matching_pull_requests(head_owner=owner, head=branch)
        # The open PR, else the newest closed one.
        return max(pulls, key=lambda pull: (pull.state == "open", pull.number), default=None)

    def checkout(self, repo, remote, fetched):
        """Verify the mapped checkout's origin is the registered remote, and fetch it once a pass."""
        if repo in fetched:
            return fetched[repo]
        checkout = Path(self.config["repositories"][repo]["checkout"]).resolve()
        top = git(checkout, "rev-parse", "--show-toplevel").stdout.decode().strip()
        # The configured URL, before any local insteadOf rewrite, names the repository.
        urls = git(checkout, "config", "--get-all", "remote.origin.url", check=False).stdout.decode().split()
        origin = GITHUB_REMOTE.fullmatch(urls[0]) if len(urls) == 1 else None
        expected = GITHUB_REMOTE.fullmatch(remote)
        if (Path(top).resolve() != checkout or origin is None or expected is None
                or origin[1].lower() != expected[1].lower()):
            raise ReconcilerError(f"{checkout} is not a checkout of the registered remote for {repo}")
        if self.state.is_relative_to(checkout):
            raise ReconcilerError("The reconciler state directory must be outside the mapped checkout")
        git(checkout, "fetch", "--no-tags", "--prune", "origin")
        fetched[repo] = checkout
        return checkout

    def run_checks(self, checkout, head, checks):
        """Run trusted checks serially in a scratch worktree of the head. Return the failures."""
        scratch = self.state / "checks" / head[:16]
        self.remove_scratch(checkout, scratch)
        scratch.parent.mkdir(parents=True, exist_ok=True)
        if git(checkout, "cat-file", "-e", f"{head}^{{commit}}", check=False).returncode:
            git(checkout, "fetch", "--no-tags", "origin", head)
        git(checkout, "worktree", "add", "--detach", str(scratch), head)
        try:
            failures = []
            for check in checks:
                self.heartbeat()
                try:
                    outcome, reason, _ = run_check(check, scratch)
                except CheckCommandError as error:
                    outcome, reason = PreOutcome.FAILED, str(error)
                if outcome != PreOutcome.PASSED:
                    failures.append(_clip(f"{check.policy.name}: {reason}"))
            return failures
        finally:
            self.remove_scratch(checkout, scratch)

    @staticmethod
    def remove_scratch(checkout, scratch):
        if not os.path.lexists(scratch):
            return
        if git(checkout, "worktree", "remove", "--force", str(scratch), check=False).returncode:
            shutil.rmtree(scratch, ignore_errors=True)
            git(checkout, "worktree", "prune")

    def notify_owner(self):
        if self.notify is None:
            return
        for flag in self.api.call("/flags")["flags"]:
            key = (flag["kind"], flag["item_id"], flag["execution_id"], flag["captain_id"], flag["first_seen"])
            if not flag["notify_owner"] or key in self.notified:
                continue
            try:
                self.notify({"event": "flag", **flag})
            except (OSError, subprocess.SubprocessError) as error:
                # Not marked notified, so the next pass retries it.
                raise ReconcilerError(f"notify_command failed: {error}") from None
            self.notified.add(key)

    def run(self, stop, interval=PASS_SECONDS, once=False):
        while True:
            try:
                result = self.run_pass()
            except (ReconcilerError, GitHubError) as error:
                result = {"observed": 0, "checked": 0, "errors": [str(error)]}
            print(json.dumps({"at": datetime.now(UTC).isoformat(), **result}), flush=True)
            if once or stop.wait(interval):
                return result


def command_notifier(argv):
    """Send each owner notification as one JSON event on the stdin of a trusted command."""
    if not argv:
        return None

    def notify(event):
        subprocess.run(argv, input=json.dumps(event).encode(), stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=30, check=True,
                       env={key: value for key, value in os.environ.items() if key.upper() not in _HIDDEN})
    return notify


def add_reconcile_command(commands):
    parser = commands.add_parser(
        "reconcile", help="observe GitHub and git for open items, run pre-PR checks, and notify on flags")
    parser.add_argument("--config", required=True, type=Path, help="runner configuration JSON")
    parser.add_argument("--state", required=True, type=Path, help="scratch directory outside every checkout")
    parser.add_argument("--interval-seconds", type=float, default=PASS_SECONDS)
    parser.add_argument("--once", action="store_true", help="run one pass and exit")


def run_reconcile(args):
    config = json.loads(args.config.read_text(encoding="utf-8-sig"))
    github_token = os.environ.get("PHALANX_GITHUB_TOKEN")
    if not github_token:
        raise ReconcilerError("Set PHALANX_GITHUB_TOKEN for the reconciler")
    reconciler = Reconciler(config, args.state, API(config["base_url"], os.environ.get("PHALANX_RUNNER_TOKEN")),
                            github_token=github_token, notify=command_notifier(config.get("notify_command")))
    try:
        result = reconciler.run(threading.Event(), args.interval_seconds, args.once)
    except KeyboardInterrupt:
        print("reconciler stopped", file=sys.stderr)
        return 0
    return 1 if result["errors"] else 0

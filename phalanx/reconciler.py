"""Background reconciler: read GitHub and git, post observations, and run trusted pre-PR checks.

It never changes an execution's state and never blocks work. The server derives
flags from what it posts. Local check results are reported, never gated on.
"""

from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from http.client import HTTPException
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .github import GitHubClient, GitHubError, merge_method
from .github_checks import collect_github_checks
from .local_checks import CheckCommandError, run_check
from .pr_models import DEPLOYMENT_WINDOW, GITHUB_REMOTE
from .transitions import item_branch
from .validation import Phase, PreOutcome
from .validation_policy import ValidationPolicyError, load_validation_policy
from .workspaces import WorkspaceError, any_file_lock

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
        # OSError covers URLError, timeouts and dropped connections while the server restarts.
        except (OSError, HTTPException, ValueError) as error:
            raise ReconcilerError(f"Phalanx API unavailable for {path}: {error!r}") from None


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
        for repo, mapping in config.get("repositories", {}).items():
            check = mapping.get("post_merge_check")
            if check is not None and (not isinstance(check, str) or not check.strip() or len(check) > 255):
                raise ReconcilerError(f"{repo}: post_merge_check must be a non-empty check-run name (up to 255 characters)")

    def run_pass(self):
        """Observe open work and opted-in deployments, run local checks, and notify the owner."""
        errors, jobs = [], []
        self.heartbeat(force=True)
        remotes = {repo["id"]: repo["remote"] for repo in self.api.call("/repositories")}
        fetched, policies = {}, {}
        observed = 0
        for execution in self.executions():
            self.heartbeat()
            try:
                repo = execution["repository_id"]
                if repo not in self.config.get("repositories", {}) or self.backoff.get(repo, 0) > self.clock():
                    continue
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
            except Exception as error:
                # Isolate unexpected item failures too; the pass log must identify them.
                errors.append(f"{execution['item_id']}: {type(error).__name__}: {error}")
                continue
            observed += posted
            if job is not None:
                jobs.append(job)
        # Checks run after observing, so a long suite never delays an observation in this pass.
        for path, head, checkout, checks in jobs:
            try:
                failures = self.run_checks(checkout, head, checks)
                self.api.call(path, {"head": head, "failures": failures})
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
            self.api.call(f"/reconcilers/{quote(self.config['reconciler_id'], safe='')}/heartbeat", {
                "deployment_checks": {repo: mapping.get("post_merge_check")
                                      for repo, mapping in self.config.get("repositories", {}).items()},
            })
            self.last_heartbeat = now

    @contextmanager
    def heartbeating(self):
        """Keep heartbeating while a long check runs, so the reconciler isn't flagged stale."""
        stop = threading.Event()

        def beat():
            while not stop.wait(HEARTBEAT_SECONDS):
                try:
                    self.heartbeat(force=True)
                except ReconcilerError as error:
                    print(f"reconciler heartbeat failed: {error}", file=sys.stderr, flush=True)
        thread = threading.Thread(target=beat, name="reconciler-heartbeat", daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join()

    def executions(self):
        yield from self.execution_pages("open")
        for execution in self.execution_pages("closed", outcome="ready_for_check_in"):
            if execution.get("stage") == "delivery" and execution.get("primary_pr_url"):
                yield execution
        if any(mapping.get("post_merge_check") for mapping in self.config.get("repositories", {}).values()):
            cutoff = datetime.now(UTC) - DEPLOYMENT_WINDOW
            for execution in self.execution_pages("closed"):
                # Closed pages are newest first; don't walk historical pages.
                if datetime.fromisoformat(execution["closed_at"]) <= cutoff:
                    break
                mapping = self.config["repositories"].get(execution["repository_id"], {})
                deployment = execution.get("deployment") or {}
                if (execution.get("outcome") == "checked_in" and mapping.get("post_merge_check")
                        and not (deployment.get("state") == "deployed"
                                 and deployment.get("check_name") == mapping["post_merge_check"])):
                    yield execution

    def execution_pages(self, kind, **filters):
        cursor = None
        while True:
            query = {"kind": kind, "limit": 100, **filters, **({"cursor": cursor} if cursor else {})}
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
        mapping = self.config["repositories"][repo]
        base = item.get("target_branch") or mapping["base_branch"]
        if execution.get("outcome") == "checked_in" and base != mapping["base_branch"]:
            return 0, None
        match = GITHUB_REMOTE.fullmatch(remote)
        if match is None:
            raise ReconcilerError(f"{repo} has no registered github.com remote")
        owner, name = match[1].split("/")
        github = self.github_client(owner, name, token=self.github_token)
        branch = item_branch(item_id)
        primary = execution.get("primary_pr_url")
        if execution.get("outcome") == "checked_in":
            primary = primary or execution["output"]["url"]
        pull, duplicate_count = self.find_pull(github, owner, branch, primary)
        body = {"observed_at": datetime.now(UTC).isoformat()}
        checks = ()
        if pull is None:
            if execution.get("outcome") == "ready_for_check_in":
                raise ReconcilerError("Completed ready execution's primary PR was not found")
            checkout = self.checkout(repo, remote, fetched)
            # With no remote branch there is nothing to flag, but posting keeps the observation fresh.
            if not git(checkout, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}",
                       check=False).returncode:
                body["merged_branch"] = git(
                    checkout, "merge-base", "--is-ancestor", f"refs/remotes/origin/{branch}",
                    f"refs/remotes/origin/{base}", check=False,
                ).returncode == 0
        else:
            body["target_base"] = base
            body["duplicate_count"] = duplicate_count
            body["pr"] = {
                "number": pull.number, "state": pull.state, "merged": bool(pull.merged), "draft": pull.draft,
                "head_ref": pull.head_ref, "head_sha": pull.head_sha, "base_ref": pull.base_ref,
                "base_sha": pull.base_sha, "merge_commit": pull.merge_commit_sha, "mergeable": pull.mergeable,
                "additions": pull.additions, "deletions": pull.deletions,
            }
            if pull.merged and pull.merge_commit_sha and pull.base_ref == base:
                # The server records how and when the PR merged with the delivery.
                if execution.get("outcome") != "checked_in":
                    body["pr"]["merge_method"], _ = merge_method(github, pull.merge_commit_sha, pull.head_sha, base)
                body["pr"]["merged_at"] = pull.merged_at
                if execution.get("outcome") == "ready_for_check_in":
                    verified_head = execution["output_revision"]["head"]
                    if pull.head_sha != verified_head and github.is_on_branch(verified_head, pull.head_sha):
                        body["pr"]["merged_head_ancestor"] = verified_head
                check = self.config["repositories"][repo].get("post_merge_check")
                # Deployment outages must not delay recording the merge itself.
                if check and execution.get("outcome") == "checked_in":
                    body["deployment"] = self.deployment(github, pull.merge_commit_sha, check)
            draft_only = item.get("execution", {}).get("state", {}).get("publication_mode") == "draft"
            if pull.state == "open" and not draft_only and execution.get("outcome") != "ready_for_check_in":
                policy = self.policy(repo, policies)
                required = policy and (policy.draft_checks if pull.draft else policy.ready.required)
                # None means the policy doesn't establish this phase's checks, so there is nothing to report.
                if required is not None:
                    collected = collect_github_checks(
                        github, pull, Phase.DRAFT if pull.draft else Phase.READY, required)
                    # An incomplete read (rate limit, transport error, or the PR moving) posts nothing,
                    # so the previous observation's checks stand until the repo's backoff ends.
                    if collected.retry_after_seconds is not None:
                        raise GitHubError("collect checks", "; ".join(collected.gate.reasons),
                                          retry_after_seconds=collected.retry_after_seconds)
                    body["checks"] = collected.gate.state
                    body["check_reasons"] = [_clip(reason) for reason in collected.gate.reasons[:20]]
                if policy is not None:
                    checks = policy.local_checks or ()
                body["feedback"] = [_clip(text) for _, text in self.feedback(github, pull.number)[:50]]
        execution_path = f"/items/{quote(item_id, safe='')}/executions/{quote(execution['id'], safe='')}"
        result = self.api.call(execution_path + "/github-observation", body)
        if not checks or result["local_checks_recorded"]:
            return 1, None
        return 1, (execution_path + "/local-checks", pull.head_sha, self.checkout(repo, remote, fetched), checks)

    @staticmethod
    def deployment(github, commit, name):
        checks = github.get_commit_checks(commit, include_statuses=False)
        runs = [run for run in checks.check_runs if run.get("name") == name]
        state = "pending"
        if runs:
            if any(not isinstance(run.get("id"), int) or run.get("head_sha") != commit
                   or not isinstance(run.get("status"), str) for run in runs):
                raise GitHubError("read deployment check", "invalid check-run response")
            latest = max(runs, key=lambda run: run["id"])
            if latest["status"] == "completed":
                if not isinstance(latest.get("conclusion"), str):
                    raise GitHubError("read deployment check", "completed check has no conclusion")
                state = "deployed" if latest["conclusion"] == "success" else "failed"
        return {"commit": commit, "check_name": name, "state": state}

    @staticmethod
    def feedback(github, number):
        try:
            return github.get_actionable_feedback(number)
        except GitHubError as error:
            # GraphQL reports rate limits and outages without a retry hint, so back the repo off.
            if error.status is None or error.status in {403, 429} or error.status >= 500:
                raise GitHubError("read review feedback", str(error), error.status,
                                  retry_after_seconds=BACKOFF_SECONDS) from None
            raise

    def policy(self, repo, policies):
        if not self.config.get("validation_policy"):
            return None
        if repo not in policies:
            policies[repo] = load_validation_policy(Path(self.config["validation_policy"]), repo)
        return policies[repo]

    @staticmethod
    def find_pull(github, owner, branch, primary):
        pulls = github.list_matching_pull_requests(head_owner=owner, head=branch)
        if primary:
            number = int(primary.rstrip("/").rsplit("/", 1)[1])
        else:
            # The open PR, else the newest closed one.
            listed = max(pulls, key=lambda pull: (pull.state == "open", pull.number), default=None)
            number = None if listed is None else listed.number
        # The list endpoint omits merged and mergeable, so re-read the PR for exact values.
        pull = None if number is None else github.get_pull_request(number)
        return pull, sum(other.state == "open" and other.number != pull.number for other in pulls) if pull else 0

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
        try:
            scratch.parent.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise ReconcilerError(f"Cannot create the check scratch directory: {error}") from None
        if git(checkout, "cat-file", "-e", f"{head}^{{commit}}", check=False).returncode:
            git(checkout, "fetch", "--no-tags", "origin", head)
        git(checkout, "worktree", "add", "--detach", str(scratch), head)
        try:
            failures = []
            for check in checks:
                self.heartbeat()
                try:
                    with self.heartbeating():
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
        """Run passes while holding the state directory, so no other reconciler removes its scratch."""
        with ExitStack() as stack:
            try:
                self.state.mkdir(parents=True, exist_ok=True)
                held = stack.enter_context(any_file_lock([self.state / "reconciler.lock"]))
            except (OSError, WorkspaceError) as error:
                raise ReconcilerError(f"Cannot lock the reconciler state directory: {error}") from None
            if held is None:
                raise ReconcilerError(f"Another reconciler is using {self.state}")
            return self._run(stop, interval, once)

    def _run(self, stop, interval, once):
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
    parser.add_argument("--config", type=Path, help="reconciler configuration JSON (otherwise discover muster)")
    parser.add_argument("--state", type=Path, help="scratch directory outside every checkout")
    parser.add_argument("--home", help="existing muster directory (otherwise PHALANX_HOME or user locator)")
    parser.add_argument("--interval-seconds", type=float, default=PASS_SECONDS)
    parser.add_argument("--once", action="store_true", help="run one pass and exit")


def run_reconcile(args):
    if args.config is None or getattr(args, "home", None) is not None:
        from .muster import direct_config
        home, args.config = direct_config("reconciler", getattr(args, "home", None))
        if args.state is None:
            args.state = home / "reconciler"
    if args.state is None:
        raise ReconcilerError("Pass --state with an explicit --config")
    config = json.loads(args.config.read_text(encoding="utf-8-sig"))
    if (not isinstance(config, dict)
            or any(not isinstance(config.get(key), str) or not config[key].strip()
                   for key in ("base_url", "reconciler_id"))
            or not isinstance(config.get("repositories"), dict)):
        raise ReconcilerError("Reconciler config needs base_url, reconciler_id and repositories")
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

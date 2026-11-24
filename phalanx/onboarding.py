"""Runner-side onboarding: do or verify every step that makes a repository dispatchable."""

import json
import os
from pathlib import Path

from .api_client import RunnerAPI
from .github import github_repository
from .runner_errors import RunnerError
from .workspaces import WorkspaceError, WorkspaceManager, _git

DONE, OK, MISSING, MANUAL = "done", "ok", "missing", "manual"


def discover_ready_checks(remote: str, base_branch: str, token: str | None) -> tuple[list | None, str]:
    """Propose ready checks from the CI that reported on the base branch tip."""
    repository = github_repository(remote)
    if repository is None:
        return None, "the remote is not a GitHub repository"
    if not token:
        return None, "PHALANX_GITHUB_TOKEN is not set"
    from .github import GitHubClient, GitHubError

    owner, name = repository.split("/")
    client = GitHubClient(owner, name, token=token)
    try:
        tip = client.get_branch_tip(base_branch)
        evidence = client.get_commit_checks(tip)
    except (GitHubError, ValueError) as error:
        return None, f"reading CI on {base_branch} failed: {error}"
    checks, seen = [], set()
    for run in evidence.check_runs:
        app = (run.get("app") or {}).get("id")
        identity = ("check_run", run.get("name"), app)
        if isinstance(run.get("name"), str) and identity not in seen:
            seen.add(identity)
            checks.append({"name": run["name"], "kind": "check_run", **({"app_id": app} if app else {})})
    for status in evidence.statuses:
        identity = ("status", status.get("context"), None)
        if isinstance(status.get("context"), str) and identity not in seen:
            seen.add(identity)
            checks.append({"name": status["context"], "kind": "status"})
    if not checks:
        return None, f"no CI reported on {base_branch} tip {tip[:12]}"
    return checks, f"{len(checks)} found on {base_branch} tip {tip[:12]}: " + ", ".join(
        check["name"] for check in checks)


def _write_json(path: Path, data: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def onboard(config_path: Path, state: Path, repository: str, *, remote=None, checkout=None,
            base_branch=None, project_id=None, host_id=None, owner_token=None, runner_token=None,
            github_token=None, discover=discover_ready_checks) -> dict:
    """Apply each runner-side onboarding step that has its inputs and report the rest.

    Never overwrites an existing registration, mapping field or validation entry
    the caller did not name. Owner trust in the worker runtime stays manual.
    """
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    mappings = config.setdefault("repositories", {})
    mapping = dict(mappings.get(repository) or {})
    steps = []

    def step(name, status, detail):
        steps.append({"step": name, "status": status, "detail": detail})
        return status != MISSING

    # 1. Server registration.
    registered = None
    token = runner_token or owner_token
    if not token:
        step("registration", MISSING, "Set PHALANX_RUNNER_TOKEN or PHALANX_OWNER_TOKEN to read registrations")
    else:
        try:
            found = {entry["id"]: entry["remote"]
                     for entry in RunnerAPI(config["base_url"], token).call("/repositories")}
            if repository in found:
                registered = found[repository]
                if remote and remote != registered:
                    step("registration", MISSING, f"{repository} is registered with remote {registered}, "
                         f"not {remote}; use the registered remote or a new repository ID")
                    registered = None
                else:
                    step("registration", OK, f"registered with remote {registered}")
            elif not remote:
                step("registration", MISSING, f"{repository} is not registered; pass --remote")
            elif not owner_token:
                step("registration", MISSING, "Set PHALANX_OWNER_TOKEN, or run "
                     f"`python -m phalanx register {repository} {remote}` as the owner")
            else:
                RunnerAPI(config["base_url"], owner_token).call(
                    "/repositories", {"id": repository, "remote": remote})
                registered = remote
                step("registration", DONE, f"registered with remote {remote}")
        except RunnerError as error:
            step("registration", MISSING, f"{error}; check base_url {config['base_url']} and the token")
    remote = registered

    # 2. Runner checkout and base branch.
    checkout = checkout or mapping.get("checkout")
    base_branch = base_branch or mapping.get("base_branch")
    checkout_ok = False
    if not checkout:
        step("checkout", MISSING, "Pass --checkout with the local clone path")
    elif remote is None:
        step("checkout", MISSING, "Complete registration first so the origin can be verified")
    else:
        checkout = Path(checkout).resolve()
        try:
            cloned = not checkout.exists() or (checkout.is_dir() and not any(checkout.iterdir()))
            if cloned:
                checkout.parent.mkdir(parents=True, exist_ok=True)
                _git(checkout.parent, "clone", "--", remote, str(checkout))
            manager = WorkspaceManager(checkout, remote, Path(state) / "worktrees")
            manager.verify_checkout()
            if not base_branch:
                try:
                    head = _git(checkout, "symbolic-ref", "--short", "refs/remotes/origin/HEAD").strip()
                    base_branch = head.removeprefix("origin/")
                except WorkspaceError:
                    base_branch = "main"
            manager.resolve_origin_base(base_branch)
            checkout_ok = step("checkout", DONE if cloned else OK,
                               f"{'cloned' if cloned else 'verified'} {checkout} on origin/{base_branch}")
        except WorkspaceError as error:
            step("checkout", MISSING, str(error))

    # 3. Runtime project. Only the Captain's runtime can resolve it.
    project_id = project_id or mapping.get("project_id")
    hosts = {other.get("host_id") for other in mappings.values() if other.get("host_id")}
    host_id = host_id or mapping.get("host_id") or (hosts.pop() if len(hosts) == 1 else None)
    if project_id and host_id:
        step("runtime_project", OK, f"project {project_id} on host {host_id}; the Captain "
             "verifies native workspace identity and trust before launching writers")
    else:
        step("runtime_project", MISSING, "Create or open a runtime project for the checkout and pass "
             + " and ".join(flag for flag, value in (("--project-id", project_id), ("--host-id", host_id))
                            if not value))

    # 4. Write only complete repository mappings.
    if checkout_ok and project_id and host_id:
        wanted = {**mapping, "checkout": str(checkout), "base_branch": base_branch,
                  "host_id": host_id, "project_id": project_id}
        if wanted == mappings.get(repository):
            step("mapping", OK, f"{config_path} maps {repository}")
        else:
            mappings[repository] = wanted
            _write_json(config_path, config)
            step("mapping", DONE, f"wrote {repository} to {config_path}")
    else:
        step("mapping", MISSING, "Complete the checkout and runtime project steps to write the mapping")

    # 5. Validation policy entry.
    from .validation_policy import ValidationPolicyError, load_validation_policy

    policy = config.get("validation_policy")
    if not policy:
        step("validation", MISSING, "Add \"validation_policy\" (an absolute path outside every checkout) "
             f"to {config_path}")
    else:
        policy = Path(policy)
        written = ""
        try:
            try:
                selected = load_validation_policy(policy, repository)
            except ValidationPolicyError as error:
                if policy.exists() and not str(error).startswith("No validation policy for"):
                    raise
                if not checkout_ok:
                    raise ValidationPolicyError("Complete the checkout step to propose ready checks") from None
                ready, found = discover(remote, base_branch, github_token)
                data = (json.loads(policy.read_text(encoding="utf-8")) if policy.exists()
                        else {"version": 1, "repositories": {}})
                data["repositories"][repository] = {
                    "local_checks": None, "draft_checks": [], "ready_checks": ready}
                _write_json(policy, data)
                written = f"wrote {repository} to {policy} (draft_checks [], ready checks {found}); "
                selected = load_validation_policy(policy, repository)
            unset = [name for name, value in (("local_checks", selected.local_checks),
                                              ("draft_checks", selected.draft_checks),
                                              ("ready_checks", selected.ready.required))
                     if value is None]
            if unset:
                step("validation", MISSING, written + "set " + " and ".join(unset) + " for the repository "
                     "(a list of checks, or [] when none apply); see docs/runner-validation-policy.md")
            else:
                step("validation", DONE if written else OK, written + f"{policy} configures {repository}")
        except ValidationPolicyError as error:
            step("validation", MISSING, str(error))

    # 6. Owner trust: never granted by Phalanx.
    step("owner_trust", MANUAL, f"The owner grants the worker runtime standing trust for "
         f"{checkout or 'the checkout'}; the Captain reports pending native trust approval")
    return {"repository": repository,
            "complete": all(entry["status"] != MISSING for entry in steps), "steps": steps}

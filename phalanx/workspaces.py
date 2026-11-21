"""Local Git workspaces. The runner owns worker exclusion and lifecycle authority."""

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from urllib.parse import unquote, urlsplit

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class WorkspaceError(RuntimeError):
    """Workspace preparation, verification, or cleanup could not proceed."""


@dataclass(frozen=True)
class Workspace:
    path: Path
    branch: str
    head: str
    dirty: bool


def _git(path: Path, *args: str, stdin: str | None = None) -> str:
    # Inherited Git overrides can redirect operations away from the verified checkout.
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith("GIT_")}
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", *args],
            cwd=path, env=env, input=stdin.encode() if stdin is not None else None,
            capture_output=True,
        )
    except OSError as exc:
        raise WorkspaceError(f"Cannot run Git in {path}: {exc}") from exc
    stdout = result.stdout.decode("utf-8", errors="surrogateescape")
    if result.returncode:
        detail = result.stderr.decode("utf-8", errors="replace").strip() or stdout.strip()
        raise WorkspaceError(f"Git {args[0]} failed in {path}: {detail}")
    return stdout


def _remote_identity(remote: str, checkout: Path) -> tuple:
    if not remote or any(char in remote for char in "\r\n\0"):
        raise WorkspaceError("A single nonempty origin URL is required")
    if "://" in remote:
        try:
            url = urlsplit(remote)
            host, port = url.hostname, url.port
        except ValueError as exc:
            raise WorkspaceError("Invalid origin URL") from exc
        if url.scheme == "file" and url.netloc.lower() in {"", "localhost"}:
            path = unquote(url.path)
            if os.name == "nt" and re.match(r"^/[a-zA-Z]:", path):
                path = path[1:]
            return ("local", os.path.normcase(str(Path(path).resolve())))
        default_ports = {"https": 443, "http": 80, "ssh": 22, "git": 9418}
        if not host or url.scheme not in default_ports or url.query or url.fragment:
            raise WorkspaceError("Unsupported origin URL")
        transport = url.scheme
        if port == default_ports[transport]:
            port = None
        path = url.path[1:]
    else:
        scp = re.fullmatch(r"(?:[^@/\\:]+@)?([^/\\:]+):(.+)", remote)
        if scp is None or re.match(r"^[a-zA-Z]:", remote):
            return ("local", os.path.normcase(str((checkout / remote).resolve())))
        host, path = scp.groups()
        host = host.lower()
        transport = "scp"
        port = None
    if not path:
        raise WorkspaceError("Origin URL is missing a repository path")
    if host == "github.com" and port is None:
        path = path.strip("/").lower().removesuffix(".git")
        if not re.fullmatch(r"[a-z0-9_.-]+/[a-z0-9_.-]+", path):
            raise WorkspaceError("GitHub origin needs an owner and repository")
        return ("github", path)
    return (transport, host, port, path)


def _item_branch(item_id: str) -> str:
    if not item_id:
        raise WorkspaceError("A work item ID is required")
    return f"codex/phalanx/{hashlib.sha256(item_id.encode()).hexdigest()}"


@contextmanager
def _repository_lock(common: Path):
    """Serialize short bookkeeping operations across local processes and threads."""
    try:
        handle = (common / "phalanx-workspaces.lock").open("a+b")
    except OSError as exc:
        raise WorkspaceError(f"Cannot open workspace lock in {common}: {exc}") from exc
    with handle:
        deadline = time.monotonic() + 30
        while True:
            try:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise WorkspaceError(f"Cannot acquire workspace lock in {common}: {exc}") from exc
                time.sleep(0.05)
        yield
        # Closing the file releases the lock, including on exceptions and process exit.


class WorkspaceManager:
    """Manage item branches in a configured checkout and its local workspace root."""

    def __init__(self, checkout: Path, expected_remote: str, root: Path):
        self.checkout = Path(checkout).resolve()
        self.expected_remote = _remote_identity(expected_remote, self.checkout)
        self.root = Path(root).resolve()

    def _verify_checkout(self) -> Path:
        top = _git(self.checkout, "rev-parse", "--show-toplevel")
        if Path(top.rstrip("\n")).resolve() != self.checkout:
            raise WorkspaceError(f"Mapped checkout is not a Git worktree root: {self.checkout}")
        origin = _git(self.checkout, "remote", "get-url", "--all", "origin").rstrip("\n")
        if _remote_identity(origin, self.checkout) != self.expected_remote:
            raise WorkspaceError(f"Origin of {self.checkout} does not match the registered remote")
        if self.root.is_relative_to(self.checkout):
            raise WorkspaceError("Workspace root must be outside the mapped checkout")
        common = _git(self.checkout, "rev-parse", "--path-format=absolute", "--git-common-dir")
        common_path = Path(common.rstrip("\n")).resolve()
        if self.root.is_relative_to(common_path):
            raise WorkspaceError("Workspace root must be outside Git metadata")
        return common_path

    def _location(self, common: Path, item_id: str) -> tuple[Path, str, str]:
        branch = _item_branch(item_id)
        item = branch.rsplit("/", 1)[1]
        repo = hashlib.sha256(os.fsencode(os.path.normcase(str(common)))).hexdigest()[:16]
        path = self.root / f"{repo}-{item}"
        owner = hashlib.sha256(os.fsencode(os.path.normcase(str(path)))).hexdigest()
        if path.resolve() != path:
            raise WorkspaceError(f"Workspace path resolves elsewhere: {path}")
        return path, branch, f"refs/phalanx/workspaces/{owner}"

    def _registered(self, path: Path, branch: str) -> bool:
        records = _git(self.checkout, "worktree", "list", "--porcelain", "-z")
        found = False
        for record in records.split("\0\0"):
            location, *fields = record.split("\0")
            if not location.startswith("worktree "):
                continue
            matches_branch = f"branch refs/heads/{branch}" in fields
            matches_path = Path(location.removeprefix("worktree ")) == path
            if matches_branch and matches_path and not found:
                if "locked initializing" in fields or any(
                    field.startswith("prunable") for field in fields
                ):
                    raise WorkspaceError(f"Git worktree registration needs recovery: {path}")
                found = True
                continue
            if matches_path or matches_branch:
                raise WorkspaceError(f"Git worktree registration conflicts with {path} ({branch})")
        if not found and os.path.lexists(path):
            raise WorkspaceError(f"Workspace path is occupied without a Git registration: {path}")
        return found

    def _snapshot(self, path: Path, branch: str, common: Path) -> Workspace:
        top = _git(path, "rev-parse", "--show-toplevel").rstrip("\n")
        actual_common = _git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
        actual_branch = _git(path, "symbolic-ref", "--no-recurse", "HEAD").rstrip("\n")
        if (Path(top).resolve() != path or Path(actual_common.rstrip("\n")).resolve() != common
                or actual_branch != f"refs/heads/{branch}"):
            raise WorkspaceError(f"Workspace checkout identity changed: {path}")
        # Verify the backlink as well as the shared repository and symbolic branch.
        # A copied .git pointer can otherwise impersonate the registered checkout.
        git_dir = Path(_git(path, "rev-parse", "--absolute-git-dir").rstrip("\n"))
        if git_dir.resolve().parent != common / "worktrees":
            raise WorkspaceError(f"Workspace Git metadata is outside the repo's worktrees: {path}")
        try:
            backlink = Path(os.fsdecode((git_dir / "gitdir").read_bytes()).rstrip("\r\n"))
        except OSError as exc:
            raise WorkspaceError(f"Cannot verify workspace registration for {path}: {exc}") from exc
        if (git_dir / backlink).resolve() != path / ".git":
            raise WorkspaceError(f"Workspace Git metadata points to a different checkout: {path}")
        head = _git(path, "rev-parse", "--verify", "HEAD^{commit}").strip()
        dirty = bool(_git(
            path, "status", "--porcelain=v1", "-z", "--untracked-files=all",
            "--ignored=matching", "--ignore-submodules=none",
        ))
        return Workspace(path, branch, head, dirty)

    def prepare(self, item_id: str, *, base: str) -> Workspace:
        """Create from a local base ref, or return the existing checkout as found."""
        common = self._verify_checkout()
        with _repository_lock(common):
            path, branch, owner_ref = self._location(common, item_id)
            registered = self._registered(path, branch)
            branch_ref = f"refs/heads/{branch}"
            refs = set(_git(
                self.checkout, "for-each-ref", "--format=%(refname)", owner_ref, branch_ref,
            ).splitlines())
            if refs & {owner_ref, branch_ref} != {owner_ref, branch_ref}:
                if registered or refs:
                    raise WorkspaceError(f"Workspace ownership or branch is inconsistent: {path}")
                commit = _git(
                    self.checkout, "rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}",
                ).strip()
            else:
                commit = None
            if not registered:
                try:
                    self.root.mkdir(parents=True, exist_ok=True)
                    with tempfile.TemporaryDirectory(prefix="phalanx-hooks-") as empty_hooks:
                        hooks = f"core.hooksPath={Path(empty_hooks).as_posix()}"
                        if commit is not None:
                            # Reserve both refs together so retries can attach the
                            # branch without resetting its tip.
                            _git(self.checkout, "-c", hooks, "update-ref", "--stdin",
                                 stdin=f"create {branch_ref} {commit}\ncreate {owner_ref} {commit}\n")
                        _git(self.checkout, "-c", hooks, "worktree", "add", "--", str(path), branch)
                except OSError as exc:
                    raise WorkspaceError(f"Cannot prepare workspace at {path}: {exc}") from exc
            return self._snapshot(path, branch, common)

    def cleanup(self, item_id: str, *, closed: bool, expected_head: str) -> bool:
        """Remove an owned, clean checkout at the recorded head. Keep its branch.

        The runner must confirm the execution is closed and no worker can still write.
        Return false if the owned checkout is already absent.
        """
        if closed is not True:
            raise WorkspaceError("Workspace cleanup requires a closed execution")
        common = self._verify_checkout()
        with _repository_lock(common):
            path, branch, owner_ref = self._location(common, item_id)
            if not self._registered(path, branch):
                return False
            owners = _git(self.checkout, "for-each-ref", "--format=%(refname)", owner_ref)
            if owner_ref not in owners.splitlines():
                raise WorkspaceError(f"Cannot verify workspace ownership: {path}")
            snapshot = self._snapshot(path, branch, common)
            if snapshot.head != expected_head:
                raise WorkspaceError(f"Workspace HEAD differs from the recorded checkpoint: {path}")
            if snapshot.dirty:
                raise WorkspaceError(f"Workspace has local changes or untracked files: {path}")
            # Git status can deliberately hide paths marked skip-worktree or
            # assume-unchanged. Refuse cleanup when the index uses those flags.
            entries = _git(path, "ls-files", "-v", "-z")
            if any(not entry.startswith("H ") for entry in entries.split("\0") if entry):
                raise WorkspaceError(f"Workspace has index entries that can hide local changes: {path}")
            git_dir = Path(_git(path, "rev-parse", "--absolute-git-dir").rstrip("\n"))
            operation_files = (
                "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "REBASE_HEAD",
                "rebase-merge", "rebase-apply", "sequencer", "BISECT_START",
            )
            if any((git_dir / name).exists() for name in operation_files):
                raise WorkspaceError(f"Workspace has an in-progress Git operation: {path}")
            _git(self.checkout, "worktree", "remove", "--", str(path))
            return True

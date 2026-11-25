"""Local Git workspaces prepared by direct Captains; queue authority stays on the server."""

from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial
import hashlib
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import unquote, urlsplit

from .git import git_text
from .transitions import item_branch

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


_git = partial(git_text, error=WorkspaceError)


def _rev_parse(path: Path, *args: str, count: int) -> list[str]:
    """Run one rev-parse that prints one line per query, in argument order."""
    lines = _git(path, "rev-parse", *args).split("\n")
    if len(lines) != count + 1 or lines[-1]:
        # Also refuses paths containing newlines, which cannot be split reliably.
        raise WorkspaceError(f"Unexpected Git rev-parse output in {path}")
    return lines[:-1]


def _status(path: Path, *flags: str) -> str:
    return _git(path, "status", "--porcelain=v1", "-z", "--untracked-files=all",
                "--ignore-submodules=none", *flags)


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
    return item_branch(item_id)


def _lock(handle) -> None:
    """Take a nonblocking exclusive lock on an open file; raise OSError when held."""
    if os.name == "nt":
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


@contextmanager
def _file_lock(path: Path):
    """Serialize an operation across local processes and threads."""
    try:
        handle = path.open("a+b")
    except OSError as exc:
        raise WorkspaceError(f"Cannot open Git operation lock at {path}: {exc}") from exc
    with handle:
        deadline = time.monotonic() + 30
        while True:
            try:
                _lock(handle)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise WorkspaceError(f"Cannot acquire Git operation lock at {path}: {exc}") from exc
                time.sleep(0.05)
        yield
        # Closing the file releases the lock, including on exceptions and process exit.


@contextmanager
def any_file_lock(paths):
    """Hold the first free lock among `paths` and yield its path, or yield None if all are held."""
    for path in paths:
        try:
            handle = path.open("a+b")
        except OSError as exc:
            raise WorkspaceError(f"Cannot open lock at {path}: {exc}") from exc
        try:
            _lock(handle)
        except OSError:
            handle.close()
            continue
        with handle:
            yield path
        return
    yield None


class WorkspaceManager:
    """Manage item branches in a configured checkout and its local workspace root."""

    def __init__(self, checkout: Path, expected_remote: str, root: Path):
        self.checkout = Path(checkout).resolve()
        self.expected_remote = _remote_identity(expected_remote, self.checkout)
        self.root = Path(root).resolve()

    def _verify_checkout(self) -> Path:
        top, common = _rev_parse(self.checkout, "--show-toplevel", "--path-format=absolute",
                                 "--git-common-dir", count=2)
        if Path(top).resolve() != self.checkout:
            # On case-insensitive macOS volumes, a mis-cased configured path lands here.
            raise WorkspaceError(f"Mapped checkout {self.checkout} is not its Git worktree root {top}; "
                                 "configure the exact on-disk path")
        origin = _git(self.checkout, "remote", "get-url", "--all", "origin").rstrip("\n")
        if _remote_identity(origin, self.checkout) != self.expected_remote:
            raise WorkspaceError(f"Origin of {self.checkout} does not match the registered remote")
        if self.root.is_relative_to(self.checkout):
            raise WorkspaceError("Workspace root must be outside the mapped checkout")
        common_path = Path(common).resolve()
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
        top, actual_common, git_dir, head = _rev_parse(
            path, "--show-toplevel", "--path-format=absolute", "--git-common-dir",
            "--absolute-git-dir", "--verify", "HEAD^{commit}", count=4,
        )
        actual_branch = _git(path, "symbolic-ref", "--no-recurse", "HEAD").rstrip("\n")
        if (Path(top).resolve() != path or Path(actual_common).resolve() != common
                or actual_branch != f"refs/heads/{branch}"):
            raise WorkspaceError(f"Workspace checkout identity changed: {path}")
        # Verify the backlink as well as the shared repository and symbolic branch.
        # A copied .git pointer can otherwise impersonate the registered checkout.
        git_dir = Path(git_dir)
        if git_dir.resolve().parent != common / "worktrees":
            raise WorkspaceError(f"Workspace Git metadata is outside the repo's worktrees: {path}")
        try:
            backlink = Path(os.fsdecode((git_dir / "gitdir").read_bytes()).rstrip("\r\n"))
        except OSError as exc:
            raise WorkspaceError(f"Cannot verify workspace registration for {path}: {exc}") from exc
        if (git_dir / backlink).resolve() != path / ".git":
            raise WorkspaceError(f"Workspace Git metadata points to a different checkout: {path}")
        # Ignored build output cannot change the candidate, so it is not dirty.
        dirty = bool(_status(path))
        return Workspace(path, branch, head, dirty)

    def verify_checkout(self) -> None:
        """Check that the local mapping can represent the registered repository."""
        self._verify_checkout()

    def inspect(self, item_id: str) -> Workspace:
        """Verify an existing workspace without creating or repairing it."""
        common = self._verify_checkout()
        with _file_lock(common / "phalanx-workspaces.lock"):
            path, branch, owner_ref = self._location(common, item_id)
            if not self._registered(path, branch):
                raise WorkspaceError("The item worktree is absent")
            owners = _git(self.checkout, "for-each-ref", "--format=%(refname)", owner_ref)
            if owner_ref not in owners.splitlines():
                raise WorkspaceError("Cannot verify workspace ownership")
            return self._snapshot(path, branch, common)

    def resolve_origin_base(self, branch: str) -> str:
        """Pin the configured origin tracking branch before preparing development."""
        self._verify_checkout()
        ref = f"refs/remotes/origin/{branch}"
        if not branch:
            raise WorkspaceError("A development base branch is required")
        _git(self.checkout, "check-ref-format", f"refs/heads/{branch}")
        sha = _git(self.checkout, "show-ref", "--verify", "--hash", ref).strip()
        if _git(self.checkout, "--no-replace-objects", "cat-file", "-t", sha).strip() != "commit":
            raise WorkspaceError("The exact origin tracking ref must point to a commit")
        return sha

    def prepare(self, item_id: str, *, base: str) -> Workspace:
        """Create from a local base ref, or return the existing checkout as found."""
        common = self._verify_checkout()
        with _file_lock(common / "phalanx-workspaces.lock"):
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

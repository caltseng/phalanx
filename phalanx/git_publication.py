"""Conditional publication of a runner-owned item branch."""

from collections.abc import Callable
from dataclasses import dataclass
import os
from pathlib import Path
import re
import subprocess
from typing import Literal

from .workspaces import WorkspaceError, _item_branch, _remote_identity


class PublicationError(RuntimeError):
    """A Git read or a preflight check failed before any push was sent."""


@dataclass(frozen=True)
class PublicationResult:
    status: Literal["success", "conflict", "uncertain"]
    reason: str


def _is_oid(value: str) -> bool:
    return bool(re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value))


class GitPublication:
    """Publish one item's verified candidate using its runner-configured origin.

    The caller owns claim validation, durable intent, and reconciliation. The
    checkout and its Git configuration must be under runner control.
    """

    def __init__(self, checkout: Path, expected_remote: str, item_id: str):
        self.checkout = Path(checkout).resolve()
        try:
            self.expected_remote = _remote_identity(expected_remote, self.checkout)
            self.ref = f"refs/heads/{_item_branch(item_id)}"
        except WorkspaceError as exc:
            raise PublicationError(str(exc)) from exc

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        env = {key: value for key, value in os.environ.items()
               if not key.upper().startswith("GIT_")}
        env.update(GIT_TERMINAL_PROMPT="0", GIT_GRAFT_FILE=os.devnull, LC_ALL="C")
        try:
            return subprocess.run(
                ["git", "--no-replace-objects", "-c", "http.followRedirects=false", *args],
                cwd=self.checkout, env=env, stdin=subprocess.DEVNULL,
                capture_output=True, text=True, encoding="utf-8", errors="surrogateescape",
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PublicationError("Git command could not complete") from exc

    def _verify_origin(self) -> None:
        root = self._git("rev-parse", "--show-toplevel")
        if root.returncode or Path(root.stdout.rstrip("\n")).resolve() != self.checkout:
            raise PublicationError("Publication checkout is not a Git worktree root")
        for flags in ((), ("--push",)):
            result = self._git("remote", "get-url", "--all", *flags, "origin")
            urls = result.stdout.splitlines()
            if result.returncode or len(urls) != 1:
                raise PublicationError("Publication requires one origin URL in each direction")
            try:
                identity = _remote_identity(urls[0], self.checkout)
            except WorkspaceError as exc:
                raise PublicationError("Invalid configured origin URL") from exc
            if identity != self.expected_remote:
                raise PublicationError("Origin does not match the registered repository")

    def read_head(self, *, authorize: Callable[[], None] | None = None) -> str | None:
        """Read the exact remote item ref. Return None only for confirmed absence."""
        return self._read_ref(self.ref, authorize)

    def read_branch(
        self, branch: str, *, authorize: Callable[[], None] | None = None,
    ) -> str | None:
        """Read an exact named remote branch, including the intended PR base."""
        ref = f"refs/heads/{branch}"
        if not branch or self._git("check-ref-format", ref).returncode:
            raise PublicationError("Invalid remote branch name")
        return self._read_ref(ref, authorize)

    def _read_ref(self, ref: str, authorize: Callable[[], None] | None) -> str | None:
        self._verify_origin()
        if authorize is not None:
            authorize()
        result = self._git("ls-remote", "--exit-code", "--refs", "origin", ref)
        if result.returncode == 2 and not result.stdout:
            return None
        if result.returncode == 0:
            oid, separator, returned_ref = result.stdout.rstrip("\n").partition("\t")
            if separator and returned_ref == ref and _is_oid(oid):
                return oid
        raise PublicationError("Could not read the exact remote branch ref")

    def publish(
        self, candidate: str, *, expected: str | None,
        authorize: Callable[[], None] | None = None,
    ) -> PublicationResult:
        """Attempt one conditional update and verify the candidate by remote read-back."""
        if not _is_oid(candidate) or (expected is not None and not _is_oid(expected)):
            raise PublicationError("Publication requires full lowercase Git object IDs")
        local = self._git("cat-file", "-t", candidate)
        if local.returncode or local.stdout.strip() != "commit":
            raise PublicationError("Publication candidate must be a local commit")

        observed = self.read_head(authorize=authorize)
        if observed != expected:
            return PublicationResult("conflict", "The remote ref differs from the expected head")
        if expected is not None and expected != candidate:
            old = self._git("cat-file", "-t", expected)
            if old.returncode or old.stdout.strip() != "commit":
                raise PublicationError("Fetch the recorded remote commit before checking ancestry")
            ancestry = self._git("merge-base", "--is-ancestor", expected, candidate)
            if ancestry.returncode == 1:
                return PublicationResult("conflict", "The update would not fast-forward")
            if ancestry.returncode:
                raise PublicationError("Could not verify candidate ancestry")
        if observed == candidate:
            return PublicationResult("success", "The remote ref already matches the expected candidate")

        self._verify_origin()
        # A revision expression avoids matching a local branch named after the SHA.
        refspec = f"{candidate}^{{commit}}:{self.ref}"
        if authorize is not None:
            authorize()
        try:
            result = self._git(
                "-c", "remote.origin.mirror=false",
                "push", "--porcelain", "--no-follow-tags",
                "--recurse-submodules=no", "--force-with-lease=" + self.ref + ":" + (expected or ""),
                "origin", refspec,
            )
        except PublicationError:
            return PublicationResult("uncertain", "Git push was interrupted. Read the remote ref")
        if result.returncode:
            # A stale lease reported by send-pack is a rejection before any write.
            for line in result.stdout.splitlines():
                if line == f"!\t{refspec}\t[rejected] (stale info)":
                    return PublicationResult("conflict", "Git rejected the stale ref lease")
            return PublicationResult("uncertain", "Git push failed. Read the remote ref")
        try:
            if self.read_head(authorize=authorize) == candidate:
                return PublicationResult("success", "Verified the candidate at the remote ref")
        except PublicationError:
            pass
        return PublicationResult("uncertain", "Could not verify the candidate. Read the remote ref")

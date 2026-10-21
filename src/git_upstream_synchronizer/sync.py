"""Safe local Git fast-forward synchronization; never pushes or force-updates."""

from dataclasses import dataclass
from pathlib import Path
import fcntl
import os
import subprocess
from typing import Literal


class SyncError(RuntimeError):
    """Precondition failure requiring operator intervention."""


class GitCommandError(RuntimeError):
    """Git operation failed (e.g. upstream unavailable)."""


@dataclass(frozen=True)
class SyncResult:
    status: Literal["UPDATED", "UP_TO_DATE", "NEEDS_REVIEW"]
    previous_sha: str
    upstream_sha: str
    current_sha: str


def _git(
    repo: Path, *args: str, allowed_exit_codes: tuple[int, ...] = (0,),
    lock_fd: int | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            text=True, capture_output=True, check=False, timeout=30,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            pass_fds=() if lock_fd is None else (lock_fd,),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise GitCommandError(f"Unable to run Git operation: {args[0]}") from error
    if result.returncode not in allowed_exit_codes:
        raise GitCommandError(
            f"git {args[0]} failed ({result.returncode}): {result.stderr.strip()}"
        )
    return result


def synchronize_repository(repo: Path, *, branch: str = "main", upstream: str = "upstream") -> SyncResult:
    """Fetch a tracked branch and fast-forward its checked-out local counterpart.

    The repository must be clean, on the selected branch, and configured with an
    upstream remote. Divergent histories are reported, never merged or overwritten.
    Recognized Git write locks require operator inspection before fetching; the
    synchronizer never removes them or attempts to repair damaged checkout state.
    This intentionally does not push the result to a hosting service.
    """
    try:
        path = Path(repo).resolve(strict=True)
    except OSError as error:
        raise SyncError("Repository path is unavailable") from error
    if not path.is_dir():
        raise SyncError("Repository path is not a directory")
    if not branch or branch.startswith("-") or _git(
        path, "check-ref-format", f"refs/heads/{branch}", allowed_exit_codes=(0, 1),
    ).returncode:
        raise SyncError("Invalid branch name")
    if not upstream or upstream.startswith("-") or upstream not in _git(path, "remote").stdout.splitlines():
        raise SyncError("Upstream must name a configured remote")

    # One Linux advisory lock covers CLI/worker calls, including linked worktrees.
    common_dir = Path(_git(path, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip())
    try:
        with (common_dir / "upstream-synchronizer.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise GitCommandError("Another synchronization is running") from error
            return _synchronize_locked(path, branch, upstream, lock.fileno())
    except OSError as error:
        raise GitCommandError("Unable to lock repository") from error


def _synchronize_locked(path: Path, branch: str, upstream: str, lock_fd: int) -> SyncResult:
    current_branch = _git(
        path, "symbolic-ref", "--quiet", "--short", "HEAD", allowed_exit_codes=(0, 1),
    ).stdout.strip()
    if current_branch != branch:
        raise SyncError(f"Expected checked-out branch {branch!r}, found {current_branch!r}")
    for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply", "sequencer"):
        marker_path = _git(path, "rev-parse", "--git-path", marker).stdout.strip()
        if (path / marker_path).exists():
            raise SyncError("Repository has an unfinished Git operation")
    for lock_name in ("index.lock", "HEAD.lock", f"refs/heads/{branch}.lock", "packed-refs.lock"):
        lock_path = _git(path, "rev-parse", "--git-path", lock_name).stdout.strip()
        if (path / lock_path).exists():
            raise SyncError(f"Repository has a Git lock file: {lock_name}; inspect Git activity before retrying")
    if _git(path, "status", "--porcelain", "--untracked-files=all", "--ignore-submodules=none").stdout.strip():
        raise SyncError("Repository has uncommitted or untracked files")

    # FETCH_HEAD is used so an arbitrary remote-tracking ref is not assumed.
    # Mutating Git children retain the lock if their Python parent dies.
    _git(path, "fetch", "--no-tags", upstream, f"refs/heads/{branch}", lock_fd=lock_fd)
    local_sha = _git(path, "rev-parse", f"refs/heads/{branch}").stdout.strip()
    upstream_sha = _git(path, "rev-parse", "FETCH_HEAD").stdout.strip()

    if local_sha == upstream_sha:
        return SyncResult("UP_TO_DATE", local_sha, upstream_sha, local_sha)

    ancestry = _git(
        path, "merge-base", "--is-ancestor", local_sha, upstream_sha,
        allowed_exit_codes=(0, 1),
    )
    if ancestry.returncode == 1:
        return SyncResult("NEEDS_REVIEW", local_sha, upstream_sha, local_sha)

    _git(path, "merge", "--ff-only", "--no-overwrite-ignore", upstream_sha, lock_fd=lock_fd)
    updated_sha = _git(path, "rev-parse", "HEAD").stdout.strip()
    return SyncResult("UPDATED", local_sha, upstream_sha, updated_sha)

"""Safe local Git fast-forward synchronization; never pushes or force-updates."""

from dataclasses import dataclass
from pathlib import Path
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


def _git(repo: Path, *args: str, allowed_exit_codes: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            text=True, capture_output=True, check=False, timeout=30,
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
    This intentionally does not push the result to a hosting service.
    """
    path = Path(repo).resolve(strict=True)
    if not path.is_dir():
        raise SyncError("Repository path is not a directory")
    current_branch = _git(path, "symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip()
    if current_branch != branch:
        raise SyncError(f"Expected checked-out branch {branch!r}, found {current_branch!r}")
    if _git(path, "status", "--porcelain").stdout.strip():
        raise SyncError("Repository has uncommitted or untracked files")

    # FETCH_HEAD is used so an arbitrary remote-tracking ref is not assumed.
    _git(path, "fetch", "--no-tags", upstream, f"refs/heads/{branch}")
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

    _git(path, "merge", "--ff-only", upstream_sha)
    updated_sha = _git(path, "rev-parse", "HEAD").stdout.strip()
    return SyncResult("UPDATED", local_sha, upstream_sha, updated_sha)

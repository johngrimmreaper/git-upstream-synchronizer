"""Compare a fetched upstream snapshot without updating the local branch."""

from pathlib import Path

from .sync import _fetched_repository, _git

COMMIT_LIMIT = 30
FILE_LIMIT = 20
PATCH_LIMIT = 20_000


def _commits(path: Path, revision: str) -> list[dict[str, str]]:
    output = _git(
        path, "log", "--no-decorate", "--no-color", f"--max-count={COMMIT_LIMIT}",
        "--format=%H%x00%an%x00%aI%x00%s", revision, "--",
    ).stdout
    return [dict(zip(("sha", "author", "date", "subject"), line.split("\0", 3)))
            for line in output.splitlines() if line]


def _files(path: Path, local: str, upstream: str) -> tuple[list[dict], int]:
    options = ("--no-ext-diff", "--no-textconv", "--no-color", "--no-renames")
    output = _git(path, "diff", *options, "--numstat", "-z", local, upstream, "--").stdout
    records = [record.split("\t", 2) for record in output.split("\0") if record]
    files = []
    for added, deleted, name in records[:FILE_LIMIT]:
        binary = added == "-" or deleted == "-"
        patch = "" if binary else _git(
            path, "diff", *options, "--unified=3", local, upstream,
            "--", f":(literal){name}",
        ).stdout
        files.append({
            "path": name, "added": added, "deleted": deleted, "binary": binary,
            "patch": patch[:PATCH_LIMIT], "truncated": len(patch) > PATCH_LIMIT,
        })
    return files, len(records)


def compare_repository(repo: Path, *, branch: str = "main", upstream: str = "upstream") -> dict:
    """Fetch and inspect under the synchronization lock; never merge or push.

    Validation matches synchronization, including refusing dirty checkouts.
    Preview lists are capped; counts cover the complete reachable histories.
    File patches compare the two tips, including when their histories diverge.
    """
    with _fetched_repository(repo, branch=branch, upstream=upstream) as snapshot:
        path, local, fetched, _lock_fd = snapshot
        ahead, behind = map(int, _git(
            path, "rev-list", "--left-right", "--count", f"{local}...{fetched}", "--",
        ).stdout.split())
        related = _git(
            path, "merge-base", local, fetched, allowed_exit_codes=(0, 1),
        ).returncode == 0
        if local == fetched:
            status = "UP_TO_DATE"
        elif not related:
            status = "UNRELATED"
        elif ahead and behind:
            status = "DIVERGED"
        else:
            status = "AHEAD" if ahead else "BEHIND"
        files, total_files = _files(path, local, fetched)
        return {
            "status": status, "ahead": ahead, "behind": behind,
            "local_sha": local, "upstream_sha": fetched,
            "local_commits": _commits(path, f"{fetched}..{local}"),
            "upstream_commits": _commits(path, f"{local}..{fetched}"),
            "files": files, "total_files": total_files,
            "commit_limit": COMMIT_LIMIT, "file_limit": FILE_LIMIT,
        }

"""Compare a fetched upstream snapshot without updating the local branch."""

from pathlib import Path
import os
import selectors
import subprocess
import time

from .sync import GitCommandError, _fetched_repository, _git

COMMIT_LIMIT = 30
FILE_LIMIT = 20
PATCH_LIMIT = 20_000


INVENTORY_LIMIT = 2 * 1024 * 1024


def _bounded_git(path: Path, *args: str, limit: int) -> tuple[bytes, bool]:
    """Read-only preview commands: bound retained bytes and stop excess output.

    Never use this helper for fetch/merge: terminating a preview is harmless,
    whereas mutating children must retain the core's inherited advisory lock.
    """
    try:
        with subprocess.Popen(
            ["git", "-C", str(path), *args], stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        ) as child:
            try:
                output = bytearray()
                deadline = time.monotonic() + 30
                with selectors.DefaultSelector() as selector:
                    selector.register(child.stdout, selectors.EVENT_READ)
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0 or not selector.select(remaining):
                            raise GitCommandError("Git preview timed out")
                        block = os.read(child.stdout.fileno(), min(65536, limit + 1 - len(output)))
                        if not block:
                            break
                        output.extend(block)
                        if len(output) > limit:
                            return bytes(output[:limit]), True
                if child.wait(timeout=max(.01, deadline - time.monotonic())):
                    raise GitCommandError("Git preview failed; inspect repository configuration")
                return bytes(output), False
            finally:
                if child.poll() is None:
                    child.kill()
                child.wait()
    except (OSError, subprocess.TimeoutExpired) as error:
        raise GitCommandError("Unable to read Git preview") from error


def _commits(path: Path, revision: str) -> list[dict[str, str]]:
    output, _ = _bounded_git(
        path, "log", "-z", "--no-decorate", "--no-color", f"--max-count={COMMIT_LIMIT}",
        "--format=%H%x00%an%x00%aI%x00%s", revision, "--", limit=INVENTORY_LIMIT,
    )
    fields = output.decode("utf-8", "replace").split("\0")
    return [dict(zip(("sha", "author", "date", "subject"), fields[i:i+4]))
            for i in range(0, len(fields) - 4, 4)]


def _files(path: Path, local: str, upstream: str) -> tuple[list[dict], int]:
    options = ("--no-ext-diff", "--no-textconv", "--no-color", "--no-renames")
    output, truncated = _bounded_git(
        path, "diff", *options, "--numstat", "-z", local, upstream, "--", limit=INVENTORY_LIMIT,
    )
    if truncated:
        raise GitCommandError("Changed-file inventory exceeds the 2 MiB preview limit; inspect with Git")
    records = [record.split("\t", 2) for record in output.decode("utf-8", "surrogateescape").split("\0") if record]
    files = []
    for added, deleted, name in records[:FILE_LIMIT]:
        binary = added == "-" or deleted == "-"
        patch, truncated = (b"", False) if binary else _bounded_git(
            path, "diff", *options, "--unified=3", local, upstream,
            "--", f":(literal){name}", limit=PATCH_LIMIT,
        )
        files.append({
            "path": name, "added": added, "deleted": deleted, "binary": binary,
            "patch": patch.decode("utf-8", "replace"), "truncated": truncated,
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

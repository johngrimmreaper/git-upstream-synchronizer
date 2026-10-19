"""Standalone adapter: Python standard library and the synchronization core only."""

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

from .sync import GitCommandError, SyncError, synchronize_repository


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Safely fast-forward a local Git repository")
    parser.add_argument("repository", type=Path)
    parser.add_argument("--branch", default="main")
    parser.add_argument("--upstream", default="upstream", help="Configured remote name")
    args = parser.parse_args(argv)
    try:
        result = synchronize_repository(args.repository, branch=args.branch, upstream=args.upstream)
    except (SyncError, GitCommandError) as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return 1
    print(json.dumps(asdict(result)))
    return 2 if result.status == "NEEDS_REVIEW" else 0


if __name__ == "__main__":
    raise SystemExit(main())

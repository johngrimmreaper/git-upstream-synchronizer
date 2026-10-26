"""Trusted repository configuration shared by optional adapters."""

import json
from pathlib import Path


def load_repositories(path: Path) -> dict[str, dict[str, str]]:
    """Load a small local allowlist; process variables cannot select arbitrary paths."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not data:
        raise ValueError("Repository configuration must be a nonempty JSON object")
    for name, config in data.items():
        if not isinstance(config, dict) or set(config) - {"path", "branch", "upstream"}:
            raise ValueError(f"Invalid configuration for {name!r}")
        if not config.get("path") or not all(isinstance(v, str) and v for v in config.values()):
            raise ValueError(f"Configuration for {name!r} needs a path and nonempty strings")
        if not Path(config["path"]).is_absolute():
            raise ValueError(f"Repository path for {name!r} must be absolute")
    return data

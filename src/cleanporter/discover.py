"""Expand command-line paths into the set of files to analyse.

Directories named on the command line are walked; dot-directories and known
build/cache directories are skipped during the walk. A path the user named
*explicitly* is never rejected -- pointing the tool at ``.venv/...`` or at an
excluded file is taken as deliberate.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path

from .config import Config

ALWAYS_SKIP_DIRS = frozenset(
    {
        "__pycache__",
        "node_modules",
        "build",
        "dist",
        "site-packages",
    }
)


def _is_skipped_dir(name: str) -> bool:
    return name.startswith(".") or name in ALWAYS_SKIP_DIRS


def _excluded(path: Path, config: Config) -> bool:
    resolved = path.resolve()
    abs_posix = resolved.as_posix()
    try:
        rel = resolved.relative_to(config.root.resolve()).as_posix()
    except ValueError:
        rel = path.as_posix()
    for pattern in config.exclude:
        if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(abs_posix, pattern):
            return True
        if any(ch in pattern for ch in "*?["):
            continue
        literal = pattern.rstrip("/")
        if rel == literal or rel.startswith(literal + "/"):
            return True
    return False


def _walk(directory: Path, config: Config) -> list[Path]:
    found: list[Path] = []
    for child in sorted(directory.iterdir()):
        if child.is_dir():
            if _is_skipped_dir(child.name) or _excluded(child, config):
                continue
            found.extend(_walk(child, config))
        elif child.suffix == ".py" and not _excluded(child, config):
            found.append(child)
    return found


def iter_python_files(paths: list[Path], config: Config) -> tuple[list[Path], list[str]]:
    """Expand *paths* into a de-duplicated sorted file list plus warnings."""
    warnings: list[str] = []
    seen: set[Path] = set()
    out: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if not path.exists():
            warnings.append(f"path does not exist: {path}")
            continue
        # An explicitly named file bypasses every filter.
        candidates = [path] if path.is_file() else _walk(path, config)
        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            out.append(candidate)
    return sorted(out), warnings

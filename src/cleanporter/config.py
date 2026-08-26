"""Configuration loading from pyproject.toml ([tool.cleanporter])."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

_VALID_SCOPES = ("all", "first-party")


class ConfigError(ValueError):
    """Raised when [tool.cleanporter] is malformed."""


@dataclass(frozen=True)
class Config:
    """Validated cleanporter configuration.

    Attributes:
        root: Directory of the pyproject.toml this config came from (or cwd).
        exclude: Glob patterns matched against paths relative to ``root``.
            Also matched against the absolute path so patterns may be rooted.
        scope: Which imports to report: ``all`` or ``first-party``.
        autofix_third_party: Allow --fix to rewrite imports whose target
            module lives outside the project source roots.
        runtime_fallback: Permit importing the parent module via importlib in
            order to disambiguate symbols that static analysis cannot settle.
        treat_unresolved_as_error: Count CP002 (unresolvable) findings toward
            the failure exit code.
        source_roots: Explicit source roots (relative to root). When empty,
            roots are auto-discovered.
    """

    root: Path
    exclude: tuple[str, ...] = ()
    scope: str = "all"
    autofix_third_party: bool = False
    runtime_fallback: bool = True
    treat_unresolved_as_error: bool = False
    source_roots: tuple[str, ...] = ()


_EMPTY_CONFIG_SENTINEL = object()


def _parse_table(table: dict[str, object], root: Path) -> Config:
    kwargs: dict[str, object] = {}
    if "exclude" in table:
        value = table["exclude"]
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ConfigError("tool.cleanporter.exclude must be a list of strings")
        kwargs["exclude"] = tuple(value)
    if "scope" in table:
        value = table["scope"]
        if not isinstance(value, str) or value not in _VALID_SCOPES:
            raise ConfigError(
                f"tool.cleanporter.scope must be one of {_VALID_SCOPES}, got {value!r}"
            )
        kwargs["scope"] = value
    for flag in (
        "autofix_third_party",
        "runtime_fallback",
        "treat_unresolved_as_error",
    ):
        if flag in table:
            value = table[flag]
            if not isinstance(value, bool):
                raise ConfigError(f"tool.cleanporter.{flag} must be a boolean")
            kwargs[flag] = value
    if "source_roots" in table:
        value = table["source_roots"]
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ConfigError("tool.cleanporter.source_roots must be a list of strings")
        kwargs["source_roots"] = tuple(value)
    unknown = set(table) - {
        "exclude",
        "scope",
        "autofix_third_party",
        "runtime_fallback",
        "treat_unresolved_as_error",
        "source_roots",
    }
    if unknown:
        raise ConfigError(f"unknown tool.cleanporter keys: {sorted(unknown)}")
    return Config(root=root, **kwargs)


def find_pyproject(start: Path) -> Path | None:
    """Walk upward from *start* looking for a pyproject.toml."""
    current = start.resolve()
    if current.is_file():
        current = current.parent
    while True:
        candidate = current / "pyproject.toml"
        if candidate.is_file():
            return candidate
        parent = current.parent
        if parent == current:
            return None
        current = parent


def load_config(start: Path) -> Config:
    """Load configuration walking upward from *start*; defaults if absent."""
    anchor = start.resolve()
    pyproject = find_pyproject(anchor)
    root = pyproject.parent if pyproject else (anchor if anchor.is_dir() else anchor.parent)
    if pyproject is None:
        return Config(root=root)
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    table = data.get("tool", {}).get("cleanporter", {})
    if not isinstance(table, dict):
        raise ConfigError("[tool.cleanporter] must be a TOML table")
    return _parse_table(table, root)

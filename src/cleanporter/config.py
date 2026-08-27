"""Configuration: exemptions, enforcement scope, and file selection.

The Google style guide explicitly exempts ``typing``; the default allowlist
extends that to the other places where importing names is idiomatic and
blessed (``typing_extensions``, ``collections.abc``, ``__future__``).
Everything is configurable under ``[tool.cleanporter]`` in the nearest
``pyproject.toml``.
"""

from __future__ import annotations

import dataclasses
import pathlib
import tomllib

# Modules whose members may be imported directly by name.
DEFAULT_EXEMPT_MODULES: frozenset[str] = frozenset(
    {"typing", "typing_extensions", "collections.abc", "__future__"}
)

_VALID_SCOPES = ("all", "first-party")

_LIST_KEYS = ("exclude", "source_roots", "exempt_modules", "exempt_names")
_BOOL_KEYS = ("treat_unresolved_as_error",)
_KNOWN_KEYS = frozenset(_LIST_KEYS + _BOOL_KEYS + ("scope", "python"))


class ConfigError(ValueError):
    """Raised when [tool.cleanporter] is malformed."""


@dataclasses.dataclass(frozen=True)
class Config:
    #: Directory of the pyproject.toml this config came from (or cwd).
    root: pathlib.Path = dataclasses.field(default_factory=pathlib.Path.cwd)
    #: Glob patterns matched against project-relative POSIX paths.
    exclude: tuple[str, ...] = ()
    #: ``all`` or ``first-party``.
    scope: str = "all"
    #: Explicit first-party import roots, relative to ``root``.
    source_roots: tuple[str, ...] = ()
    #: Count CP002 findings toward the failure exit code.
    treat_unresolved_as_error: bool = False
    #: ``from MODULE import X`` is allowed when MODULE is in this set.
    exempt_modules: frozenset[str] = DEFAULT_EXEMPT_MODULES
    #: Individual bound names that are always allowed.
    exempt_names: frozenset[str] = frozenset()
    #: Interpreter used for the stdlib/third-party probe (None -> current).
    python: str | None = None

    def is_exempt(self, parent: str, name: str) -> bool:
        if name in self.exempt_names:
            return True
        # Exempt if the from-module or any ancestor is exempted.
        parts = parent.split(".")
        return any(".".join(parts[:i]) in self.exempt_modules for i in range(len(parts), 0, -1))


def _str_list(table: dict[str, object], key: str) -> tuple[str, ...]:
    value = table[key]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"tool.cleanporter.{key} must be a list of strings")
    return tuple(value)


def _parse_table(table: dict[str, object], root: pathlib.Path) -> Config:
    unknown = set(table) - _KNOWN_KEYS
    if unknown:
        raise ConfigError(f"unknown tool.cleanporter keys: {sorted(unknown)}")

    kwargs: dict[str, object] = {"root": root}
    if "exclude" in table:
        kwargs["exclude"] = _str_list(table, "exclude")
    if "source_roots" in table:
        kwargs["source_roots"] = _str_list(table, "source_roots")
    if "exempt_modules" in table:
        kwargs["exempt_modules"] = DEFAULT_EXEMPT_MODULES | frozenset(
            _str_list(table, "exempt_modules")
        )
    if "exempt_names" in table:
        kwargs["exempt_names"] = frozenset(_str_list(table, "exempt_names"))
    if "scope" in table:
        value = table["scope"]
        if not isinstance(value, str) or value not in _VALID_SCOPES:
            raise ConfigError(
                f"tool.cleanporter.scope must be one of {_VALID_SCOPES}, got {value!r}"
            )
        kwargs["scope"] = value
    if "python" in table:
        value = table["python"]
        if not isinstance(value, str):
            raise ConfigError("tool.cleanporter.python must be a string")
        kwargs["python"] = value
    for key in _BOOL_KEYS:
        if key in table:
            value = table[key]
            if not isinstance(value, bool):
                raise ConfigError(f"tool.cleanporter.{key} must be a boolean")
            kwargs[key] = value
    return Config(**kwargs)  # type: ignore[arg-type]


def find_pyproject(start: pathlib.Path) -> pathlib.Path | None:
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


def load_config(start: pathlib.Path) -> Config:
    """Load configuration walking upward from *start*; defaults if absent."""
    anchor = start.resolve()
    pyproject = find_pyproject(anchor)
    if pyproject is None:
        return Config(root=anchor if anchor.is_dir() else anchor.parent)
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    table = data.get("tool", {}).get("cleanporter", {})
    if not isinstance(table, dict):
        raise ConfigError("[tool.cleanporter] must be a TOML table")
    return _parse_table(table, pyproject.parent)

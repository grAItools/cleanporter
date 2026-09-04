"""Configuration: exemptions, enforcement scope, and file selection.

The Google style guide explicitly exempts ``typing``; the default allowlist
extends that to the other places where importing names is idiomatic and
blessed (``typing_extensions``, ``collections.abc``, ``__future__``).
Everything is configurable under ``[tool.cleanporter]`` in the nearest
``pyproject.toml``. The table is validated as it is read -- an unknown key or a
wrongly typed value raises `ConfigError`, which the CLI turns into exit code 2
rather than carrying on with a half-understood configuration.
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


def _scope(table: dict[str, object]) -> str:
    value = table["scope"]
    if not isinstance(value, str) or value not in _VALID_SCOPES:
        raise ConfigError(f"tool.cleanporter.scope must be one of {_VALID_SCOPES}, got {value!r}")
    return value


def _python(table: dict[str, object]) -> str:
    value = table["python"]
    if not isinstance(value, str):
        raise ConfigError("tool.cleanporter.python must be a string")
    return value


def _bool(table: dict[str, object], key: str) -> bool:
    value = table[key]
    if not isinstance(value, bool):
        raise ConfigError(f"tool.cleanporter.{key} must be a boolean")
    return value


def _parse_table(table: dict[str, object], root: pathlib.Path) -> Config:
    """Build a Config from a validated ``[tool.cleanporter]`` table.

    Every field is passed by name. Collecting them into a ``dict[str, object]``
    and splatting it would read shorter, but it types every argument as
    ``object`` -- so the constructor call has to be silenced, and with it any
    genuine mistake in what this function assigns. A parser whose whole job is
    to reject wrongly typed input should not be the one place that is unchecked.

    Defaults are read back off ``Config`` rather than repeated here, so a
    changed default cannot diverge from what an absent key produces. Keys are
    validated in declaration order, so a table with several bad keys reports
    the same one it always has.
    """
    unknown = set(table) - _KNOWN_KEYS
    if unknown:
        raise ConfigError(f"unknown tool.cleanporter keys: {sorted(unknown)}")

    defaults = Config(root=root)
    exclude = _str_list(table, "exclude") if "exclude" in table else defaults.exclude
    source_roots = (
        _str_list(table, "source_roots") if "source_roots" in table else defaults.source_roots
    )
    exempt_modules = (
        DEFAULT_EXEMPT_MODULES | frozenset(_str_list(table, "exempt_modules"))
        if "exempt_modules" in table
        else defaults.exempt_modules
    )
    exempt_names = (
        frozenset(_str_list(table, "exempt_names"))
        if "exempt_names" in table
        else defaults.exempt_names
    )
    scope = _scope(table) if "scope" in table else defaults.scope
    python = _python(table) if "python" in table else defaults.python
    # Validating the boolean keys stays driven by _BOOL_KEYS, but every one of
    # them has to be applied by name below; `test_every_known_key_reaches_the
    # _config` is what stops a new key being validated and then dropped.
    flags = {key: _bool(table, key) for key in _BOOL_KEYS if key in table}
    return Config(
        root=root,
        exclude=exclude,
        scope=scope,
        source_roots=source_roots,
        treat_unresolved_as_error=flags.get(
            "treat_unresolved_as_error", defaults.treat_unresolved_as_error
        ),
        exempt_modules=exempt_modules,
        exempt_names=exempt_names,
        python=python,
    )


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

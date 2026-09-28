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
import os
import pathlib
import re
import tomllib
from collections.abc import Sequence

from cleanporter import skip as skip_lib

# Modules whose members may be imported directly by name.
DEFAULT_EXEMPT_MODULES: frozenset[str] = frozenset(
    {"typing", "typing_extensions", "collections.abc", "__future__"}
)

_VALID_SCOPES = ("all", "first-party")

_LIST_KEYS = ("exclude", "source_roots", "exempt_modules", "exempt_names")
_BOOL_KEYS = ("treat_unresolved_as_error",)
_KNOWN_KEYS = frozenset(_LIST_KEYS + _BOOL_KEYS + ("scope", "python", "skip"))


class ConfigError(ValueError):
    """Raised when [tool.cleanporter] is malformed."""


@dataclasses.dataclass(frozen=True)
class Config:
    #: Directory of the pyproject.toml this config came from; `load_config`
    #: uses the first path's directory when there is none. Defaults to the
    #: cwd for a `Config` built by hand.
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
    #: Interpreter used for the stdlib/third-party probe: ``None`` (unset) or
    #: ``"auto"`` detects the project's own (``$UV_PROJECT_ENVIRONMENT`` or
    #: ``.venv`` at ``root``, or at the uv workspace root for a member, else
    #: ``$VIRTUAL_ENV``;
    #: see `cleanporter._interpreter`), ``"self"`` is cleanporter's own, in
    #: process, and anything else names one. A relative path read from
    #: pyproject.toml arrives already joined to ``root``.
    python: str | None = None
    #: Regions the author declared off-limits; see `cleanporter.skip`.
    skip: tuple[skip_lib.Rule, ...] = ()

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


def _python(table: dict[str, object], root: pathlib.Path) -> str:
    r"""The ``python`` key, with a relative *path* anchored at *root*.

    A value is a path when it contains a path separator, and a relative one is
    read against the directory of the ``pyproject.toml`` that declared it --
    as ``exclude`` and ``source_roots`` are -- so ``".venv/bin/python"`` names
    the same interpreter whichever directory cleanporter is started from. Read
    against the cwd, as it used to be, it named a different file (or none)
    from every subdirectory.

    A value with no separator (``"python3"``) is a command, not a path, and is
    left for the operating system to look up on ``PATH``: anchoring it would
    turn "whatever ``python3`` is" into "a file called ``python3`` next to
    pyproject.toml", which is a different request. The two words that are not
    commands at all, ``"auto"`` and ``"self"`` (see `Config.python`), have no
    separator either and so pass through as written; an interpreter really
    called ``auto`` or ``self`` is named with one, as ``"./self"``.

    A leading ``~`` or ``~user`` is expanded before that test, as a shell
    would have expanded it on a command line; one naming no known user is an
    error rather than a directory literally called ``~user``. Environment
    variables are *not* expanded, and no symlink is resolved, because a
    virtual environment's interpreter *is* a symlink whose location matters.

    A Windows drive (``C:\...``, ``\\server\share\...``) also makes the
    value a path, and one with its own root is left as written. A drive with
    *no* root (``C:python.exe``) is relative to that drive's current
    directory -- per-process state the project root cannot stand in for -- so
    it is rejected. Drives are recognised on every platform, so one
    configuration means the same thing wherever it is read.
    """
    value = table["python"]
    if not isinstance(value, str):
        raise ConfigError("tool.cleanporter.python must be a string")
    if not value:
        raise ConfigError("tool.cleanporter.python must not be empty; omit the key instead")
    if value.startswith("~"):
        try:
            value = str(pathlib.Path(value).expanduser())
        except RuntimeError as exc:  # no such user, or no home directory to be found
            raise ConfigError(f"tool.cleanporter.python: cannot expand {value!r}: {exc}") from exc
    return _anchor_python(value, root)


def _anchor_python(value: str, root: pathlib.Path) -> str:
    """*value*, a user-expanded ``python``, made independent of the cwd (see `_python`)."""
    windows = pathlib.PureWindowsPath(value)
    if windows.drive:
        if not windows.root:
            raise ConfigError(
                f"tool.cleanporter.python: {value!r} is relative to its drive's current "
                "directory; give a full path (C:\\venv\\python.exe) or one relative to "
                "pyproject.toml"
            )
        return value
    is_path = any(sep in value for sep in (os.sep, os.altsep) if sep)
    if is_path and not pathlib.Path(value).is_absolute():
        return str(root / value)
    return value


def _skip_rules(table: dict[str, object]) -> tuple[skip_lib.Rule, ...]:
    """Validate ``skip`` into `cleanporter.skip.Rule` objects.

    Every way a rule can be *malformed* is rejected here rather than quietly
    ignored: an unknown key, a non-string value, an uncompilable pattern, two
    name keys that could never both match, and a table with no matcher at all
    (``{}``, or one carrying nothing but a ``reason``), which constrains
    nothing and would take the entire project.

    What cannot be caught here is a rule that is well-formed and *wrong*: a
    pattern that fullmatches nothing fires never, and one that fullmatches
    everything fires always, and neither is distinguishable from a correct
    rule until it is run against real files. That is what the ``CP004`` count
    and ``--show-skipped`` are for.
    """
    value = table["skip"]
    if not isinstance(value, list):
        raise ConfigError("tool.cleanporter.skip must be a list of tables")
    rules: list[skip_lib.Rule] = []
    for position, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            raise ConfigError(f"tool.cleanporter.skip[{position}] must be a table")
        rules.append(_skip_rule(item, position))
    return tuple(rules)


def _skip_rule(item: dict[str, object], position: int) -> skip_lib.Rule:
    """One validated rule table. *position* is its 1-based place in the list."""
    where = f"tool.cleanporter.skip[{position}]"
    unknown = sorted(set(item) - set(skip_lib.RULE_KEYS))
    if unknown:
        raise ConfigError(f"{where} has unknown keys: {unknown}")
    if not set(item) & set(skip_lib.MATCHER_KEYS):
        raise ConfigError(
            f"{where} sets no matcher ({list(skip_lib.MATCHER_KEYS)}); a rule that constrains "
            "nothing would skip the whole project"
        )
    # Narrow every value to `str` once, here, so the rule is built from a
    # typed table rather than from `object`s re-checked at each use.
    patterns: dict[str, str] = {}
    for key, value in item.items():
        if not isinstance(value, str):
            raise ConfigError(f"{where}.{key} must be a string")
        patterns[key] = value
    named = [key for key in skip_lib.NAME_KEYS if key in patterns]
    if len(named) > 1:
        raise ConfigError(
            f"{where} sets more than one of {list(skip_lib.NAME_KEYS)}: {named}. Only one "
            "name pattern is kept, so the others would be silently dropped; write the "
            "nesting into one pattern instead, as in method = 'Class\\.method'"
        )
    name_key = named[0] if named else ""
    for key in ("file", "decorator", *named):
        pattern = patterns.get(key)
        if pattern is None:
            continue
        try:
            skip_lib.compile_pattern(pattern)
        except re.error as exc:
            raise ConfigError(f"{where}.{key} is not a valid regex: {pattern!r} ({exc})") from exc
    return skip_lib.Rule(
        index=position,
        file=patterns.get("file"),
        name=patterns.get(name_key) if name_key else None,
        name_key=name_key,
        kinds=skip_lib.KINDS_BY_KEY[name_key] if name_key else frozenset(),
        decorator=patterns.get("decorator"),
        reason=patterns.get("reason", ""),
    )


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
    python = _python(table, root) if "python" in table else defaults.python
    skip = _skip_rules(table) if "skip" in table else defaults.skip
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
        skip=skip,
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


def mismatch_warning(paths: Sequence[pathlib.Path]) -> str | None:
    """Warning text when *paths* do not all share the first one's pyproject.toml.

    A run loads one configuration, found from its first path (see
    `load_config`). A later path whose nearest ``pyproject.toml`` is a
    different file -- another project, or one nested inside this one -- is
    still analysed, but under the first project's rules, and nothing used to
    say so. This names the configuration in use and every path whose own is
    being ignored. It does not change which configuration wins. ``None`` when
    every path agrees.
    """
    if not paths:
        return None
    used = find_pyproject(paths[0])
    ignored: dict[pathlib.Path | None, list[str]] = {}
    for path in paths[1:]:
        own = find_pyproject(path)
        # Two spellings of one file (a case-insensitive filesystem) agree.
        same = own == used or (own is not None and used is not None and own.samefile(used))
        if not same:
            ignored.setdefault(own, []).append(str(path))
    if not ignored:
        return None
    using = str(used) if used is not None else "the built-in defaults (no pyproject.toml above it)"
    others = "; ".join(
        f"{', '.join(names)} (nearest: {own if own is not None else 'no pyproject.toml'})"
        for own, names in ignored.items()
    )
    return (
        f"the paths belong to different pyproject.toml files; only the first path's "
        f"({paths[0]}) configuration is used: {using}. Analysed under it anyway, their own "
        f"configuration ignored: {others}. Run cleanporter once per project to apply each one's"
    )

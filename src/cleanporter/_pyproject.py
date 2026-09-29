"""Dotted references in a ``pyproject.toml``: entry points, plugin addresses, and the like.

The cross-file string guard (`resolver.Resolver.named_by`) weighs every string
in the run that names a module attribute by its dotted path. A project's
``pyproject.toml`` holds such strings too, and not only under the PEP 621
tables: ``[project.scripts]``, ``[project.gui-scripts]`` and
``[project.entry-points.*]`` are the standard places, but ``[tool.poetry.scripts]``,
``[tool.poetry.plugins.*]``, a pytest ``-p`` plugin or a tool's own hook
setting name code the same way. So `references` reads *every* string value
in the file, however deep, and keeps the ones that are a reference:

* an entry point, ``module:attribute`` with optional whitespace around the
  colon and an optional ``[extras]`` suffix, both dropped -- and nothing
  else stripped, so ``"pkg.cli : main [x]"`` is ``pkg.cli:main`` but
  ``"pkg. cli:main"`` is not a reference at all;
* otherwise, a value that is a whole dotted path (`guards.dotted_reference`).

Precision is the caller's, as for strings in Python files: a candidate only
counts when its first component is first-party and the module re-exports the
name, so ``version = "0.4.0"`` or ``readme = "README.md"`` cost nothing.

TOML keeps no positions, so the line is *found*, and found strictly: the
line that assigns this key this value, as a plain one-line string, inside the
table that holds it. Anything else -- a value in an array or an inline table,
a dotted key, a multi-line or escaped string -- has no line, and the reason
names the file alone rather than a line that might be the wrong one.
"""

from __future__ import annotations

import dataclasses
import re
import tomllib
from collections.abc import Iterator

from . import guards

#: ``module : attribute [extras]`` -- whitespace allowed only around the
#: colon and before the extras.
_ENTRY_POINT = re.compile(r"(?P<module>[^\s:\[\]]+)\s*:\s*(?P<attr>[^\s:\[\]]+)(?:\s*\[[^\]]*\])?")

#: A table or array-of-tables header, with any trailing comment.
_HEADER = re.compile(r"\s*\[\[?(?P<keys>.*?)\]\]?\s*(?:#.*)?")

#: One component of a dotted TOML key: bare, basic-quoted or literal-quoted.
_KEY_PART = re.compile(
    r"""\s*(?:"(?P<basic>[^"\\]*)"|'(?P<literal>[^']*)'|(?P<bare>[A-Za-z0-9_-]+))\s*"""
)


@dataclasses.dataclass(frozen=True)
class Reference:
    """One string value of a ``pyproject.toml`` that is a dotted reference."""

    #: The reference, normalised (``pkg.cli:main`` for ``"pkg.cli : main [x]"``).
    text: str
    #: Its components (`guards.dotted_reference`).
    parts: tuple[str, ...]
    #: The line that assigns it, or ``None`` when it cannot be pinned down.
    line: int | None


def references(text: str) -> list[Reference]:
    """Every dotted reference among the string values of the TOML *text*.

    Raises `tomllib.TOMLDecodeError` for text that is not TOML.
    """
    data = tomllib.loads(text)
    lines = text.splitlines()
    found: list[Reference] = []
    for table, key, value in _strings(data, ()):
        spec = _reference(value)
        if spec is None:
            continue
        parts = guards.dotted_reference(spec)
        if parts is None:
            continue
        line = _line(lines, table, key, value) if key is not None else None
        found.append(Reference(spec, parts, line))
    return found


def _reference(value: str) -> str | None:
    """*value* as an entry point (`_ENTRY_POINT`), else as written."""
    match = _ENTRY_POINT.fullmatch(value)
    if match is not None:
        return f"{match['module']}:{match['attr']}"
    return value


def _strings(
    node: object, table: tuple[str, ...]
) -> Iterator[tuple[tuple[str, ...], str | None, str]]:
    """``(table, key, value)`` for every string under *node*.

    *key* is ``None`` for a string in an array, which no line can be pinned
    to. A nested table's strings carry the nested table's path, which is
    what a header spells -- or what an inline table would, had it one, and
    then `_line` simply finds no line.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            name = str(key)
            if isinstance(value, str):
                yield table, name, value
            else:
                yield from _strings(value, (*table, name))
    elif isinstance(node, list):
        for item in node:
            if isinstance(item, str):
                yield table, None, item
            else:
                yield from _strings(item, table)


def _header(line: str) -> tuple[str, ...] | None:
    """The table path a header line opens, or ``None`` if it is not one."""
    match = _HEADER.fullmatch(line)
    if match is None:
        return None
    keys = _dotted_key(match["keys"])
    return keys or None


def _dotted_key(spelled: str) -> tuple[str, ...]:
    """``a."b.c".d`` as ``("a", "b.c", "d")``; empty if it is not a key."""
    parts: list[str] = []
    for chunk in _split_dots(spelled):
        match = _KEY_PART.fullmatch(chunk)
        if match is None:
            return ()
        parts.append(match["basic"] or match["literal"] or match["bare"] or "")
    return tuple(parts)


def _split_dots(spelled: str) -> list[str]:
    """Split a dotted key on the dots that are not inside quotes."""
    chunks: list[str] = []
    current: list[str] = []
    quote: str | None = None
    for char in spelled:
        if quote is None and char == ".":
            chunks.append("".join(current))
            current = []
            continue
        if quote is None and char in "\"'":
            quote = char
        elif char == quote:
            quote = None
        current.append(char)
    chunks.append("".join(current))
    return chunks


def _line(lines: list[str], table: tuple[str, ...], key: str, value: str) -> int | None:
    """The one line in *table* that says ``key = "value"``, or ``None``."""
    assignment = re.compile(
        r"\s*(?:" + "|".join(re.escape(k) for k in _spellings(key)) + r")\s*=\s*"
        r"(?P<quote>[\"'])" + re.escape(value) + r"(?P=quote)\s*(?:#.*)?"
    )
    current: tuple[str, ...] = ()
    for number, line in enumerate(lines, 1):
        opened = _header(line)
        if opened is not None:
            current = opened
        elif current == table and assignment.fullmatch(line):
            return number
    return None


def _spellings(key: str) -> list[str]:
    """How *key* can be written on the left of ``=``."""
    spelled = [f'"{key}"', f"'{key}'"]
    if re.fullmatch(r"[A-Za-z0-9_-]+", key):
        spelled.append(key)
    return spelled

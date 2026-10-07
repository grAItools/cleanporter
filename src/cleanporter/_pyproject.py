# Copyright (c) 2026 grAItools
# SPDX-License-Identifier: BSD-3-Clause
# See LICENSE for the full license text.

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

TOML keeps no positions, so the line is *found*, and *proved* (`_line`): the
line that assigns this key this value, as a plain one-line string, and that
the parser agrees does. Anything else -- a value in an array or an inline
table, a dotted key, a multi-line or escaped string -- has no line, and the
reason names the file alone rather than a line that might be the wrong one.
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
    found: list[Reference] = []
    for table, key, value in _strings(data, ()):
        spec = _reference(value)
        if spec is None:
            continue
        parts = guards.dotted_reference(spec)
        if parts is None:
            continue
        line = _line(text, data, table, key, value) if key is not None else None
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


def _line(text: str, data: object, table: tuple[str, ...], key: str, value: str) -> int | None:
    """The line that provably assigns *value* to *key* in *table*, or ``None``.

    A line that *looks* like ``key = "value"`` is only a candidate: it may
    sit inside a multi-line string, or below something that looks like a
    table header and is not one (a line of a multi-line string, an element
    ``["x"]`` of a multi-line array). So each candidate is proved rather
    than trusted: its value is swapped for a sentinel, the text is parsed
    again, and the candidate is the line only if the sentinel -- and nothing
    else -- has moved, to exactly *table* and *key*. The first candidate that
    proves out is the answer; none is ``None``, never a guess.
    """
    assignment = re.compile(
        r"(?P<head>\s*(?:" + "|".join(re.escape(k) for k in _spellings(key)) + r")\s*=\s*)"
        r"(?P<quote>[\"'])" + re.escape(value) + r"(?P=quote)(?P<tail>\s*(?:#.*)?)"
    )
    sentinel = _sentinel(text)
    lines = text.split("\n")
    for index, line in enumerate(lines):
        match = assignment.fullmatch(line.rstrip("\r"))
        if match is None:
            continue
        swapped = [*lines]
        swapped[index] = f'{match["head"]}"{sentinel}"{match["tail"]}'
        try:
            proof = tomllib.loads("\n".join(swapped))
        except tomllib.TOMLDecodeError:
            continue
        if _at(proof, (*table, key)) == sentinel and _without(proof, table, key) == _without(
            data, table, key
        ):
            return index + 1
    return None


def _sentinel(text: str) -> str:
    """A string value that appears nowhere in *text*."""
    candidate, n = "cleanporter-line-probe", 0
    while candidate in text:
        n += 1
        candidate = f"cleanporter-line-probe-{n}"
    return candidate


def _at(data: object, path: tuple[str, ...]) -> object:
    """What *data* holds at *path* of table keys, or ``None``."""
    for name in path:
        if not isinstance(data, dict) or name not in data:
            return None
        data = data[name]
    return data


def _without(data: object, table: tuple[str, ...], key: str) -> object:
    """*data* with *table*'s *key* removed: what a proven swap must leave alone."""
    if not isinstance(data, dict):
        return data
    if not table:
        return {k: v for k, v in data.items() if k != key}
    head, rest = table[0], table[1:]
    return {k: (_without(v, rest, key) if k == head else v) for k, v in data.items()}


def _spellings(key: str) -> list[str]:
    """How *key* can be written on the left of ``=``."""
    spelled = [f'"{key}"', f"'{key}'"]
    if re.fullmatch(r"[A-Za-z0-9_-]+", key):
        spelled.append(key)
    return spelled

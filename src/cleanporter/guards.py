"""Whole-file safety predicates.

Each function returns ``(line, reason)`` hits. A single hit blocks the entire
file from being rewritten: libcst's scope analysis is not flow-sensitive and
these constructs make a mechanical rename unprovable, so the conservative
answer is to leave the file exactly as the author wrote it and explain why.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection

import libcst as cst

#: ``(line, human-readable reason)``.
Hit = tuple[int, str]
LineOf = Callable[[cst.CSTNode], int]


def _patterns(names: Collection[str]) -> list[tuple[str, re.Pattern[str]]]:
    return [(n, re.compile(rf"\b{re.escape(n)}\b")) for n in sorted(names)]


def find_string_mentions(
    tree: cst.Module,
    names: Collection[str],
    line_of: LineOf,
    skip_ids: frozenset[int] = frozenset(),
) -> list[Hit]:
    """Names mentioned inside string literals.

    ``__all__ = ["Widget"]`` and ``getattr(m, "Widget")`` keep working only if
    the name survives; a rename would silently break them. ``skip_ids`` exempts
    string nodes the caller is rewriting itself (lazy annotations, Task 16).
    """
    hits: list[Hit] = []
    if not names:
        return hits
    patterns = _patterns(names)

    class V(cst.CSTVisitor):
        def visit_SimpleString(self, node: cst.SimpleString) -> None:
            if id(node) in skip_ids:
                return
            for name, pattern in patterns:
                if pattern.search(node.raw_value):
                    hits.append((line_of(node), f"name '{name}' appears in a string literal"))
                    break

    tree.visit(V())
    return hits

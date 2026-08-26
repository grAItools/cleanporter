"""Whole-file safety predicates.

Each function returns ``(line, reason)`` hits. A single hit blocks the entire
file from being rewritten: libcst's scope analysis is not flow-sensitive and
these constructs make a mechanical rename unprovable, so the conservative
answer is to leave the file exactly as the author wrote it and explain why.

Product decision -- prose docstrings are exempt, doctests are not: a module,
class or function docstring that happens to name an imported symbol is
extremely common (it is *describing* the import, not depending on its exact
spelling), and treating that as unfixable would block a rewrite on a large
fraction of real files. A stale name in prose is a documentation nit, not
broken code, so ``find_string_mentions`` does not flag it. A doctest embedded
in that same docstring is different: ``>>> Thing()`` is executable, and a
rename genuinely breaks it. So docstrings are only exempt when they contain no
``>>>`` anywhere -- that is the doctest prompt, and continuation lines always
follow one, so checking for it is sufficient. This is deliberate and settled;
later guards should not re-litigate it.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection

import libcst as cst

#: ``(line, human-readable reason)``.
Hit = tuple[int, str]
LineOf = Callable[[cst.CSTNode], int]

#: The only doctest prompt; continuation lines always follow one.
_DOCTEST_MARKER = ">>>"


def _patterns(names: Collection[str]) -> list[tuple[str, re.Pattern[str]]]:
    return [(n, re.compile(rf"\b{re.escape(n)}\b")) for n in sorted(names)]


def _docstring_ids(tree: cst.Module) -> set[int]:
    """ids of ``SimpleString`` nodes that are genuine, non-doctest docstrings.

    A "genuine docstring" is identified structurally, never by guessing from
    position or content: it is the value of an ``Expr`` that is the first
    statement of a ``Module``, ``ClassDef`` or ``FunctionDef`` body. A bare
    string anywhere else -- a pseudo attribute-docstring after an assignment,
    a string inside a list, ``__all__`` -- is not a docstring and keeps
    blocking. A docstring containing ``>>>`` is a doctest and is excluded from
    the result (i.e. it keeps blocking too).
    """
    ids: set[int] = set()

    def _check(statements: Collection[cst.BaseStatement]) -> None:
        first = next(iter(statements), None)
        if not isinstance(first, cst.SimpleStatementLine) or len(first.body) != 1:
            return
        stmt = first.body[0]
        if not isinstance(stmt, cst.Expr) or not isinstance(stmt.value, cst.SimpleString):
            return
        node = stmt.value
        if _DOCTEST_MARKER not in node.raw_value:
            ids.add(id(node))

    class V(cst.CSTVisitor):
        def visit_Module(self, node: cst.Module) -> None:
            _check(node.body)

        def visit_ClassDef(self, node: cst.ClassDef) -> None:
            _check(node.body.body)

        def visit_FunctionDef(self, node: cst.FunctionDef) -> None:
            _check(node.body.body)

    tree.visit(V())
    return ids


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
    Genuine prose docstrings are exempt (see module docstring); doctests
    inside them are not.
    """
    hits: list[Hit] = []
    if not names:
        return hits
    patterns = _patterns(names)
    docstring_ids = _docstring_ids(tree)

    class V(cst.CSTVisitor):
        def visit_SimpleString(self, node: cst.SimpleString) -> None:
            if id(node) in skip_ids or id(node) in docstring_ids:
                return
            for name, pattern in patterns:
                if pattern.search(node.raw_value):
                    hits.append((line_of(node), f"name '{name}' appears in a string literal"))
                    break

    tree.visit(V())
    return hits


def find_scope_declarations(
    tree: cst.Module, names: Collection[str], line_of: LineOf
) -> list[Hit]:
    """``global`` / ``nonlocal`` declarations naming a rewritten local.

    Such a declaration keeps a module-level name writable from another scope.
    Qualifying the reads without also rewriting the writes would silently
    decouple them, so the file is left alone.
    """
    hits: list[Hit] = []
    if not names:
        return hits
    wanted = set(names)

    class V(cst.CSTVisitor):
        def visit_Global(self, node: cst.Global) -> None:
            self._record(node, "global")

        def visit_Nonlocal(self, node: cst.Nonlocal) -> None:
            self._record(node, "nonlocal")

        def _record(self, node: cst.CSTNode, keyword: str) -> None:
            clashing = [i.name.value for i in node.names if i.name.value in wanted]
            if clashing:
                hits.append(
                    (line_of(node), f"'{'/'.join(sorted(clashing))}' declared {keyword}")
                )

    tree.visit(V())
    return hits

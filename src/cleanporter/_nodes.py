# Copyright (c) 2026 grAItools
# SPDX-License-Identifier: BSD-3-Clause
# See LICENSE for the full license text.

"""Small libCST tree walks the fixer and its planning helpers use.

Each collects one kind of node, or one fact, from a whole subtree, with no
scope metadata: the callers decide what the collected nodes mean.
"""

from __future__ import annotations

import libcst as cst


def import_froms(node: cst.CSTNode) -> list[cst.ImportFrom]:
    found: list[cst.ImportFrom] = []

    class V(cst.CSTVisitor):
        def visit_ImportFrom(self, node: cst.ImportFrom) -> None:
            found.append(node)

    node.visit(V())
    return found


def plain_imports(node: cst.CSTNode) -> list[cst.Import]:
    found: list[cst.Import] = []

    class V(cst.CSTVisitor):
        def visit_Import(self, node: cst.Import) -> None:
            found.append(node)

    node.visit(V())
    return found


def strings(node: cst.CSTNode) -> list[cst.SimpleString]:
    found: list[cst.SimpleString] = []

    class V(cst.CSTVisitor):
        def visit_SimpleString(self, node: cst.SimpleString) -> None:
            found.append(node)

    node.visit(V())
    return found


def deleted_names(tree: cst.Module) -> set[str]:
    """Simple names that appear as a ``del`` target anywhere in the file.

    libcst's scope analysis records ``del x`` as an *access* of ``x``, not as
    an assignment, so a deleted name sails straight past the rebinding guard
    and lands in ``plan.name_repl`` like any other reference -- turning
    ``del Thing`` into ``del mod.Thing``, which does not unbind a local at
    all: it deletes the attribute from ``sys.modules['a.mod']``, silently
    breaking every *other* importer of that module. ``del name`` right after
    an import is a real ``__init__.py`` cleanup idiom, so this is not
    theoretical.

    Only bare names (including inside a ``del a, b`` tuple/list target)
    count. ``del obj.attr`` / ``del obj[k]`` rebind nothing, so descending
    into an ``Attribute``/``Subscript`` would only cause spurious blocks.
    """
    names: set[str] = set()

    def absorb(target: cst.BaseExpression) -> None:
        if isinstance(target, cst.Name):
            names.add(target.value)
        elif isinstance(target, (cst.Tuple, cst.List)):
            for element in target.elements:
                absorb(element.value)

    class V(cst.CSTVisitor):
        def visit_Del(self, node: cst.Del) -> None:
            absorb(node.target)

    tree.visit(V())
    return names


def interior_comments(node: cst.CSTNode) -> bool:
    """Any comment sitting *inside* this node's own whitespace.

    The kept-names line is regenerated from text via `cst.parse_statement`,
    which cannot carry interior trivia across, so a comment inside a
    parenthesized multi-line import -- including a per-name ``# noqa:`` or
    ``# type: ignore`` -- would be silently dropped. Line-level trivia
    (``leading_lines`` / ``trailing_whitespace``) belongs to the enclosing
    `SimpleStatementLine`, is carried over explicitly, and is checked
    separately; this looks only at what the regenerated statement would
    lose.
    """
    found = False

    class V(cst.CSTVisitor):
        # libcst dispatches to this exact signature, so the node parameter
        # has to stay even though only its existence matters here.
        def visit_Comment(self, node: cst.Comment) -> None:  # noqa: ARG002
            nonlocal found
            found = True

    node.visit(V())
    return found

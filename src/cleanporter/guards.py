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

Product decision -- a mention only counts when the string could *be* a
reference: matching a bare word inside a string is not enough, because the
overwhelming majority of such matches are prose that no rename can reach
(``"expected Type, got int"``, ``"--include=PATTERN"``, an f-string-free error
message). ``find_string_mentions`` therefore requires a second, structural
condition on top of the word match -- see `_string_references`.

This is a deliberate, bounded reduction in conservatism, and it is the one
guard here that gives something up. A context the *caller* knows is code --
an annotation slot, an ``__all__`` list -- is passed as ``strict_ids`` and
keeps blocking on the word match alone. What is given up is content no parse
can reach: a regex literal, a payload assembled rather than written out, a
fragment that is not valid Python on its own, source for another Python
version. Those are listed in the fixer-safety documentation; keep the two in
step.
"""

from __future__ import annotations

import ast
import re
import textwrap
from collections.abc import Callable, Collection

import libcst as cst

#: ``(line, human-readable reason)``.
Hit = tuple[int, str]
LineOf = Callable[[cst.CSTNode], int]

#: The only doctest prompt; continuation lines always follow one.
_DOCTEST_MARKER = ">>>"

#: How deep to follow strings nested inside strings (``"Sequence['Thing']"``).
#: Forward references nest in real code, but not without bound; past this the
#: string is reported as unclassifiable, which blocks.
_MAX_NESTING = 4


def _patterns(names: Collection[str]) -> list[tuple[str, re.Pattern[str]]]:
    return [(n, re.compile(rf"\b{re.escape(n)}\b")) for n in sorted(names)]


def _reference_path(content: str) -> set[str]:
    """Components of *content* read as a dotted/colon reference path, else empty.

    Covers the shapes an importable target is spelled as in a string when it
    is not valid Python on its own: an entry point (``"mypkg.cli:main"``), a
    ``setuptools`` console script, a plugin address. ``"pkg.mod.Thing"`` is
    valid Python too and would be caught by `_string_references` anyway; this
    is the fallback for the ones that are not.
    """
    head, _colon, tail = content.partition(":")
    parts = [p for segment in (head, tail) for p in segment.split(".") if p]
    if parts and all(p.isidentifier() for p in parts):
        return set(parts)
    return set()


def _parse_or_none(source: str) -> ast.Module | None:
    try:
        return ast.parse(source)
    except (SyntaxError, ValueError):
        return None


def _string_references(content: str, depth: int = 0) -> set[str]:
    """Names *content* could be referring to, if it is code rather than prose.

    The question this answers is narrow: could renaming a local binding change
    what this string means at runtime? A string can reach a binding only by
    *being* a reference to it -- a ``getattr`` argument, an eagerly evaluated
    annotation, an ``eval``/``exec`` payload, an ``importlib`` or entry-point
    address. Those are valid Python (as written, or once ``textwrap.dedent``
    has removed a block payload's indentation) or a dotted/colon path. Prose
    is neither: ``"expected Type, got int"`` does not parse, so no rename can
    reach it.

    This is *not* exhaustive, and the module docstring lists what it gives
    up. Two things follow. Contexts that are code by **declaration** rather
    than by content -- an ``__all__`` list, an annotation slot -- are never
    left to this function: the caller marks them via ``strict_ids`` and they
    block on the word match alone. ``__all__ = "Widget helper".split()`` is
    exactly why, since its content is a name list that is not Python. And
    content this function cannot parse is reported as prose, which is the
    deliberate reduction in conservatism -- not a claim that no such string
    could ever matter.

    So *content* is parsed, and the names it genuinely references are
    returned:

    * ``Name`` ids and ``Attribute`` attrs, so both halves of
      ``"pkg.mod.Thing"`` count -- an attribute name is how a dotted path
      spells its leaf, which is exactly what `monkeypatch.setattr` takes.
    * keyword-argument names, and ``import``/``from`` alias names, because a
      string carrying code that *names* the symbol is far more likely a
      template or an ``exec`` payload than prose. Neither is a reference a
      rename would actually break, but including them costs almost nothing
      and keeps the guard on the conservative side of its own contract.
    * strings nested inside the parse, recursively, up to `_MAX_NESTING`:
      ``"Sequence['Thing']"`` is a forward reference in its own right, and
      the inner ``'Thing'`` has no CST node of its own for the caller to
      have visited.

    Returns an empty set when *content* neither parses nor reads as a
    reference path. That is the only case that lets the caller stop blocking,
    and per the paragraph above it means "nothing found here", not "nothing
    could be here".
    """
    if depth > _MAX_NESTING:
        # Unclassifiable rather than prose: fall back to the name itself so
        # the caller keeps blocking. Never guess in the permissive direction.
        return {content.strip()}
    stripped = content.strip()
    tree = _parse_or_none(stripped)
    if tree is None:
        # A block payload keeps its own indentation after the outer strip, so
        # ``"\n    x = Thing()\n"`` raises IndentationError and would read as
        # prose. It is code, written out verbatim, and ``textwrap.dedent`` is
        # exactly how such a payload is fed to ``exec``.
        tree = _parse_or_none(textwrap.dedent(content).strip())
    if tree is None:
        # Not Python. It may still be a reference path; otherwise it is prose.
        return _reference_path(stripped)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.keyword) and node.arg is not None:
            found.add(node.arg)
        elif isinstance(node, ast.alias):
            found.add(node.asname or node.name.split(".")[-1])
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            found.update(node.names)
        elif isinstance(node, ast.MatchAs) and node.name:
            found.add(node.name)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            found |= _string_references(node.value, depth + 1)
    return found


def _docstring_ids(tree: cst.Module) -> set[int]:
    """Ids of ``SimpleString`` nodes that are genuine, non-doctest docstrings.

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
    strict_ids: frozenset[int] = frozenset(),
) -> list[Hit]:
    """Names a string literal could be *referring to*.

    ``__all__ = ["Widget"]`` and ``getattr(m, "Widget")`` keep working only if
    the name survives; a rename would silently break them. ``skip_ids`` exempts
    string nodes the caller is rewriting itself (lazy annotations, Task 16).
    Genuine prose docstrings are exempt (see module docstring); doctests
    inside them are not.

    Two conditions must both hold for a hit. The word match comes first and is
    unchanged -- it is cheap, and it is what bounds the cost of the second.
    The second, `_string_references`, asks whether the string is code at all;
    a string that is only prose cannot reach a binding and is not a hit. A
    doctest is a hit regardless, and so is a string whose content is not text
    (a bytes literal): undecodable means unclassifiable, and unclassifiable
    blocks.

    ``strict_ids`` names string nodes whose *context* already proves they are
    code, so the second condition is skipped and a word match alone is a hit.
    That is what separates the two reasons a string can fail to parse: prose
    (``"expected Type, got int"`` -- safe, no rename can reach it) from
    malformed code (``"Thing["`` sitting in an annotation slot -- intended as
    a type, unclassifiable, and therefore blocking). Only the caller knows
    which slot a string occupies, so only the caller can draw that line.
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
            content = node.evaluated_value
            for name, pattern in patterns:
                if not pattern.search(node.raw_value):
                    continue
                if not isinstance(content, str):
                    # A bytes literal whose *raw* text names the symbol. It
                    # cannot be parsed as a reference, so it cannot be
                    # cleared either -- report it and let the file block.
                    hits.append((line_of(node), f"name '{name}' appears in a bytes literal"))
                    return
                if (
                    id(node) in strict_ids
                    or _DOCTEST_MARKER in content
                    or name in _string_references(content)
                ):
                    hits.append((line_of(node), f"name '{name}' appears in a string literal"))
                    return

    tree.visit(V())
    return hits


def find_match_captures(tree: cst.Module, names: Collection[str], line_of: LineOf) -> list[Hit]:
    """``match`` patterns that *bind* a rewritten local rather than read it.

    ``case VAL:`` is a capture pattern: it always matches and binds ``VAL``.
    Qualifying it into ``case mod.VAL:`` turns it into a *value* pattern,
    which matches only when the subject equals ``mod.VAL`` -- a silent
    change of control flow. libcst reports the captured name as an access
    of the import, so nothing else in the fixer can tell the two apart.
    ``case Thing():`` (a class pattern) and ``case mod.Thing:`` (already a
    value pattern) are genuine references and are left to be rewritten
    normally.

    The same reasoning covers the other binding forms a pattern can use:
    ``case [*rest]`` and ``case {**rest}``.
    """
    hits: list[Hit] = []
    if not names:
        return hits
    wanted = set(names)

    class V(cst.CSTVisitor):
        def visit_MatchAs(self, node: cst.MatchAs) -> None:
            self._record(node, node.name)

        def visit_MatchStar(self, node: cst.MatchStar) -> None:
            self._record(node, node.name)

        def visit_MatchMapping(self, node: cst.MatchMapping) -> None:
            self._record(node, node.rest)

        def _record(self, node: cst.CSTNode, name: cst.Name | None) -> None:
            if name is not None and name.value in wanted:
                hits.append(
                    (
                        line_of(node),
                        (
                            f"'{name.value}' is bound by a match capture pattern; "
                            "qualifying it would turn it into a value pattern"
                        ),
                    )
                )

    tree.visit(V())
    return hits


def find_scope_declarations(tree: cst.Module, names: Collection[str], line_of: LineOf) -> list[Hit]:
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
                hits.append((line_of(node), f"'{'/'.join(sorted(clashing))}' declared {keyword}"))

    tree.visit(V())
    return hits

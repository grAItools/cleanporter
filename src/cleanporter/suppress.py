"""Inline suppressions: ``# cleanporter: ignore[CP001]`` on an import's line.

A `[tool.cleanporter.skip]` rule is the tool for a *class* of code the author
declares off-limits. For the one import a reviewer has already argued about, a
pattern in ``pyproject.toml`` is the wrong distance from the code, so the
decision can sit on the import itself::

    from gt4py.next import broadcast  # cleanporter: ignore[CP001]

**The grammar is strict.** A comment is split at each ``#``, so ``# noqa:
F401  # cleanporter: ignore[CP001]`` works. A piece that begins, after
optional whitespace, with ``cleanporter`` and a colon -- in any case, with
any spacing before the colon -- is meant as a directive, and must then be
exactly: ``cleanporter:`` (lowercase, no space before the colon), optional
whitespace, ``ignore[``, one or more codes separated by commas (whitespace
around each allowed), ``]``, and then either nothing or whitespace followed
by any text. ``#cleanporter:ignore[CP001]`` and ``# cleanporter:
ignore[CP001, CP002]  -- vendored`` are accepted; ``# Cleanporter:
ignore[CP001]``, ``# cleanporter : ignore[CP001]``, ``ignore [CP001]`` and
``ignore[cp001]`` are not. Only `CP001`, `CP002` and `CP003` can be named --
the findings a suppression can replace. A bare ``ignore`` is refused for the reason the repository
refuses a bare ``# noqa``: it would silence findings nobody has seen yet.
`CP004` is already the author's own decision, and `CP005` is the report that
a suppression did nothing, which silencing would defeat. A comment that breaks
any of these rules suppresses **nothing** and is reported as a warning naming
its file and line (`problems`); a warning, not an operational error, because
the run is still sound -- the findings it would have suppressed are reported.

**Attachment is by physical line**, and a comment covers:

1. every name imported by a ``from`` statement that *starts* on its line, and
2. every imported name *written* on its line.

So a trailing comment on a one-line import covers the whole statement; in a
parenthesised import spanning lines, a comment on the ``from ... import (``
line covers the whole statement, and one ending a later line covers only the
names on that line. A comment on a line of its own, on the closing ``)``, or
on anything that is not a ``from`` import covers nothing -- and so is always
reported as unused. Lines are counted as libcst counts them, which is how
every other line in a report is counted.

What a match does is `analyze.Decider.decide`'s business: the finding the
decision would have reported becomes a `CP004`, exactly as a skip rule's
does, so the fixer keeps the name on the path a skip already takes and the
all-or-nothing contract is untouched. What never matched anything becomes a
`CP005`, from `analyze.analyze_record`.
"""

from __future__ import annotations

import dataclasses
import pathlib
import re
from collections.abc import Mapping

import libcst as cst
from libcst import metadata

from cleanporter import model

#: The codes a suppression may name, by the status of the finding each is.
CODES: Mapping[model.Status, str] = {
    model.Status.VIOLATION: "CP001",
    model.Status.UNRESOLVED: "CP002",
    model.Status.SKIPPED: "CP003",
}

#: What a comment piece starts with when it is meant as a directive: loose on
#: purpose, so that a near miss (``Cleanporter:``, ``cleanporter :``) is
#: warned about rather than silently ignored.
_ATTEMPT = re.compile(r"cleanporter\s*:", re.IGNORECASE)

_IGNORE = re.compile(r"cleanporter:\s*ignore(?:\[(?P<codes>[^\]]*)\])?(?P<tail>.*)", re.DOTALL)
_CODE = re.compile(r"CP\d{3}")
_FORM = "`# cleanporter: ignore[CODE, ...]`"

#: Why a well-formed code still cannot be named, for the warning.
_UNSUPPRESSIBLE = {
    "CP004": "CP004 is already a skip and cannot be suppressed",
    "CP005": "CP005 (an unused suppression) cannot itself be suppressed; remove the comment",
}


@dataclasses.dataclass(frozen=True)
class Suppression:
    """One well-formed suppression comment."""

    line: int
    column: int
    #: The codes it names, in the order written, without duplicates.
    codes: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class Hit:
    """A finding a suppression replaced: its code, and every comment naming it."""

    code: str
    by: tuple[Suppression, ...]


@dataclasses.dataclass(frozen=True)
class Suppressions:
    """Every suppression comment in one file, and what each can cover."""

    #: Well-formed suppressions, in source order.
    comments: tuple[Suppression, ...] = ()
    #: ``(line, message)`` for each comment that suppresses nothing because
    #: it is malformed; `warnings` spells them for a run.
    problems: tuple[tuple[int, str], ...] = ()
    #: The line each ``from`` statement starts on, and the line each of its
    #: imported names is written on. Only filled in when `comments` is not
    #: empty: without a suppression there is nothing to look up.
    lines: Mapping[cst.CSTNode, int] = dataclasses.field(default_factory=dict)
    #: `comments` by line.
    by_line: Mapping[int, Suppression] = dataclasses.field(
        default_factory=dict, init=False, compare=False
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "by_line", {s.line: s for s in self.comments})

    def covering(
        self, node: cst.ImportFrom, alias: cst.ImportAlias | None
    ) -> tuple[Suppression, ...]:
        """The suppressions covering the name *alias* of *node* (see the module docstring)."""
        if not self.by_line:
            return ()
        found: list[Suppression] = []
        for key in (node, alias):
            line = self.lines.get(key) if key is not None else None
            hit = self.by_line.get(line) if line is not None else None
            if hit is not None and hit not in found:
                found.append(hit)
        return tuple(found)

    def match(self, node: cst.ImportFrom, alias: cst.ImportAlias | None, code: str) -> Hit | None:
        """The `Hit` when a suppression covering this name lists *code*, else None."""
        by = tuple(s for s in self.covering(node, alias) if code in s.codes)
        return Hit(code, by) if by else None

    def warnings(self, path: pathlib.Path) -> list[str]:
        """One warning per malformed comment, naming *path* and its line."""
        return [
            f"{path}:{line}: {message}; it suppresses nothing" for line, message in self.problems
        ]


#: Shared "no suppression in this file".
EMPTY = Suppressions()


def collect(tree: cst.Module, positions: Mapping[cst.CSTNode, metadata.CodeRange]) -> Suppressions:
    """Every suppression comment in *tree*, and the lines they are looked up by.

    *positions* is the tree's resolved ``PositionProvider`` mapping. Callers
    check `may_hold` against the source first; this walks the tree regardless.
    """
    collector = _Collector(positions)
    tree.visit(collector)
    if not collector.comments:
        return Suppressions(problems=tuple(collector.problems))
    return Suppressions(tuple(collector.comments), tuple(collector.problems), collector.lines)


def may_hold(source: str) -> bool:
    """Whether *source* could hold a directive at all: a search, not a walk."""
    return _ATTEMPT.search(source) is not None


def parse(text: str) -> tuple[tuple[str, ...] | None, list[str]]:
    """The codes comment *text* suppresses, and why any directive in it is refused.

    ``(None, [])`` for a comment with no directive at all. Any refusal makes
    the whole comment suppress nothing: the codes are ``None`` then too.
    """
    codes: list[str] = []
    problems: list[str] = []
    found = False
    for piece in text.split("#")[1:]:
        segment = piece.strip()
        if not _ATTEMPT.match(segment):
            continue
        found = True
        problem = _parse_directive(segment, codes)
        if problem is not None:
            problems.append(problem)
    if not found or problems:
        return None, problems
    return tuple(dict.fromkeys(codes)), problems


def _parse_directive(segment: str, codes: list[str]) -> str | None:
    """Append *segment*'s codes to *codes*; return why it is refused, if it is."""
    match = _IGNORE.fullmatch(segment)
    if match is None or (match["tail"] and not match["tail"][0].isspace()):
        return f"`# {segment}` is not a suppression: expected {_FORM}"
    if match["codes"] is None:
        if match["tail"].strip():
            return f"`# {segment}` is not a suppression: expected {_FORM}"
        return (
            "a bare `# cleanporter: ignore` would silence every finding on the line; "
            f"name the codes it suppresses, as {_FORM}"
        )
    for raw in match["codes"].split(","):
        code = raw.strip()
        if not code:
            return f"`# {segment}` has an empty code: expected {_FORM}"
        if not _CODE.fullmatch(code):
            return f"`# {segment}` names {code!r}, which is not a finding code: expected {_FORM}"
        if code in _UNSUPPRESSIBLE:
            return f"`# {segment}`: {_UNSUPPRESSIBLE[code]}"
        if code not in CODES.values():
            return (
                f"`# {segment}` names {code}, which is not a finding code a suppression "
                f"can name ({', '.join(CODES.values())})"
            )
        codes.append(code)
    return None


class _Collector(cst.CSTVisitor):
    """The one walk `collect` makes: comments, and the lines imports sit on."""

    def __init__(self, positions: Mapping[cst.CSTNode, metadata.CodeRange]) -> None:
        super().__init__()
        self._positions = positions
        self.comments: list[Suppression] = []
        self.problems: list[tuple[int, str]] = []
        self.lines: dict[cst.CSTNode, int] = {}

    def visit_Comment(self, node: cst.Comment) -> None:
        codes, problems = parse(node.value)
        start = self._positions[node].start
        self.problems.extend((start.line, problem) for problem in problems)
        if codes is not None:
            self.comments.append(Suppression(start.line, start.column, codes))

    def visit_ImportFrom(self, node: cst.ImportFrom) -> None:
        self.lines[node] = self._positions[node].start.line
        if not isinstance(node.names, cst.ImportStar):
            for alias in node.names:
                self.lines[alias] = self._positions[alias].start.line

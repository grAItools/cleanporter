"""Configured skips: the regions of a file the author declares off-limits.

Some bindings are load-bearing for a consumer no analysis of the file can see.
A function body under ``@gtx.field_operator`` is re-parsed by GT4Py's own
frontend, which rejects a module-qualified call outright; a ``conftest.py``
namespace *is* pytest's fixture registry. Both look like ordinary Python and
resolve perfectly -- the resolver is right and the rewrite is still wrong. The
only thing that knows is the author, so ``[tool.cleanporter.skip]`` is where
they say so.

A rule is a table of patterns that must *all* match the same definition (AND);
the list of rules is an OR. Every pattern is `re.fullmatch` against a small,
documented set of candidate strings -- see `Rule` -- so that "does this match"
never depends on where in a string the pattern happened to land.

Two things follow from a rule matching, and the second is the one that makes
the feature work:

1. an import *inside* a skipped region is neither reported nor rewritten, and
2. a binding whose name appears anywhere inside a skipped region is **pinned**:
   never rewritten, wherever its import statement sits.

Pinning is what protects a module-level ``from gt4py.next import broadcast``
from a use inside a field operator two hundred lines below. Without it the
import would be rewritten and the skipped region left holding an unbound name
-- the exact failure this exists to prevent.

The pin test is deliberately **token-based, not scope-based**: does the
identifier appear as any `libcst.Name` inside a skipped span, or as a name a
string in that span could be referring to? A local variable of the same name
pins the import too, and so does ``mod.go()``, whose ``go`` is an attribute
leaf rather than a reference. That over-approximates, and only ever in the
direction of declining a rewrite. It also lets `analyze` and `rewrite` share
one predicate with no scope metadata in the analysis path, and the two
agreeing is a correctness requirement rather than a nicety: if the check were
the more permissive of the pair, ``--fix`` would rewrite something ``check``
had called skipped.

The invariant the module is built to keep, asserted directly in
`tests/test_rewrite`: **a skip never changes how a name is rewritten, and
never causes a name to be rewritten that would not have been.** Nothing here
reaches the resolver, so a skip never supplies a verdict; a skipped name joins
the fixer's existing ``keep`` list -- the same path a re-export already takes
-- so all-or-nothing per file is untouched.

It is *not* true that a skip can only ever make ``--fix`` do less, and the
difference is worth being exact about. Every whole-file guard is keyed on the
set of names being rewritten (`rewrite._Fixer._run_guards`), so keeping a name
takes its blocker with it: a file declined entirely because ``__all__``
mentions the one name a rule now pins will have its *other* imports rewritten.
That is sound -- the pinned name stays bound, and the guard still fires for
every name still being rewritten -- but it means a rule can cause more of a
file to change, not less. ``exempt_names`` has always behaved this way; a skip
is not special.
"""

from __future__ import annotations

import dataclasses
import functools
import pathlib
import re
from collections.abc import Mapping, Sequence

import libcst as cst
from libcst import metadata

from cleanporter import guards

#: Node kinds each name key admits. A ``def`` directly inside a class body is a
#: ``method`` and nothing else; one nested in a function is a ``function``.
KINDS_BY_KEY: Mapping[str, frozenset[str]] = {
    "function": frozenset({"function"}),
    "method": frozenset({"method"}),
    "class": frozenset({"class"}),
    "symbol": frozenset({"function", "method", "class"}),
}

#: Keys that select a definition *by name*. At most one may appear in a rule,
#: because a `Rule` keeps one name pattern and a second would be silently
#: dropped -- and for ``function``/``method``/``class``, which name mutually
#: exclusive node kinds, a pair could not have matched anything anyway.
#: `config` rejects the combination rather than accept a rule that does less
#: than it says. "the method y of class X" is spelled ``method = "X\\.y"``.
NAME_KEYS: tuple[str, ...] = tuple(KINDS_BY_KEY)

#: Keys that actually select something. A rule needs at least one: without a
#: matcher there is nothing to constrain it, so it would take the whole
#: project -- which is why ``{ reason = "..." }`` is a configuration error and
#: not a comment.
MATCHER_KEYS: tuple[str, ...] = (*NAME_KEYS, "file", "decorator")

#: Every key a rule table may carry.
RULE_KEYS: tuple[str, ...] = (*MATCHER_KEYS, "reason")


@functools.cache
def compile_pattern(pattern: str) -> re.Pattern[str]:
    """Compile *pattern*, caching it. Raises `re.error` on a bad pattern.

    `config` calls this while validating, so an uncompilable pattern is a
    configuration error rather than a crash in the middle of a run.
    """
    return re.compile(pattern)


@dataclasses.dataclass(frozen=True)
class Rule:
    r"""One ``[tool.cleanporter.skip]`` table.

    Every pattern present must match, and each is matched with `re.fullmatch`
    against every candidate for its key:

    ``file``
        The file's path relative to the project root, POSIX-spelled. A file
        that does not live under the root -- possible when a path argument
        points outside the project -- offers its absolute path instead, since
        it has no relative one.
    ``function`` / ``method`` / ``class`` / ``symbol``
        The definition's bare name; its qualified name within the module
        (``Cache.get``); and its ``module:qualname`` address in the spelling
        `pkgutil.resolve_name` takes (``pkg.mod:Cache.get``), when the file's
        module name is known.
    ``decorator``
        The decorator as a dotted name with any call stripped
        (``@gtx.program(backend=...)`` -> ``gtx.program``), and that name's
        last component (``program``). The pair is what lets
        ``decorator = "field_operator"`` cover ``@field_operator``,
        ``@gtx.field_operator`` and ``@gt4py.next.field_operator`` alike, while
        ``decorator = "gtx\\.field_operator"`` still pins one spelling.
        A decorator that is neither a dotted name nor a call on one offers no
        candidate and matches nothing.
    """

    #: 1-based position in the configured list, for the report.
    index: int = 0
    file: str | None = None
    #: The pattern of whichever name key was used, and that key's spelling.
    name: str | None = None
    name_key: str = ""
    kinds: frozenset[str] = frozenset()
    decorator: str | None = None
    #: Free text from the rule, echoed in `CP004` so a report says *why*.
    reason: str = ""

    @property
    def whole_file(self) -> bool:
        """True when the rule selects files rather than definitions in them."""
        return self.name is None and self.decorator is None

    def describe(self) -> str:
        """How this rule is named in a `CP004` finding."""
        parts = [f"{key}={value!r}" for key, value in self._matchers()]
        text = f"skip rule #{self.index} ({', '.join(parts)})"
        return f"{text}: {self.reason}" if self.reason else text

    def _matchers(self) -> list[tuple[str, str]]:
        pairs: list[tuple[str, str]] = []
        if self.file is not None:
            pairs.append(("file", self.file))
        if self.name is not None:
            pairs.append((self.name_key, self.name))
        if self.decorator is not None:
            pairs.append(("decorator", self.decorator))
        return pairs


@dataclasses.dataclass(frozen=True)
class Skipped:
    """What one file's rules matched: whole file, or line spans plus pins."""

    #: The rule that took the whole file, if one did.
    file_rule: Rule | None = None
    #: ``(first line, last line, rule)`` per matched definition, inclusive.
    spans: tuple[tuple[int, int, Rule], ...] = ()
    #: Identifier -> the rule whose span it appears in. See the module
    #: docstring on why this is token-based.
    names: Mapping[str, Rule] = dataclasses.field(default_factory=dict)
    #: `spans` indexed by line. Both `covers` and the name harvest ask "which
    #: rule owns this line?" for every node in the file, and scanning the span
    #: list for each was quadratic -- 1.7s on a 13k-line file with 400 matched
    #: definitions. Derived, never passed: ``init=False`` plus an
    #: unconditional rebuild is what makes "an instance cannot carry spans it
    #: does not index" true of `dataclasses.replace` too, and `regions` builds
    #: its result with exactly that.
    lines: Mapping[int, Rule] = dataclasses.field(default_factory=dict, init=False, compare=False)

    def __post_init__(self) -> None:
        lines: dict[int, Rule] = {}
        for start, end, rule in self.spans:
            for line in range(start, end + 1):
                # An enclosing span is recorded first and wins.
                lines.setdefault(line, rule)
        object.__setattr__(self, "lines", lines)

    @property
    def whole_file(self) -> bool:
        return self.file_rule is not None

    def covers(self, line: int) -> Rule | None:
        """The rule skipping *line*, or None."""
        if self.file_rule is not None:
            return self.file_rule
        return self.lines.get(line)

    def pin(self, name: str) -> Rule | None:
        """The rule pinning *name*, or None. See the module docstring."""
        if self.file_rule is not None:
            return self.file_rule
        return self.names.get(name)


#: Shared "nothing is skipped". Returned without walking anything when no rule
#: applies, which is what keeps the feature free for everyone not using it.
EMPTY = Skipped()


def file_candidates(path: pathlib.Path, root: pathlib.Path) -> tuple[str, ...]:
    """Strings a ``file`` pattern is matched against. See `Rule`."""
    resolved = path.resolve()
    try:
        return (resolved.relative_to(root.resolve()).as_posix(),)
    except ValueError:
        # Outside the project root, so there is no relative spelling to match.
        return (resolved.as_posix(),)


def matches(pattern: str | None, candidates: Sequence[str]) -> bool:
    """True when *pattern* is absent, or fully matches any candidate."""
    if pattern is None:
        return True
    compiled = compile_pattern(pattern)
    return any(compiled.fullmatch(candidate) for candidate in candidates)


def regions(
    tree: cst.Module,
    positions: Mapping[cst.CSTNode, metadata.CodeRange],
    rules: Sequence[Rule],
    paths: Sequence[str],
    module: str = "",
) -> Skipped:
    """What *rules* skip in this file, and the names those regions pin.

    *paths* is what a ``file`` pattern is matched against, from
    `file_candidates`; *module* is the file's dotted module name, or ``""``
    when it is not under a known import root.

    *positions* is the caller's already-resolved ``PositionProvider`` mapping:
    the analysis path has one anyway, and resolving a second would be the
    single most expensive thing this function could do.

    Use `region_spans` when only the regions are wanted: the pin harvest is a
    second full-tree walk that parses string content, and it is dead work for
    a caller asking "which regions does this source have?" rather than "what
    may I rewrite?".
    """
    skipped = region_spans(tree, positions, rules, paths, module)
    if not skipped.spans:
        return skipped
    return dataclasses.replace(skipped, names=_names_in(tree, positions, skipped.lines))


def region_spans(
    tree: cst.Module,
    positions: Mapping[cst.CSTNode, metadata.CodeRange],
    rules: Sequence[Rule],
    paths: Sequence[str],
    module: str = "",
) -> Skipped:
    """`regions`, without harvesting the names each region pins."""
    if not rules:
        return EMPTY
    applicable = [rule for rule in rules if matches(rule.file, paths)]
    for rule in applicable:
        if rule.whole_file:
            return Skipped(file_rule=rule)
    definition_rules = [rule for rule in applicable if not rule.whole_file]
    if not definition_rules:
        return EMPTY

    finder = _Finder(definition_rules, positions, module)
    tree.visit(finder)
    if not finder.spans:
        return EMPTY
    return Skipped(spans=tuple(finder.spans))


def _dotted(node: cst.BaseExpression) -> str | None:
    """*node* as a dotted name, unwrapping a call. None when it is neither."""
    if isinstance(node, cst.Call):
        return _dotted(node.func)
    if isinstance(node, cst.Name):
        return node.value
    if isinstance(node, cst.Attribute):
        head = _dotted(node.value)
        return f"{head}.{node.attr.value}" if head is not None else None
    return None


def _decorator_candidates(decorators: Sequence[cst.Decorator]) -> list[str]:
    out: list[str] = []
    for decorator in decorators:
        dotted = _dotted(decorator.decorator)
        if dotted is not None:
            out.append(dotted)
            out.append(dotted.rpartition(".")[2])
    return out


class _Finder(cst.CSTVisitor):
    """Collect the span of every definition a rule matches.

    A definition's span runs from its **first decorator** to its last line.
    libCST's ``CodeRange`` for a ``FunctionDef`` starts at the ``def``, with
    the decorators as children that begin earlier, so the start has to be
    widened explicitly -- a span that began at the ``def`` would leave
    ``@gtx.program(backend=run_gtfn)`` outside the region it introduces.
    """

    def __init__(
        self,
        rules: Sequence[Rule],
        positions: Mapping[cst.CSTNode, metadata.CodeRange],
        module: str,
    ) -> None:
        super().__init__()
        self._rules = rules
        self._positions = positions
        self._module = module
        self.spans: list[tuple[int, int, Rule]] = []
        #: Enclosing definition names, innermost last, for the qualified name.
        self._stack: list[str] = []
        #: Whether each enclosing definition is a class, for function/method.
        self._is_class: list[bool] = []

    def _span(self, node: cst.FunctionDef | cst.ClassDef) -> tuple[int, int]:
        start = self._positions[node].start.line
        for decorator in node.decorators:
            start = min(start, self._positions[decorator].start.line)
        return start, self._positions[node].end.line

    def _name_candidates(self, name: str) -> list[str]:
        qualname = ".".join([*self._stack, name])
        candidates = [name, qualname]
        if self._module:
            candidates.append(f"{self._module}:{qualname}")
        return candidates

    def _consider(self, node: cst.FunctionDef | cst.ClassDef, kind: str) -> None:
        names = self._name_candidates(node.name.value)
        decorators = _decorator_candidates(node.decorators)
        for rule in self._rules:
            if rule.name is not None and (kind not in rule.kinds or not matches(rule.name, names)):
                continue
            if not matches(rule.decorator, decorators):
                continue
            start, end = self._span(node)
            self.spans.append((start, end, rule))
            return

    def visit_FunctionDef(self, node: cst.FunctionDef) -> None:
        self._consider(node, "method" if self._is_class and self._is_class[-1] else "function")
        self._stack.append(node.name.value)
        self._is_class.append(False)

    def leave_FunctionDef(self, original_node: cst.FunctionDef) -> None:  # noqa: ARG002 - libcst hook signature
        self._stack.pop()
        self._is_class.pop()

    def visit_ClassDef(self, node: cst.ClassDef) -> None:
        self._consider(node, "class")
        self._stack.append(node.name.value)
        self._is_class.append(True)

    def leave_ClassDef(self, original_node: cst.ClassDef) -> None:  # noqa: ARG002 - libcst hook signature
        self._stack.pop()
        self._is_class.pop()


def _names_in(
    tree: cst.Module,
    positions: Mapping[cst.CSTNode, metadata.CodeRange],
    lines: Mapping[int, Rule],
) -> dict[str, Rule]:
    """Every identifier appearing inside a skipped line, mapped to its rule.

    Every `libcst.Name`, whatever it is doing there -- an attribute's leaf, a
    keyword argument, a parameter. Over-collecting is the safe direction: each
    name collected here is one the fixer will decline to rewrite.

    **And every name a string in the region could be referring to**, read with
    `guards.string_references`. A lazy annotation is not a `Name` node, so a
    region whose only use of an import is ``def op(a: "Field")`` would not pin
    ``Field`` on the token scan alone -- the import would be rewritten, and
    the fixer's own annotation-string pass would then edit that annotation
    *inside* the region, which is precisely what a skip promises will not
    happen. Reusing `guards`' reader rather than matching words keeps this on
    the same settled line the string guard already draws: content that parses
    as code can be a reference, prose cannot.

    It inherits that reader's giveaways as well. A concatenation with an
    f-string part (``f"Th" "ing"``) has no ``evaluated_value``, a lone
    f-string is not a `libcst.SimpleString` at all, and ``"Th" + "ing"`` is an
    expression rather than a literal -- none of the three pins ``Thing``. That
    is symmetrical rather than lucky: `rewrite._annotation_strings` collects
    only `libcst.SimpleString`, so the fixer does not rewrite those shapes
    either, and the region comes out byte-identical regardless. What is left
    is the pre-existing gap in the whole-file string guard, which misses them
    identically in a file with no rule at all.
    """
    found: dict[str, Rule] = {}

    class Collect(cst.CSTVisitor):
        def visit_Name(self, node: cst.Name) -> None:
            rule = lines.get(positions[node].start.line)
            if rule is not None:
                found.setdefault(node.value, rule)

        def visit_SimpleString(self, node: cst.SimpleString) -> None:
            self._absorb(node, node.evaluated_value)

        def visit_ConcatenatedString(self, node: cst.ConcatenatedString) -> None:
            # Its parts are visited too, but separately they are not the
            # name: ``"Fie" "ld"`` harvests `Fie` and `ld` and never `Field`,
            # which is exactly the reference a lazy annotation makes.
            self._absorb(node, node.evaluated_value)

        def _absorb(self, node: cst.CSTNode, content: str | bytes | None) -> None:
            rule = lines.get(positions[node].start.line)
            if rule is None or not isinstance(content, str):
                return
            for name in guards.string_references(content):
                found.setdefault(name, rule)

    tree.visit(Collect())
    return found

r"""Alias conventions: which name a module must be bound under, and where.

A project that writes ``import numpy as np`` everywhere wants two things from
this tool: to be told when a file binds ``numpy`` as ``npy`` (``CP006``), and
for ``--fix`` to write ``np`` -- not ``numpy`` -- when it rewrites ``from
numpy import array``. Both questions have one answer, `Conventions.expected`,
and `analyze` and `rewrite` both ask it, so a name the fixer allocates is by
construction one the check accepts.

**Ordering.** Rules are an ordered list and the *first* rule that matches
wins. That is the whole precedence model, and it is what expresses an
exception without a negation key: "``gt4py.next`` is ``gtx`` everywhere but
inside ``gt4py.next`` itself" is an ``importer``-scoped rule for the package
placed before the unconditional one. Two unconditional rules for the same
``module`` text would make the second unreachable, so `parse_rules` refuses
them; overlapping *patterns* are the point of ordering and are allowed.

**Patterns.** ``module`` is a dotted name whose components may be ``*``
(exactly one component) or ``**`` (one or more). No other wildcard exists --
no ``foo*`` -- so a pattern reads as a module path and matches as one:
``gt4py.next.*`` matches ``gt4py.next.ffront`` but not ``gt4py.next`` or
``gt4py.next.ffront.decorator``. ``importer`` and ``file`` are regexes,
`re.fullmatch`\ed like a ``[tool.cleanporter.skip]`` rule's, against the
importing file's dotted module name and its project-relative POSIX path. A
file with no known module name never matches an ``importer`` pattern: an
unknown name is not evidence of any particular one.

**Names.** ``as`` is an identifier, a template in which ``{leaf}`` stands
for the matched module's last component (``gtx_{leaf}``), or ``false``: the
module's own name, which is ``import M`` for a top-level module and ``from P
import L`` for a submodule -- a binding named after its leaf.

**Ruff's table is a lower-priority default.** ``aliases`` and
``extend-aliases`` from ``[tool.ruff.lint.flake8-import-conventions]`` (or
the legacy ``[tool.ruff.flake8-import-conventions]``) in the same
``pyproject.toml`` become unconditional exact rules appended after every
cleanporter rule, so a cleanporter rule always wins. Ruff's *built-in*
default table is deliberately not reproduced: it changes between ruff
versions, and inventing it would be the configuration equivalent of the
resolver guessing -- a convention nobody wrote down.

**Why declining beats ``np_2``.** Without a rule, a new binding whose leaf is
taken gets a ``_2`` suffix, which is harmless: nothing asked for a particular
name. With a rule, a suffixed name is a ``CP006`` the fixer itself wrote.
`rewrite` therefore declines the whole file (``CP003``) when the configured
name is taken, keeping both halves of the contract: the fix is all-or-nothing,
and ``--fix`` never introduces a finding.

**Why only proven modules.** A ``from P import L`` binds a module only when
the resolver proves ``P.L`` is one; anything else is ``CP001``'s or
``CP002``'s business, and a ``CP006`` about a name that might be a class would
be a guess. ``import M`` needs no resolver: the syntax proves it.
"""

from __future__ import annotations

import dataclasses
import functools
import keyword
import re
import string
from collections.abc import Mapping

#: Every key an ``[[tool.cleanporter.alias]]`` table may carry.
RULE_KEYS: tuple[str, ...] = ("module", "as", "importer", "file", "reason")

#: The ruff tables read for defaults, in the order looked up: the first
#: present wins, as the ``lint`` section outranks the legacy one in ruff.
RUFF_TABLES: tuple[tuple[str, ...], ...] = (
    ("lint", "flake8-import-conventions"),
    ("flake8-import-conventions",),
)

#: The keys of a ruff table read, in order; a later one overrides an earlier
#: one for the same module, as ``extend-aliases`` overrides ``aliases``.
RUFF_KEYS: tuple[str, ...] = ("aliases", "extend-aliases")

_LEAF = "leaf"


class AliasConfigError(ValueError):
    """An alias rule, or a ruff alias table, that cannot be used as written."""


def usable_name(name: str) -> bool:
    """Whether *name* can be bound by an import: an identifier, not a keyword, not ``__debug__``.

    ``import json as __debug__`` is a `SyntaxError` like ``import json as if``.
    Soft keywords (``match``, ``type``, ``_``) are ordinary names here.
    """
    return name.isidentifier() and not keyword.iskeyword(name) and name != "__debug__"


@functools.cache
def compile_module_pattern(pattern: str) -> re.Pattern[str]:
    """*pattern*, a ``module`` pattern, as a regex; `AliasConfigError` if malformed."""
    parts: list[str] = []
    for component in pattern.split("."):
        if component == "*":
            parts.append(r"[^.]+")
        elif component == "**":
            parts.append(r"[^.]+(?:\.[^.]+)*")
        elif component.isidentifier():
            parts.append(re.escape(component))
        else:
            raise AliasConfigError(
                f"{pattern!r} is not a module pattern: each dotted component must be an "
                "identifier, '*' (one component) or '**' (one or more)"
            )
    return re.compile(r"\.".join(parts))


@functools.cache
def _compile_regex(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern)


def render(template: str, leaf: str) -> str:
    """*template*, a validated ``as``, with ``{leaf}`` replaced by *leaf*."""
    return "".join(
        literal + (leaf if field is not None else "")
        for literal, field, _spec, _conversion in string.Formatter().parse(template)
    )


def check_template(template: str) -> None:
    """Raise `AliasConfigError` unless *template* is a usable ``as`` value.

    The only field is ``{leaf}``, with no conversion or format spec; stray or
    escaped braces are refused. With a sample identifier for ``{leaf}`` the
    result must be a `usable_name`. A template can still render an unusable
    name for some particular leaf (``i{leaf}`` for a module ``f``); that is
    only known once a module is matched, and `Expectation.usable` says so.
    """
    try:
        pieces = list(string.Formatter().parse(template))
    except ValueError as exc:
        raise AliasConfigError(f"{template!r} is not a valid name template ({exc})") from exc
    for literal, field, spec, conversion in pieces:
        if "{" in literal or "}" in literal:
            raise AliasConfigError(f"{template!r} has a stray brace; the only field is {{leaf}}")
        if field is not None and (field != _LEAF or spec or conversion):
            raise AliasConfigError(f"{template!r} has a field other than {{leaf}}")
    sample = render(template, "sample")
    if not usable_name(sample):
        raise AliasConfigError(f"{template!r} does not make a name an import can bind")


@dataclasses.dataclass(frozen=True)
class Rule:
    """One alias rule: ``[[tool.cleanporter.alias]]``, or a ruff default."""

    #: The ``module`` pattern (see the module docstring).
    module: str
    #: The ``as`` template, or ``None`` for ``as = false``: the module's own name.
    alias: str | None
    importer: str | None = None
    file: str | None = None
    #: Free text, echoed in ``CP006`` and in the fixer's ``CP003``.
    reason: str = ""
    #: How the rule is named in a message: ``alias rule #2`` or the ruff key.
    origin: str = ""

    @property
    def unconditional(self) -> bool:
        return self.importer is None and self.file is None

    def matches(self, module: str, importer: str, path: str) -> bool:
        """Whether this rule applies to *module* imported by *importer* in *path*."""
        if compile_module_pattern(self.module).fullmatch(module) is None:
            return False
        if self.importer is not None and not (
            importer and _compile_regex(self.importer).fullmatch(importer)
        ):
            return False
        return self.file is None or _compile_regex(self.file).fullmatch(path) is not None

    def describe(self) -> str:
        """How this rule is named in a finding."""
        parts = [f"module={self.module!r}"]
        if self.importer is not None:
            parts.append(f"importer={self.importer!r}")
        if self.file is not None:
            parts.append(f"file={self.file!r}")
        text = f"{self.origin} ({', '.join(parts)})"
        return f"{text}: {self.reason}" if self.reason else text


@dataclasses.dataclass(frozen=True)
class Expectation:
    """What the first matching rule says about one module in one file."""

    module: str
    rule: Rule
    #: The configured identifier, or ``None`` when the rule asks for the own name.
    name: str | None

    @property
    def own_name(self) -> bool:
        return self.name is None

    @property
    def usable(self) -> bool:
        """Whether `binding` can be bound at all (`usable_name`).

        False only when a ``{leaf}`` template renders a keyword or
        ``__debug__`` for this module. No binding can then satisfy the rule:
        the check reports every binding it judges, naming the rendered name,
        and the fixer declines the file rather than write it.
        """
        return self.name is None or usable_name(self.name)

    @property
    def binding(self) -> str:
        """The name a binding of `module` must have: the configured one, else its leaf."""
        return self.name if self.name is not None else self.module.rpartition(".")[2]


@dataclasses.dataclass(frozen=True)
class Conventions:
    """The ordered rules of a run. Empty is the common case and costs nothing."""

    rules: tuple[Rule, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.rules)

    def expected(self, module: str, importer: str, path: str) -> Expectation | None:
        """The first rule matching *module* in this file, or ``None``.

        *importer* is the importing file's dotted module name (``""`` when
        unknown) and *path* its project-relative POSIX path
        (`skip.file_candidates`).
        """
        for rule in self.rules:
            if rule.matches(module, importer, path):
                name = None if rule.alias is None else render(rule.alias, module.rpartition(".")[2])
                return Expectation(module, rule, name)
        return None


def parse_rules(value: object, where: str = "tool.cleanporter.alias") -> tuple[Rule, ...]:
    """Validate the ``alias`` list into rules; `AliasConfigError` on anything malformed."""
    if not isinstance(value, list):
        raise AliasConfigError(f"{where} must be a list of tables")
    rules: list[Rule] = []
    unconditional: dict[str, int] = {}
    for position, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            raise AliasConfigError(f"{where}[{position}] must be a table")
        rule = _rule(item, f"{where}[{position}]", position)
        if rule.unconditional:
            first = unconditional.setdefault(rule.module, position)
            if first != position:
                raise AliasConfigError(
                    f"{where}[{position}] repeats the unconditional module {rule.module!r} of "
                    f"{where}[{first}], so it could never apply; the first matching rule wins"
                )
        rules.append(rule)
    return tuple(rules)


def _rule(item: Mapping[object, object], where: str, position: int) -> Rule:
    unknown = sorted(str(key) for key in item if key not in RULE_KEYS)
    if unknown:
        raise AliasConfigError(f"{where} has unknown keys: {unknown}")
    for required in ("module", "as"):
        if required not in item:
            raise AliasConfigError(f"{where} needs a {required!r} key")
    texts: dict[str, str] = {}
    for key in ("module", "importer", "file", "reason"):
        raw = item.get(key)
        if raw is None:
            continue
        if not isinstance(raw, str):
            raise AliasConfigError(f"{where}.{key} must be a string")
        texts[key] = raw
    try:
        compile_module_pattern(texts["module"])
    except AliasConfigError as exc:
        raise AliasConfigError(f"{where}.module: {exc}") from exc
    for key in ("importer", "file"):
        pattern = texts.get(key)
        if pattern is None:
            continue
        try:
            _compile_regex(pattern)
        except re.error as exc:
            raise AliasConfigError(
                f"{where}.{key} is not a valid regex: {pattern!r} ({exc})"
            ) from exc
    return Rule(
        module=texts["module"],
        alias=_alias(item["as"], where),
        importer=texts.get("importer"),
        file=texts.get("file"),
        reason=texts.get("reason", ""),
        origin=f"alias rule #{position}",
    )


def _alias(value: object, where: str) -> str | None:
    if value is False:
        return None
    if not isinstance(value, str):
        raise AliasConfigError(f"{where}.as must be a string or false")
    try:
        check_template(value)
    except AliasConfigError as exc:
        raise AliasConfigError(f"{where}.as: {exc}") from exc
    return value


def ruff_rules(ruff: object) -> tuple[Rule, ...]:
    """Unconditional exact rules from ruff's ``flake8-import-conventions`` aliases.

    *ruff* is the ``[tool.ruff]`` table (anything else, or nothing, yields no
    rules). See the module docstring for which tables and keys are read.
    """
    if not isinstance(ruff, dict):
        return ()
    for path in RUFF_TABLES:
        table = _at(ruff, path)
        if table is None:
            continue
        where = "tool.ruff." + ".".join(path)
        if not isinstance(table, dict):
            raise AliasConfigError(f"{where} must be a table")
        return _ruff_table(table, where)
    return ()


def _at(table: dict[str, object], path: tuple[str, ...]) -> object:
    current: object = table
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def _ruff_table(table: dict[str, object], where: str) -> tuple[Rule, ...]:
    merged: dict[str, tuple[str, str]] = {}
    for key in RUFF_KEYS:
        entries = table.get(key)
        if entries is None:
            continue
        origin = f"{where}.{key}"
        if not isinstance(entries, dict):
            raise AliasConfigError(f"{origin} must be a table of module = alias")
        for module, alias in entries.items():
            name = str(module)
            if not all(part.isidentifier() for part in name.split(".")):
                raise AliasConfigError(f"{origin}: {name!r} is not a dotted module name")
            if not isinstance(alias, str) or not usable_name(alias):
                raise AliasConfigError(f"{origin}.{name!r} must be an identifier, got {alias!r}")
            merged[name] = (alias, origin)
    return tuple(
        Rule(module=module, alias=alias, reason=f"from ruff's {origin}", origin="ruff alias")
        for module, (alias, origin) in merged.items()
    )

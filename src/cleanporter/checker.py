"""Violation detection: visit ImportFrom statements and classify aliases."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import libcst as cst

from cleanporter.config import Config
from cleanporter.resolver import Classification, Origin, Resolver, SymbolKind

CP001 = "CP001"  # non-module from-import (violation)
CP002 = "CP002"  # unresolvable import target (warning)
CP003 = "CP003"  # fix skipped / informational in --fix mode


@dataclass(frozen=True)
class Finding:
    path: Path
    line: int
    col: int
    code: str
    message: str

    def format(self) -> str:
        return f"{self.path}:{self.line}:{self.col}: {self.code}: {self.message}"


@dataclass(frozen=True)
class FromImportContext:
    """One ``from P import S`` statement plus position/safety context."""

    node: cst.ImportFrom
    absolute_prefix: str | None  # None => could not anchor relative import
    gated_by_type_checking: bool
    in_one_liner_suite: bool
    is_module_top_level: bool


# --------------------------------------------------------------------- #
# CST helpers                                                            #
# --------------------------------------------------------------------- #


def dotted_name(node: object) -> str | None:
    """Stringify a CST attribute chain like ``pkg.sub``; None when exotic."""
    if isinstance(node, cst.Name):
        return node.value
    if isinstance(node, cst.Attribute):
        base = dotted_name(node.value)
        return f"{base}.{node.attr.value}" if base is not None else None
    return None


def containing_module_of(roots: list[Path], file: Path) -> str | None:
    """Dotted name of the module/package a file belongs to, under roots.

    Prefers the deepest matching root so nested trees anchor correctly.
    For ``__init__.py`` files the package name itself is returned.
    """
    file = file.resolve()
    best: tuple[int, str] | None = None
    for root in roots:
        try:
            rel = file.relative_to(root.resolve())
        except ValueError:
            continue
        parts = list(rel.with_suffix("").parts)
        if parts and parts[-1] == "__init__":
            parts.pop()
        candidate = ".".join(parts)
        if best is None or len(root.resolve().parts) > best[0]:
            best = (len(root.resolve().parts), candidate)
    return best[1] if best else None


def absolute_import_prefix(
    node: cst.ImportFrom,
    containing_module: str | None,
    *,
    containing_is_package: bool = False,
) -> str | None:
    """Absolute dotted prefix of a possibly-relative ImportFrom.

    Returns "" for bare ``from . import x`` anchored at a top-level package;
    None when the relative dots escape every known root.
    """
    level = len(node.relative) if node.relative is not None else 0
    own = dotted_name(node.module) if node.module is not None else ""
    if level == 0:
        return own or None
    if containing_module is None:
        return None
    parts = [p for p in containing_module.split(".") if p]
    if not containing_is_package:
        # A module's level-1 dots anchor at its *package*.
        parts = parts[:-1]
        if parts and level > 1:
            pass
    drop = level - 1
    if drop < 0 or drop > len(parts):
        return None
    base_parts = parts[: len(parts) - drop] if drop else parts
    base = ".".join(base_parts)
    if not own:
        return base
    return f"{base}.{own}" if base else own


# --------------------------------------------------------------------- #
# scanning                                                               #
# --------------------------------------------------------------------- #


def _is_type_checking_test(test: cst.BaseExpression) -> bool:
    return dotted_name(test) == "TYPE_CHECKING"


def _record(
    stmt: cst.ImportFrom,
    *,
    containing: str | None,
    gate: bool,
    in_suite: bool,
    top: bool,
    out: list[FromImportContext],
) -> None:
    out.append(
        FromImportContext(
            node=stmt,
            absolute_prefix="",  # resolved by caller once all contexts known
            gated_by_type_checking=gate,
            in_one_liner_suite=in_suite,
            is_module_top_level=top,
        )
    )


def _scan_stmt(
    stmt: cst.BaseStatement,
    *,
    gate: bool,
    top: bool,
    containing: str | None,
    out: list[FromImportContext],
) -> None:
    if isinstance(stmt, cst.ImportFrom):
        _record(stmt, containing=containing, gate=gate, in_suite=False, top=top, out=out)
        return
    if isinstance(stmt, cst.SimpleStatementLine):
        for small in stmt.body:
            if isinstance(small, cst.ImportFrom):
                _record(small, containing=containing, gate=gate, in_suite=False, top=top, out=out)
        return

    if isinstance(stmt, cst.FunctionDef) or isinstance(stmt, getattr(cst, "AsyncFunctionDef", ())):
        _scan_suite(stmt.body, gate=gate, top=False, containing=containing, out=out)
        return

    def scan_pair(first: cst.BaseSuite | None, second: cst.BaseSuite | None = None, second_gate: bool | None = None) -> None:
        if first is not None:
            g = gate
            if isinstance(stmt, cst.If) and first is stmt.body:
                g = gate or _is_type_checking_test(stmt.test)
            elif second_gate is not None:
                g = second_gate
            _scan_suite(first, gate=g, top=False, containing=containing, out=out)
        if second is not None:
            g = gate if second_gate is None else second_gate
            _scan_suite(second, gate=g, top=False, containing=containing, out=out)

    if isinstance(stmt, cst.ClassDef):
        scan_pair(stmt.body)
    elif isinstance(stmt, (cst.While, cst.For)):
        scan_pair(stmt.body, stmt.orelse)
    elif isinstance(stmt, cst.If):
        if isinstance(stmt.orelse, cst.Else):
            scan_pair(stmt.body, stmt.orelse.body)
        else:
            scan_pair(stmt.body, stmt.orelse)
            if isinstance(stmt.orelse, cst.If):
                _scan_stmt(stmt.orelse, gate=gate, top=False, containing=containing, out=out)
    elif isinstance(stmt, cst.With):
        scan_pair(stmt.body)
    elif isinstance(stmt, cst.Try):
        scan_pair(stmt.body, stmt.orelse)
        for handler in stmt.handlers:
            _scan_suite(handler.body, gate=gate, top=False, containing=containing, out=out)
        scan_pair(stmt.finalbody)
    elif hasattr(cst, "Match") and isinstance(stmt, cst.Match):
        for case in stmt.cases:
            inner = getattr(case, "body", None)
            if isinstance(inner, cst.BaseSuite):
                _scan_suite(inner, gate=gate, top=False, containing=containing, out=out)


def _scan_suite(
    suite: cst.BaseSuite,
    *,
    gate: bool,
    top: bool,
    containing: str | None,
    out: list[FromImportContext],
) -> None:
    one_liner = isinstance(suite, cst.SimpleStatementSuite)
    for stmt in suite.body:
        if isinstance(stmt, cst.ImportFrom):
            _record(stmt, containing=containing, gate=gate, in_suite=one_liner, top=top, out=out)
        elif isinstance(stmt, cst.BaseStatement):
            if one_liner:
                # Nested statements cannot occur syntactically in a suite;
                # guard regardless.
                continue
            _scan_stmt(stmt, gate=gate, top=False, containing=containing, out=out)


def scan_from_imports(
    module: cst.Module,
    containing_module: str | None,
    *,
    containing_is_package: bool = False,
) -> list[FromImportContext]:
    """Find every ImportFrom with safety/position context flags attached."""
    out: list[FromImportContext] = []
    for stmt in module.body:
        if isinstance(stmt, cst.ImportFrom):
            _record(stmt, containing=None, gate=False, in_suite=False, top=True, out=out)
        else:
            _scan_stmt(stmt, gate=False, top=True, containing=containing_module, out=out)
    return [
        dataclass_replace_ctx(
            entry,
            absolute_import_prefix(
                entry.node,
                containing_module,
                containing_is_package=containing_is_package,
            ),
        )
        for entry in out
    ]


def dataclass_replace_ctx(entry: FromImportContext, prefix: str | None) -> FromImportContext:
    from dataclasses import replace

    return replace(entry, absolute_prefix=prefix)


def alias_local_name(alias: cst.ImportAlias) -> str:
    base = dotted_name(alias.name)
    assert base is not None
    return alias.asname.name.value if alias.asname else base.split(".")[-1]


# --------------------------------------------------------------------- #
# checking                                                               #
# --------------------------------------------------------------------- #


def check_module(
    module: cst.Module,
    path: Path,
    resolver: Resolver,
    config: Config,
    project_roots: list[Path],
) -> tuple[list[Finding], list[FromImportContext]]:
    """Produce CP001/CP002 findings plus the raw scan for reuse by the fixer."""
    findings: list[Finding] = []
    is_package = path.name == "__init__.py"
    contexts = scan_from_imports(
        module,
        containing_module_of(project_roots, path),
        containing_is_package=is_package,
    )
    wrapper = cst.MetadataWrapper(module, unsafe_skip_copy=True)
    positions = wrapper.resolve(cst.metadata.PositionProvider)

    for entry in contexts:
        pos = positions.get(entry.node)
        line = pos.start.line if pos is not None else 0
        col = (pos.start.column + 1) if pos is not None else 0
        if entry.in_one_liner_suite or entry.absolute_prefix is None:
            continue  # reported as fix blockers elsewhere / CP002-style warns here
        if isinstance(entry.node.names, cst.ImportStar):
            continue  # wildcard import: cannot classify or fix individual aliases
        for alias in entry.node.names:
            if not isinstance(alias, cst.ImportAlias):
                continue
            symbol = dotted_name(alias.name)
            if symbol is None:
                continue
            classification = resolver.classify_absolute(entry.absolute_prefix, symbol)
            origin_ok = config.scope == "all" or classification.origin == Origin.FIRST_PARTY
            if classification.is_violation and origin_ok:
                local = alias_local_name(alias)
                note = f" ({classification.note})" if classification.note else ""
                msg = (
                    f"'{entry.absolute_prefix}' imports non-module '{symbol}' "
                    f"(bound locally as '{local}'); import the module instead{note}"
                )
                findings.append(Finding(path, line, col, CP001, msg))
            elif classification.kind is SymbolKind.UNRESOLVABLE and origin_ok:
                note = f": {classification.note}" if classification.note else ""
                msg = f"cannot prove '{symbol}' of '{entry.absolute_prefix}' is a module{note}"
                findings.append(Finding(path, line, col, CP002, msg))
    return findings, contexts

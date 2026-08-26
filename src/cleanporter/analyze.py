"""Analysis driver: turn source files into :class:`Finding` objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import libcst as cst
from libcst.metadata import MetadataWrapper, PositionProvider

from . import _imports
from .config import Config
from .discover import iter_python_files
from .firstparty import ModuleMap
from .model import Finding, Status
from .resolver import Resolver


@dataclass
class ImportUnit:
    node: cst.ImportFrom
    parent: str | None  # resolved absolute module, or None if unresolved
    name: str
    asname: str | None
    alias: cst.ImportAlias | None
    star: bool


@dataclass
class FileRecord:
    path: Path
    source: str
    tree: cst.Module
    base_pkg: str
    _units: list[ImportUnit] | None = field(default=None, repr=False, compare=False)
    _positions: object | None = field(default=None, repr=False, compare=False)

    @property
    def units(self) -> list[ImportUnit]:
        """Every ``from`` import in the file. Computed once."""
        if self._units is None:
            self._units = list(iter_units(self.tree, self.base_pkg))
        return self._units

    @property
    def positions(self):  # type: ignore[no-untyped-def]
        """``PositionProvider`` mapping for this tree. Resolved once."""
        if self._positions is None:
            self._positions = MetadataWrapper(
                self.tree, unsafe_skip_copy=True
            ).resolve(PositionProvider)
        return self._positions


def package_of(path: Path, module_map: ModuleMap) -> str:
    """Package containing ``path`` (``""`` for a top-level module)."""
    qn = module_map.qualname_for(path)
    if qn is None:
        return ""
    if path.name == "__init__.py":
        return qn
    return qn.rsplit(".", 1)[0] if "." in qn else ""


def iter_units(tree: cst.Module, base_pkg: str):
    """Yield an :class:`ImportUnit` per name in every ``from`` import."""
    for node in _walk_import_froms(tree):
        parent = _imports.resolve_parent(node, base_pkg)
        if _imports.is_star(node):
            yield ImportUnit(node, parent, "*", None, None, star=True)
            continue
        for name, asname, alias in _imports.imported_names(node):
            yield ImportUnit(node, parent, name, asname, alias, star=False)


def _walk_import_froms(tree: cst.Module) -> list[cst.ImportFrom]:
    found: list[cst.ImportFrom] = []

    class V(cst.CSTVisitor):
        def visit_ImportFrom(self, node: cst.ImportFrom) -> None:
            found.append(node)

    tree.visit(V())
    return found


def collect_pairs(records: list[FileRecord]) -> list[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for rec in records:
        for unit in rec.units:
            if unit.parent and not unit.star:
                pairs.add((unit.parent, unit.name))
    return sorted(pairs)


def analyze_record(rec: FileRecord, resolver: Resolver, config: Config) -> list[Finding]:
    positions = rec.positions
    findings: list[Finding] = []
    for unit in rec.units:
        pos = positions[unit.node].start
        line, col = pos.line, pos.column

        if unit.star:
            findings.append(
                Finding(rec.path, line, col, unit.parent or "?", "*",
                        Status.SKIPPED, "wildcard import cannot be rewritten to a module import")
            )
            continue
        if unit.parent is None:
            findings.append(
                Finding(rec.path, line, col, "?", unit.name, Status.UNRESOLVED,
                        "relative import could not be anchored to a package")
            )
            continue
        if config.is_exempt(unit.parent, unit.name):
            continue
        if config.scope == "first-party" and not resolver.is_first_party(unit.parent):
            continue

        verdict = resolver.is_module(unit.parent, unit.name)
        if verdict is True:
            continue  # importing a module -> compliant
        if verdict is None:
            findings.append(
                Finding(rec.path, line, col, unit.parent, unit.name,
                        Status.UNRESOLVED, resolver.reason(unit.parent, unit.name))
            )
            continue
        findings.append(
            Finding(rec.path, line, col, unit.parent, unit.name, Status.VIOLATION)
        )
    return findings


def build(
    paths: list[Path], config: Config
) -> tuple[list[FileRecord], Resolver, list[Finding], list[str]]:
    """Expand paths, parse files, build the resolver and warm its cache.

    Returns the parsed records, the resolver, any parse-error findings, and
    any warnings produced while expanding ``paths`` (e.g. missing paths).
    """
    files, warnings = iter_python_files(paths, config)
    roots = [config.root / r for r in config.source_roots]
    module_map = ModuleMap.from_paths(files + roots)
    resolver = Resolver(module_map, python=config.python)

    records: list[FileRecord] = []
    errors: list[Finding] = []
    for f in files:
        source = f.read_text(encoding="utf-8")
        try:
            tree = cst.parse_module(source)
        except cst.ParserSyntaxError as exc:  # pragma: no cover - defensive
            errors.append(Finding(f, exc.raw_line, exc.raw_column, "?", "?",
                                  Status.UNRESOLVED, f"parse error: {exc.message}"))
            continue
        records.append(FileRecord(f, source, tree, package_of(f, module_map)))

    resolver.warm(collect_pairs(records))
    return records, resolver, errors, warnings

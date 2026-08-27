"""Analysis driver: turn source files into :class:`Finding` objects."""

from __future__ import annotations

import dataclasses
import pathlib
from collections.abc import Iterator, Mapping

import libcst as cst
from libcst import metadata

from cleanporter import config, discover, firstparty, model
from cleanporter import resolver as resolver_lib

from . import _imports


@dataclasses.dataclass
class ImportUnit:
    node: cst.ImportFrom
    parent: str | None  # resolved absolute module, or None if unresolved
    name: str
    asname: str | None
    alias: cst.ImportAlias | None
    star: bool


@dataclasses.dataclass
class FileRecord:
    path: pathlib.Path
    source: str
    tree: cst.Module
    base_pkg: str
    _units: list[ImportUnit] | None = dataclasses.field(default=None, repr=False, compare=False)
    _positions: Mapping[cst.CSTNode, metadata.CodeRange] | None = dataclasses.field(
        default=None, repr=False, compare=False
    )

    @property
    def units(self) -> list[ImportUnit]:
        """Every ``from`` import in the file. Computed once."""
        if self._units is None:
            self._units = list(iter_units(self.tree, self.base_pkg))
        return self._units

    @property
    def positions(self) -> Mapping[cst.CSTNode, metadata.CodeRange]:
        """``PositionProvider`` mapping for this tree. Resolved once."""
        if self._positions is None:
            self._positions = metadata.MetadataWrapper(self.tree, unsafe_skip_copy=True).resolve(
                metadata.PositionProvider
            )
        return self._positions


def package_of(
    path: pathlib.Path, module_map: firstparty.ModuleMap, relative_level: int = 0
) -> str:
    """Package containing ``path`` (``""`` for a top-level module).

    ``relative_level`` is the deepest relative import in the file; it tells
    `ModuleMap.qualname_for` how deep this file must sit, which is the only
    evidence that separates a real import root from a PEP 420 namespace
    directory that merely looks like one.
    """
    qn = module_map.qualname_for(path, relative_level)
    if qn is None:
        return ""
    if path.name == "__init__.py":
        return qn
    return qn.rsplit(".", 1)[0] if "." in qn else ""


def iter_units(tree: cst.Module, base_pkg: str) -> Iterator[ImportUnit]:
    """Yield an :class:`ImportUnit` per name in every ``from`` import."""
    for node in _walk_import_froms(tree):
        parent = _imports.resolve_parent(node, base_pkg)
        if _imports.is_star(node):
            yield ImportUnit(node, parent, "*", None, None, star=True)
            continue
        for name, asname, alias in _imports.imported_names(node):
            yield ImportUnit(node, parent, name, asname, alias, star=False)


def absolute_import_heads(tree: cst.Module) -> set[str]:
    """Top-level names *tree* imports absolutely (``import a.b`` -> ``a``).

    Evidence for `ModuleMap.demote_roots`: whatever a file imports by an
    absolute name lives under an import root, so it is not a root itself.
    """
    heads: set[str] = set()

    class V(cst.CSTVisitor):
        def visit_ImportFrom(self, node: cst.ImportFrom) -> None:
            if _imports.relative_level(node) == 0:
                heads.add(_imports.dotted(node.module).split(".")[0])

        def visit_Import(self, node: cst.Import) -> None:
            for alias in node.names:
                heads.add(_imports.dotted(alias.name).split(".")[0])

    tree.visit(V())
    heads.discard("")
    return heads


def max_relative_level(tree: cst.Module) -> int:
    """Deepest ``from ... import`` dot count in *tree* (0 if none are relative)."""
    return max((_imports.relative_level(node) for node in _walk_import_froms(tree)), default=0)


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


def analyze_record(
    rec: FileRecord, resolver: resolver_lib.Resolver, config: config.Config
) -> list[model.Finding]:
    positions = rec.positions
    findings: list[model.Finding] = []
    for unit in rec.units:
        pos = positions[unit.node].start
        line, col = pos.line, pos.column

        if unit.star:
            findings.append(
                model.Finding(
                    rec.path,
                    line,
                    col,
                    unit.parent or "?",
                    "*",
                    model.Status.SKIPPED,
                    "wildcard import cannot be rewritten to a module import",
                )
            )
            continue
        if unit.parent is None:
            findings.append(
                model.Finding(
                    rec.path,
                    line,
                    col,
                    "?",
                    unit.name,
                    model.Status.UNRESOLVED,
                    "relative import could not be anchored to a package",
                )
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
                model.Finding(
                    rec.path,
                    line,
                    col,
                    unit.parent,
                    unit.name,
                    model.Status.UNRESOLVED,
                    resolver.reason(unit.parent, unit.name),
                )
            )
            continue
        findings.append(
            model.Finding(rec.path, line, col, unit.parent, unit.name, model.Status.VIOLATION)
        )
    return findings


def build(
    paths: list[pathlib.Path], config: config.Config
) -> tuple[list[FileRecord], resolver_lib.Resolver, list[model.Finding], list[str]]:
    """Expand paths, parse files, build the resolver and warm its cache.

    Returns the parsed records, the resolver, any parse-error findings, and
    any warnings produced while expanding ``paths`` (e.g. missing paths).
    """
    files, warnings = discover.iter_python_files(paths, config)
    roots = tuple(config.root / r for r in config.source_roots)
    module_map = firstparty.ModuleMap.from_paths(files, declared=roots)
    warnings.extend(module_map.warnings)
    resolver = resolver_lib.Resolver(module_map, python=config.python)

    parsed: list[tuple[pathlib.Path, str, cst.Module]] = []
    errors: list[model.Finding] = []
    evidence: dict[str, list[pathlib.Path]] = {}
    for f in files:
        source = f.read_text(encoding="utf-8")
        try:
            tree = cst.parse_module(source)
        except cst.ParserSyntaxError as exc:  # pragma: no cover - defensive
            errors.append(
                model.Finding(
                    f,
                    exc.raw_line,
                    exc.raw_column,
                    "?",
                    "?",
                    model.Status.UNRESOLVED,
                    f"parse error: {exc.message}",
                )
            )
            continue
        parsed.append((f, source, tree))
        for head in absolute_import_heads(tree):
            evidence.setdefault(head, []).append(f)

    # Every file's absolute imports say which directories are packages, so
    # settle the root set before anchoring anyone's relative imports.
    module_map.demote_roots(evidence)
    records = [
        FileRecord(f, source, tree, package_of(f, module_map, max_relative_level(tree)))
        for f, source, tree in parsed
    ]

    resolver.warm(collect_pairs(records))
    return records, resolver, errors, warnings

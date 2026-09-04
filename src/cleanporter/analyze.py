"""Analysis driver: turn source files into :class:`Finding` objects.

`FileRecord` carries a parsed file and lazily caches what is expensive to
derive from it -- the import units and libcst's position metadata -- so a
record survives being walked more than once (the CLI re-parses into a fresh
record after a fix and analyses it again).
"""

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
    #: This file's own dotted module name (``""`` when it is not under a
    #: known import root). Needed to ask whether *this* module's imports are
    #: load-bearing re-exports for other files in the run.
    qualname: str = ""
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
    """``(module, name)`` pairs to classify: every name imported *from* a module."""
    pairs: set[tuple[str, str]] = set()
    for rec in records:
        for unit in rec.units:
            if unit.parent and not unit.star:
                pairs.add((unit.parent, unit.name))
    return sorted(pairs)


def module_bindings(tree: cst.Module, base_pkg: str) -> dict[str, str]:
    """Local name -> dotted module it is bound to, for every import in *tree*.

    ``import a.b`` binds ``a``; ``import a.b as ab`` binds ``ab`` to ``a.b``;
    ``from p import m`` binds ``m`` to ``p.m``. Whether ``p.m`` is really a
    module is not checked here -- recording it regardless only ever makes
    `attribute_pairs` see *more* uses, and this evidence is used to decline
    rewrites, so over-collecting is the safe direction.
    """
    bound: dict[str, str] = {}

    class V(cst.CSTVisitor):
        def visit_Import(self, node: cst.Import) -> None:
            for alias in node.names:
                dotted = _imports.dotted(alias.name)
                if alias.asname is not None and isinstance(alias.asname.name, cst.Name):
                    bound[alias.asname.name.value] = dotted
                else:
                    # ``import a.b`` binds only ``a``, which names ``a``.
                    head = dotted.split(".")[0]
                    bound[head] = head

        def visit_ImportFrom(self, node: cst.ImportFrom) -> None:
            parent = _imports.resolve_parent(node, base_pkg)
            if parent is None:
                return
            for name, asname, _alias in _imports.imported_names(node):
                bound[asname or name] = f"{parent}.{name}"

    tree.visit(V())
    return bound


def attribute_pairs(tree: cst.Module, base_pkg: str) -> set[tuple[str, str]]:
    """``(module, attribute)`` pairs *tree* reads through a module binding.

    ``import pkg.tool`` followed by ``pkg.tool.dump`` is a use of
    ``pkg.tool.dump`` every bit as much as ``from pkg.tool import dump`` is,
    and it is the shape this tool rewrites everything *into* -- so a fixer
    that only looked at ``from`` imports for evidence was blind to its own
    output, and a second ``--fix`` run would happily delete the attribute the
    first run had just protected.
    """
    bound = module_bindings(tree, base_pkg)
    found: set[tuple[str, str]] = set()

    class V(cst.CSTVisitor):
        def visit_Attribute(self, node: cst.Attribute) -> None:
            try:
                prefix = _imports.dotted(node.value)
            except TypeError:
                return  # a call, a subscript, ... -- not a dotted module path
            head, _dot, rest = prefix.partition(".")
            target = bound.get(head)
            if target is None:
                return
            module = f"{target}.{rest}" if rest else target
            found.add((module, node.attr.value))

    tree.visit(V())
    return found


def star_imported_modules(tree: cst.Module, base_pkg: str) -> set[str]:
    """Modules *tree* does ``from M import *`` on.

    A star import takes every public name, so any of *M*'s re-exports could be
    the one it needs. There is no way to narrow it, so all of them count.
    """
    found: set[str] = set()
    for node in _walk_import_froms(tree):
        if _imports.is_star(node):
            parent = _imports.resolve_parent(node, base_pkg)
            if parent is not None:
                found.add(parent)
    return found


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
        if _imports.is_explicit_reexport(unit.name, unit.asname):
            findings.append(
                model.Finding(
                    rec.path,
                    line,
                    col,
                    unit.parent,
                    unit.name,
                    model.Status.SKIPPED,
                    "explicit re-export ('as' aliasing the name to itself); "
                    "rewriting it would remove a public name",
                )
            )
            continue
        bound = unit.asname or unit.name
        if rec.qualname and resolver.is_load_bearing(rec.qualname, bound):
            findings.append(
                model.Finding(
                    rec.path,
                    line,
                    col,
                    unit.parent,
                    unit.name,
                    model.Status.SKIPPED,
                    f"another file imports '{bound}' from '{rec.qualname}'; "
                    "rewriting this import would remove that attribute",
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
        FileRecord(
            f,
            source,
            tree,
            package_of(f, module_map, max_relative_level(tree)),
            module_map.qualname_for(f, max_relative_level(tree)) or "",
        )
        for f, source, tree in parsed
    ]

    pairs = collect_pairs(records)
    # Every *use* of ``M.N`` in the run is evidence that M must keep binding
    # N, which constrains what M's own imports may be rewritten to. A use is
    # any of: ``from M import N``, ``M.N`` through a module binding, or
    # ``from M import *`` (which could need any of them). See
    # `Resolver.is_load_bearing`.
    uses: set[tuple[str, str]] = set(pairs)
    star: set[str] = set()
    for rec in records:
        uses |= attribute_pairs(rec.tree, rec.base_pkg)
        star |= star_imported_modules(rec.tree, rec.base_pkg)
    resolver.note_uses(uses, star)
    resolver.warm(pairs)
    return records, resolver, errors, warnings

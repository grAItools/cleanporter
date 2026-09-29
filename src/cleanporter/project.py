"""The run's inputs, built once and in order: files, records, map, evidence, resolver.

A `Project` is everything a check or fix of a set of paths needs to know
before it looks at any one import, and `build` is the supported way to get
one. Constructing a `Project` by hand bypasses the staging below, and with it
the guarantee that its resolver was built with its records' evidence. The
stages are fixed because each needs the one before it:

1. **Files.** The paths are expanded into Python files (`discover`), and the
   first-party `firstparty.ModuleMap` is built from them.
2. **Records.** Every file is read, decoded and parsed; a file that cannot be
   is a file-level error and the run carries on without it. Each tree is
   walked exactly once, for its `analyze.FileFacts`.
3. **Module map settled.** Every file's absolute imports say which
   directories are packages, so the map demotes the roots they disprove --
   and only then is each file's own package and module name known, which is
   what anchors its relative imports (`analyze.FileRecord`).
4. **Use evidence.** What every file reads -- ``from M import N``, ``M.N``
   through an import, ``from M import *`` -- and every string that names a
   first-party ``M.N`` by its dotted path, in a file or among the entry
   points of the ``pyproject.toml`` in use (`_named`), is collected into a
   `resolver.Evidence`. It needs every record, because it is cross-file.
5. **Resolver ready.** The resolver is constructed *with* that evidence, and
   with the interpreter `_interpreter.choose` makes of the ``python`` setting
   -- the project's own, detected, unless one is named -- then warmed with
   every pair the run will ask about, in one probe batch.

Stage 4 before stage 5 is the point of the module. The resolver used to be
built early and told about the evidence afterwards, so a resolver held by
anything other than the run loop answered `resolver.Resolver.is_load_bearing`
from no evidence at all, silently. Now the evidence is a required
constructor argument, and `build` supplies it from every record.

`Project` is frozen: the records, errors and warnings are tuples. The
resolver inside still caches answers -- and collects warnings from lookups
the warm-up did not foresee, which `engine.run` reports at the end -- but
what `is_load_bearing` answers no longer depends on who asks first. (Probe
verdicts still can depend on the order pairs are asked in, which is why
`build` warms them in one batch; see `resolver.Resolver.warm`.)
"""

from __future__ import annotations

import dataclasses
import pathlib
import tomllib
from collections.abc import Callable, Mapping

import libcst as cst

from cleanporter import analyze, discover, firstparty, guards, model
from cleanporter import config as config_lib
from cleanporter import resolver as resolver_lib

from . import _interpreter, _source


@dataclasses.dataclass(frozen=True)
class Project:
    """A set of paths, parsed and ready to check or fix. Made by `build`."""

    config: config_lib.Config
    #: One per file that was read and parsed, in discovery order.
    records: tuple[analyze.FileRecord, ...]
    #: The run's resolver, built with the run's `resolver.Evidence` and warmed.
    resolver: resolver_lib.Resolver
    #: A `model.Status.UNRESOLVED` finding per file that could not be read,
    #: decoded or parsed. Every other file is in `records`.
    errors: tuple[model.Finding, ...]
    #: Warnings from expanding the paths (a missing path), building the module
    #: map (roots that nest), and the warm-up probe (a batch that failed).
    warnings: tuple[str, ...]
    #: Notes: which interpreter detection picked for the probe, and why.
    notes: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True)
class _Parsed:
    path: pathlib.Path
    decoded: _source.Decoded
    tree: cst.Module
    facts: analyze.FileFacts


def build(paths: list[pathlib.Path], config: config_lib.Config) -> Project:
    """Expand *paths* and take them through every stage in the module docstring."""
    files, warnings = discover.iter_python_files(paths, config)
    roots = tuple(config.root / r for r in config.source_roots)
    module_map = firstparty.ModuleMap.from_paths(files, declared=roots)
    warnings.extend(module_map.warnings)

    parsed, errors = _parse(files)
    records = _records(parsed, module_map, config)
    pairs = analyze.collect_pairs(records)
    interpreter = _interpreter.choose(config.python, config.root)
    named = _named(records, module_map, config, warnings)
    resolver = resolver_lib.Resolver(
        module_map, python=interpreter.python, evidence=_evidence(records, pairs, named)
    )
    # Out-of-scope pairs are never asked about, so classifying them -- an
    # import of a third-party package, in the probe -- would be wasted. A
    # replacement pair shares its import's top-level package, so it is in
    # scope exactly when that import is; a first-party re-export of a
    # third-party name still reaches the probe, because `Resolver.warm` adds
    # its origin to the batch itself. The *use* evidence is left whole: it is
    # only ever consulted about a first-party module.
    batch = pairs + analyze.replacement_pairs(pairs)
    resolver.warm([pair for pair in batch if analyze.in_scope(pair[0], resolver, config)])
    warnings.extend(resolver.take_warnings())
    notes = () if interpreter.note is None else (interpreter.note,)
    return Project(config, tuple(records), resolver, tuple(errors), tuple(warnings), notes)


def _parse(files: list[pathlib.Path]) -> tuple[list[_Parsed], list[model.Finding]]:
    """Read, decode and parse every file, walking each tree once for its facts."""
    parsed: list[_Parsed] = []
    errors: list[model.Finding] = []
    for f in files:
        try:
            decoded = _source.read(f)
        except _source.SourceError as exc:
            # Reported like a parse error -- a file that was not checked, so
            # exit 2 -- and the run carries on with every other file.
            errors.append(
                model.Finding(f, exc.line, 0, "?", "?", model.Status.UNRESOLVED, str(exc))
            )
            continue
        try:
            tree = cst.parse_module(decoded.text)
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
        # The one walk of this tree: everything after this reads these facts.
        parsed.append(_Parsed(f, decoded, tree, analyze.collect_facts(tree)))
    return parsed, errors


def _records(
    parsed: list[_Parsed], module_map: firstparty.ModuleMap, config: config_lib.Config
) -> list[analyze.FileRecord]:
    """Settle the module map's roots, then anchor every file against them."""
    evidence: dict[str, list[pathlib.Path]] = {}
    for p in parsed:
        for head in p.facts.absolute_import_heads():
            evidence.setdefault(head, []).append(p.path)
    # Every file's absolute imports say which directories are packages, so
    # settle the root set before anchoring anyone's relative imports.
    module_map.demote_roots(evidence)
    records: list[analyze.FileRecord] = []
    for p in parsed:
        level = p.facts.max_relative_level()
        records.append(
            analyze.FileRecord(
                p.path,
                p.decoded.text,
                p.tree,
                analyze.package_of(p.path, module_map, level),
                module_map.qualname_for(p.path, level) or "",
                root=config.root,
                skip_rules=config.skip,
                encoding=p.decoded.encoding,
                raw=p.decoded.raw,
                _facts=p.facts,
            )
        )
    return records


def _evidence(
    records: list[analyze.FileRecord],
    pairs: list[tuple[str, str]],
    named: Mapping[tuple[str, str], tuple[resolver_lib.StringReference, ...]],
) -> resolver_lib.Evidence:
    """Every *use* of ``M.N`` in the run, which says M must keep binding N.

    A use is any of: ``from M import N`` (*pairs*), ``M.N`` through a module
    binding, or ``from M import *`` (which could need any of them). See
    `resolver.Resolver.is_load_bearing`.
    """
    uses: set[tuple[str, str]] = set(pairs)
    star: set[str] = set()
    for rec in records:
        uses |= rec.facts.attribute_pairs(rec.base_pkg)
        star |= rec.facts.star_imported_modules(rec.base_pkg)
    return resolver_lib.Evidence(frozenset(uses), frozenset(star), named)


def _named(
    records: list[analyze.FileRecord],
    module_map: firstparty.ModuleMap,
    config: config_lib.Config,
    warnings: list[str],
) -> dict[tuple[str, str], tuple[resolver_lib.StringReference, ...]]:
    """Every string in the run that names a first-party ``module.name`` by path.

    The strings are the ones `analyze.FileFacts` already kept off each tree's
    single walk -- a literal whose whole text is ``a.b.c`` or ``a.b:c`` --
    plus the entry points of the ``pyproject.toml`` in use
    (`_entry_points`). Each yields every split `guards.cross_file_pairs`
    offers, kept when its first component is first-party: nothing else can
    be something this run rewrites. Whether the pair is a *re-export* the
    rewrite would remove is `resolver.Resolver.named_by`'s question, asked
    only of the modules the fixer actually touches.
    """
    first_party: dict[str, bool] = {}
    named: dict[tuple[str, str], list[resolver_lib.StringReference]] = {}

    def wanted(parts: tuple[str, ...]) -> bool:
        head = parts[0]
        if head not in first_party:
            first_party[head] = module_map.is_first_party(head)
        return first_party[head]

    def add(parts: tuple[str, ...], ref: resolver_lib.StringReference) -> None:
        for pair in guards.cross_file_pairs(parts):
            named.setdefault(pair, []).append(ref)

    for rec in records:
        shown: pathlib.Path | None = None
        for node, parts in rec.facts.path_strings:
            if not wanted(parts):
                continue  # ``"os.path.join"``, ``"setup.py"``: not this run's to rewrite
            if shown is None:
                shown = _shown(rec.path, config.root)
            ref = resolver_lib.StringReference(
                node.raw_value, shown, rec.path.resolve(), _line_of(rec, node)
            )
            add(parts, ref)
    for parts, ref in _entry_points(config, warnings):
        if wanted(parts):
            add(parts, ref)
    return {pair: tuple(refs) for pair, refs in named.items()}


def _line_of(rec: analyze.FileRecord, node: cst.CSTNode) -> Callable[[], int]:
    """The line of *node* in *rec*, resolved only if someone asks."""
    return lambda: rec.positions[node].start.line


#: The ``[project]`` tables whose values are ``module:attribute`` entry points.
_ENTRY_POINT_TABLES = ("scripts", "gui-scripts")


def _entry_points(
    config: config_lib.Config, warnings: list[str]
) -> list[tuple[tuple[str, ...], resolver_lib.StringReference]]:
    """Every entry point the ``pyproject.toml`` in use declares, as a reference.

    ``[project.scripts]``, ``[project.gui-scripts]`` and every group under
    ``[project.entry-points]``. The value is ``module:attribute`` with
    optional whitespace around the colon and an optional ``[extras]`` suffix,
    both dropped. TOML keeps no positions, so the line is the first one of
    the file that holds the value as written -- or the first line, when the
    value is spelled some other way (a multi-line or escaped string).
    """
    pyproject = config.root / "pyproject.toml"
    try:
        text = pyproject.read_text(encoding="utf-8")
        data = tomllib.loads(text)
    except FileNotFoundError:
        return []
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        warnings.append(f"{pyproject}: cannot read its entry points ({exc}); they are not evidence")
        return []
    project = data.get("project")
    if not isinstance(project, dict):
        return []
    tables = [project.get(key) for key in _ENTRY_POINT_TABLES]
    groups = project.get("entry-points")
    if isinstance(groups, dict):
        tables.extend(groups.values())
    lines = text.splitlines()
    shown = _shown(pyproject, config.root)
    found: list[tuple[tuple[str, ...], resolver_lib.StringReference]] = []
    for table in tables:
        if not isinstance(table, dict):
            continue
        for value in table.values():
            if not isinstance(value, str):
                continue
            spec = "".join(value.partition("[")[0].split())
            parts = guards.dotted_reference(spec)
            if parts is None:
                continue
            line = next((i for i, row in enumerate(lines, 1) if value in row), 1)
            ref = resolver_lib.StringReference(spec, shown, pyproject.resolve(), _constant(line))
            found.append((parts, ref))
    return found


def _constant(line: int) -> Callable[[], int]:
    return lambda: line


def _shown(path: pathlib.Path, root: pathlib.Path) -> pathlib.Path:
    """*path* as a reason names it: relative to the project root, else as the run spells it.

    The reason is read next to a finding about a *different* file, so it
    names the referencing file the way the project does (``tests/test_x.py``,
    ``pyproject.toml``) whatever the paths on the command line looked like.
    """
    try:
        return path.resolve().relative_to(root.resolve())
    except ValueError:
        return path

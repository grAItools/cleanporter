"""Conservative auto-fixer for CP001 violations.

Strategy per file (all-or-nothing: a file is either fully rewritten or left
untouched):

  1. Analyze the parsed tree with the shared checker scanner plus resolver.
     Either produces a complete ``FixPlan`` or collects blocking reasons.
  2. Execute in one transformer pass driven by id() of *original* nodes
     (hooks receive ``original_node``, keeping identity maps exact):
        - preferred rewrite: ``from <parent> import <leaf>`` plus qualified
          ``<leaf>.<Symbol>`` references (style-compliant because *leaf*
          names a module);
        - fallback rewrite: ``import <full.module.path>`` plus
          ``<path>.<Symbol>`` references -- used when the preferred form
          itself would import a non-module (e.g. objects living in a package
          ``__init__``);
        - the enclosing physical line gains the prepended import(s),
          merged across aliases needing the same target.
  3. Verify the rewritten source parses; otherwise revert and report.
"""

from __future__ import annotations

import ast as pyast
import difflib
import re
from dataclasses import dataclass
from pathlib import Path

import libcst as cst

from cleanporter.checker import (
    CP003,
    Finding,
    dotted_name,
    scan_from_imports,
    containing_module_of,
    alias_local_name,
)
from cleanporter.config import Config
from cleanporter.resolver import Origin, Resolver, SymbolKind


class FixAborted(Exception):
    """Raised mid-transform when a newly discovered hazard forbids fixing."""


@dataclass(frozen=True)
class BindingNeed:
    """An import requirement produced by fixing one violating alias."""

    mode: str  # "ref" -> from parent import leaf ; "plain" -> import module
    module: str  # "ref": parent package ; "plain": full module path
    leaf: str  # imported name for "ref"; "" otherwise

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.mode, self.module, self.leaf)

    @property
    def binding_name(self) -> str:
        """Local name this import binds."""
        return self.leaf if self.mode == "ref" else self.module.split(".")[0]

    @property
    def qualifier(self) -> str:
        """Name used to attribute-access the fixed symbol."""
        return self.binding_name


@dataclass
class _StmtPlan:
    ctx_owner_id: int
    remove_symbols: set[str]
    needs: list[BindingNeed]


@dataclass
class FixOutcome:
    status: str  # "fixed" | "clean" | "skipped" | "error"
    new_source: str | None
    findings: list[Finding]
    skips: list[Finding]
    diff: str | None


def _iter_scopes_unique(mapping: dict[cst.CSTNode, cst.metadata.Scope]) -> list[cst.metadata.Scope]:
    seen: dict[int, cst.metadata.Scope] = {}
    for scope in mapping.values():
        seen[id(scope)] = scope
    return list(seen.values())


def _binding_for(
    alias: cst.ImportAlias,
    stmt: cst.ImportFrom,
    scopes: list[cst.metadata.Scope],
    local: str,
):
    """Locate the Assignment created by *alias*.

    Returns (status, binding) where status is:
      "ok"        exactly one candidate found
      "ambiguous" multiple candidates inside one statement (duplicate locals)
      "absent"    no matching assignment (import never referenced)
    """
    ours: dict[int, object] = {}
    for scope in scopes:
        try:
            named = list(scope[local])
        except KeyError:
            continue
        for a in named:
            if (
                type(a).__name__ == "ImportAssignment"
                and getattr(a, "node", None) is stmt
            ):
                ours[id(a)] = a
    if len(ours) > 1:
        return "ambiguous", None
    if not ours:
        return "absent", None
    return "ok", next(iter(ours.values()))


def _co_line_rebind_conflict(line_orig: cst.BaseStatement, needs: list[BindingNeed]) -> bool:
    """True when another small statement on this physical line assigns a
    name colliding with any binding our inserted imports introduce."""
    introduced = {n.binding_name for n in needs}
    if not isinstance(line_orig, cst.SimpleStatementLine):
        return False
    for small in line_orig.body:
        if isinstance(small, cst.ImportFrom):
            continue
        for target_name in _assign_target_names(small):
            if target_name in introduced:
                return True
        tgt = getattr(small, "target", None)
        if isinstance(tgt, cst.Name) and tgt.value in introduced:
            return True
    return False


def _assign_target_names(small: cst.BaseSmallStatement) -> list[str]:
    out: list[str] = []
    for target in getattr(small, "targets", []) or []:
        inner = getattr(target, "target", target)
        if isinstance(inner, cst.Name):
            out.append(inner.value)
        elif isinstance(inner, cst.Tuple):
            for el in inner.elements:
                leaf = getattr(el, "value", el)
                if isinstance(leaf, cst.Name):
                    out.append(leaf.value)
    return out


class _FixAnalyzer:
    def __init__(
        self,
        module: cst.Module,
        *,
        path: Path,
        resolver: Resolver,
        config: Config,
        project_roots: list[Path],
        scopes_map: dict[cst.CSTNode, cst.metadata.Scope],
        positions_map: dict[cst.CSTNode, cst.metadata.CodePosition],
    ) -> None:
        self.module = module
        self.path = path
        self.resolver = resolver
        self.config = config
        self.project_roots = project_roots
        self.scopes = _iter_scopes_unique(scopes_map)
        self.positions = positions_map

        self.plans: dict[int, _StmtPlan] = {}
        self.renames: dict[int, tuple[str, str]] = {}  # id(Name) -> (qualifier, symbol)
        self.blockers: list[tuple[int, str]] = []

        # Raw top-level import inventory (node_id, is_from, module_str,
        # bound_name) consumed after plans exist so self-edits are excluded.
        self._toplevel_inventory: list[tuple[int, bool, str, str]] = []
        self.existing_from_pairs: set[tuple[str, str]] = set()  # (parent, leaf)
        self.future_annotations = False
        # id(orig SimpleString) -> (full original token incl. quotes, rewritten token)
        self.str_renames: dict[int, tuple[str, str]] = {}

    # -------------------------------------------------------------- scans --

    def _scan_module_level(self) -> None:
        for stmt in self.module.body:
            if not isinstance(stmt, cst.SimpleStatementLine):
                continue
            for small in stmt.body:
                if isinstance(small, cst.Import):
                    for alias in small.names:
                        mod = dotted_name(alias.name)
                        bound = alias.asname.name.value if alias.asname else (mod or "").split(".")[0]
                        if mod:
                            self._toplevel_inventory.append((id(small), False, mod, bound, alias.asname is not None))
                elif isinstance(small, cst.ImportFrom) and (
                    small.relative is None or len(small.relative) == 0
                ):
                    parent = dotted_name(small.module) or ""
                    if isinstance(small.names, cst.ImportStar):
                        continue
                    for alias in small.names:
                        if not isinstance(alias, cst.ImportAlias):
                            continue
                        name_str = dotted_name(alias.name)
                        bound = alias.asname.name.value if alias.asname else (name_str or "").split(".")[-1]
                        self._toplevel_inventory.append(
                            (
                                id(small),
                                True,
                                f"{parent}.{name_str}" if name_str else parent,
                                bound,
                                alias.asname is not None,
                            )
                        )
                    if parent == "__future__":
                        for alias in small.names:
                            if (
                                isinstance(alias, cst.ImportAlias)
                                and dotted_name(alias.name) == "annotations"
                            ):
                                self.future_annotations = True

    # ------------------------------------------------------- classification --

    def _classify_alias(self, prefix: str | None, symbol_full: str):
        return self.resolver.classify_absolute(prefix, symbol_full)

    def _resolve_need(self, prefix: str) -> tuple[BindingNeed, str]:
        """Pick the import shape for replacing uses of ``from <prefix> import S``.

        Preferred: ``from <parent> import <leaf>`` with ``<leaf>.S`` uses --
        legal because *leaf* is a module. Falls back to ``import <prefix>``
        with ``<prefix-rooted>.S`` when the leaf itself is not a module
        (objects defined in a package __init__) or there is no parent.
        """
        parent, _sep, leaf = prefix.rpartition(".")
        if parent:
            cls_leaf = self.resolver.classify_absolute(parent, leaf)
            if cls_leaf.kind is SymbolKind.MODULE:
                need = BindingNeed("ref", parent, leaf)
                return need, need.qualifier
        need = BindingNeed("plain", prefix, "")
        return need, need.qualifier

    @staticmethod
    def _entry_masks_plan(
        entry: tuple[int, bool, str, str, bool],
        edited: dict[int, set[str]],
    ) -> bool:
        """True when an inventory binding dies as a side effect of editing."""
        node_id, is_from, _target, bound, aliased = entry
        if not is_from or node_id not in edited:
            return False
        return not aliased and bound in edited[node_id]

    def _refresh_binding_tables(self) -> None:
        """Recompute usable (parent, leaf) suppression pairs and taken names."""
        edited = {nid: plan.remove_symbols for nid, plan in self.plans.items()}
        alive = [e for e in self._toplevel_inventory if not self._entry_masks_plan(e, edited)]
        self.existing_from_pairs = {
            (target.rsplit(".", 1)[0], bound)
            for nid, is_from, target, bound, _aliased in alive
            if is_from
        }
        self.taken_names: dict[str, tuple[str, str, str]] = {
            bound: ("from" if is_from else "import", target, bound)
            for _nid, is_from, target, bound, _aliased in alive
        }

    def _qualifier_conflicts(self, qualifier: str, need: BindingNeed) -> bool:
        """True when reusing/inserting *qualifier* would be ambiguous."""
        holder = self.taken_names.get(qualifier)
        if holder is None:
            return False
        kind, target, bound = holder
        if need.mode == "ref":
            same_target = kind == "from" and target == f"{need.module}.{need.leaf}"
        else:
            same_target = kind == "import" and target == need.module
        return not same_target

    def analyze(self) -> None:
        self._scan_module_level()
        containing = containing_module_of(self.project_roots, self.path)
        contexts = scan_from_imports(
            self.module,
            containing,
            containing_is_package=self.path.name == "__init__.py",
        )

        pending_refs: list[tuple[cst.ImportFrom, cst.ImportAlias, str, str, str]] = []  # stmt, alias, prefix, symbol, qualifier
        candidate_locals: set[str] = set()

        for entry in contexts:
            pos = self.positions.get(entry.node)
            line = pos.start.line if pos is not None else 0
            if entry.absolute_prefix is None:
                continue  # unanchorable relative import -> checker already warned

            violation_aliases: list[tuple[cst.ImportAlias, str]] = []
            if isinstance(entry.node.names, cst.ImportStar):
                continue
            for alias in entry.node.names:
                if not isinstance(alias, cst.ImportAlias):
                    continue
                symbol_full = dotted_name(alias.name)
                if symbol_full is None:
                    continue
                cls = self._classify_alias(entry.absolute_prefix, symbol_full)
                origin_ok = (
                    self.config.scope == "all" or cls.origin is Origin.FIRST_PARTY
                )
                if not (cls.is_violation and origin_ok):
                    continue
                if cls.origin is not Origin.FIRST_PARTY and not self.config.autofix_third_party:
                    continue
                violation_aliases.append((alias, symbol_full))

            if not violation_aliases:
                continue

            if entry.gated_by_type_checking and not self.future_annotations:
                self.blockers.append(
                    (
                        line,
                        "TYPE_CHECKING-gated import; rewriting without "
                        "`from __future__ import annotations` risks NameError",
                    )
                )
                continue
            if entry.in_one_liner_suite:
                self.blockers.append(
                    (line, "violation inside a one-liner suite; unsupported layout")
                )
                continue

            plan = _StmtPlan(ctx_owner_id=id(entry.node), remove_symbols=set(), needs=[])
            for alias, symbol_full in violation_aliases:
                local = alias_local_name(alias)
                prefix = entry.absolute_prefix
                assert prefix is not None
                need, qualifier = self._resolve_need(prefix)
                if need.key not in [n.key for n in plan.needs]:
                    plan.needs.append(need)
                plan.remove_symbols.add(symbol_full)
                pending_refs.append((entry.node, alias, prefix, symbol_full, qualifier))
                candidate_locals.add(local)
            self.plans[id(entry.node)] = plan

        if not self.plans:
            return

        self._refresh_binding_tables()
        for nid, plan in self.plans.items():
            for need in plan.needs:
                if self._qualifier_conflicts(need.qualifier, need):
                    self.blockers.append(
                        (
                            0,
                            f"qualified name '{need.qualifier}' is already bound "
                            "to a different import at module level",
                        )
                    )

        # ---- safe-reference collection via scope metadata ----
        for stmt_node, alias, prefix, symbol_full, qualifier in pending_refs:
            local = alias_local_name(alias)
            status, binding = _binding_for(alias, stmt_node, self.scopes, local)
            if status == "ambiguous":
                self.blockers.append((0, f"ambiguous rebinding of local '{local}'"))
                continue
            if status == "absent":
                continue  # nothing referenced the import; alias removal only

            # Module-level rebinding makes access attribution ambiguous:
            # libcst is not flow-sensitive, so *all* accesses may list both
            # the import and the later assignment as referents. Block.
            our_scope = getattr(binding, "scope", None)
            if our_scope is not None:
                sibling_assignments = {
                    id(a)
                    for a in our_scope[local]
                    if a is not binding
                    and type(a).__name__ != "BuiltinAssignment"
                }
                if sibling_assignments:
                    self.blockers.append(
                        (0, f"local '{local}' is rebound at module level")
                    )
                    continue

            symbol_leaf = symbol_full.split(".")[-1]
            for scope in self.scopes:
                for access in scope.accesses:
                    refs = getattr(access, "referents", ())
                    if binding in set(refs):
                        self.renames[id(access.node)] = (qualifier, symbol_leaf)

        # ---- conservative whole-file guards ----
        for ln, msg in sorted(set(self._global_nonlocal_hits(candidate_locals))):
            self.blockers.append((ln, msg))
        annotation_string_ids = self._plan_annotation_string_renames(candidate_locals)
        for ln, msg in sorted(set(self._string_mentions(candidate_locals, skip_ids=annotation_string_ids))):
            self.blockers.append((ln, msg))

    # ------------------------------------------------- annotation strings --

    def _annotation_context_strings(self) -> dict[int, cst.SimpleString]:
        """SimpleStrings appearing inside genuine annotation slots.

        Under ``from __future__ import annotations`` these are never
        evaluated at runtime, so textual renames inside them are safe.
        """
        hits: dict[int, cst.SimpleString] = {}

        def absorb(node: object) -> None:
            if isinstance(node, cst.SimpleString):
                hits[id(node)] = node

        def walk_annot(annot: cst.BaseExpression | None) -> None:
            if annot is None:
                return
            stack = [annot]
            while stack:
                current = stack.pop()
                absorb(current)
                stack.extend(current.children)

        containers: list[cst.BaseExpression | None] = []

        class _W(cst.CSTVisitor):
            METADATA_DEPENDENCIES = ()

            def on_visit(wself, node: cst.CSTNode) -> bool:
                if isinstance(node, (cst.FunctionDef, getattr(cst, "AsyncFunctionDef", ()))):
                    for param in node.params.params + node.params.posonly_params + node.params.kwonly_params:
                        walk_annot(param.annotation)
                    containers.append(node.returns)
                    walk_annot(node.returns)
                    return True  # still descend into body for nested defs
                if isinstance(node, cst.AnnAssign):
                    walk_annot(node.annotation)
                    return True
                return True

        self.module.visit(_W())
        del containers
        return hits

    def _plan_annotation_string_renames(self, locals_of_interest: set[str]) -> set[int]:
        """Rewrite renamed locals inside lazy string annotations; returns the
        ids excluded from whole-file string blockers."""
        if not (self.future_annotations and locals_of_interest and self.renames):
            return set()
        targets = {
            symbol: f"{qualifier}.{symbol}"
            for qualifier, symbol in self.renames.values()
        }
        planned: set[int] = set()
        for ident, node in self._annotation_context_strings().items():
            full = node.value
            new_full = full
            matched_any = False
            for local, replacement in targets.items():
                pattern = rf"\b{re.escape(local)}\b"
                candidate = re.sub(pattern, replacement, new_full)
                if candidate != new_full:
                    matched_any = True
                new_full = candidate
            if matched_any:
                self.str_renames[ident] = (full, new_full)
                planned.add(ident)
        return planned

    # ------------------------------------------------------------ guards --

    def _visitor_with_positions(self, cb):
        class _V(cst.CSTVisitor):
            METADATA_DEPENDENCIES = ()

            def on_visit(vself, node: cst.CSTNode) -> bool:
                pos = self.positions.get(node)
                cb(node, pos.start if pos is not None else None)
                return True

        self.module.visit(_V())

    def _global_nonlocal_hits(self, names: set[str]) -> list[tuple[int, str]]:
        hits: list[tuple[int, str]] = []

        def cb(node, pos):
            if isinstance(node, (cst.Global, cst.Nonlocal)):
                clashing = [
                    item.name.value
                    for item in node.names
                    if item.name.value in names
                ]
                if clashing:
                    hits.append(
                        ((pos.line if pos else 0), f"'{'/'.join(clashing)}' declared {'global' if isinstance(node, cst.Global) else 'nonlocal'}")
                    )

        self._visitor_with_positions(cb)
        return hits

    def _string_mentions(self, names: set[str], *, skip_ids: set[int] | None = None) -> list[tuple[int, str]]:
        hits: list[tuple[int, str]] = []
        skip_ids = skip_ids or set()
        patterns = [(nm, re.compile(rf"\b{re.escape(nm)}\b")) for nm in sorted(names)]

        def cb(node, pos):
            if isinstance(node, cst.SimpleString):
                if id(node) in skip_ids:
                    return
                raw = node.raw_value
                for nm, pat in patterns:
                    if pat.search(raw):
                        hits.append(((pos.line if pos else 0), f"name '{nm}' appears in a string literal"))
                        break

        self._visitor_with_positions(cb)
        return hits


class _PlanExecutor(cst.CSTTransformer):
    def __init__(self, analyzer: _FixAnalyzer) -> None:
        self.a = analyzer
        self._line_frames: list[list[BindingNeed]] = []
        self._emitted: set[tuple[str, str, str]] = set()

    # ---- scope-driven renames ----

    def leave_SimpleString(
        self, original_node: cst.SimpleString, updated_node: cst.SimpleString
    ):
        planned = self.a.str_renames.get(id(original_node))
        if planned is None:
            return updated_node
        _orig_full, new_full = planned
        return updated_node.with_changes(value=new_full)

    def leave_Name(self, original_node: cst.Name, updated_node: cst.Name):
        target = self.a.renames.get(id(original_node))
        if target is None:
            return updated_node
        qualifier, symbol_leaf = target
        return cst.Attribute(value=cst.Name(qualifier), attr=cst.Name(symbol_leaf))

    # ---- physical-line bookkeeping ----

    def on_visit(self, node: cst.CSTNode) -> bool:
        if isinstance(node, cst.SimpleStatementLine):
            self._line_frames.append([])
        return True

    def leave_ImportFrom(
        self, original_node: cst.ImportFrom, updated_node: cst.ImportFrom
    ):
        plan = self.a.plans.get(id(original_node))
        if plan is None or not plan.remove_symbols:
            return updated_node
        if self._line_frames:
            frame = self._line_frames[-1]
            for need in plan.needs:
                if need.key not in [n.key for n in frame]:
                    frame.append(need)
        kept = [
            alias
            for alias in updated_node.names
            if isinstance(alias, cst.ImportAlias)
            and dotted_name(alias.name) not in plan.remove_symbols
        ]
        if not kept:
            return cst.RemoveFromParent()
        return updated_node.with_changes(names=_normalize_commas(kept))

    def leave_SimpleStatementLine(
        self, original_node: cst.SimpleStatementLine, updated_node: cst.SimpleStatementLine
    ):
        frame = self._line_frames.pop() if self._line_frames else []
        ordered: list[BindingNeed] = []
        for need in frame:
            if need.key not in [n.key for n in ordered]:
                ordered.append(need)
        effective = [n for n in ordered if not self._suppressed(n)]
        if not effective:
            return updated_node

        if _co_line_rebind_conflict(original_node, effective):
            names = "/".join(sorted({n.binding_name for n in effective}))
            raise FixAborted(
                "prepending the replacement import on this line would race "
                f"with a same-line rebinding of {names}"
            )

        new_smalls = _build_import_small_statements(effective)
        for need in effective:
            self._emitted.add(need.key)

        final_smalls: list[cst.BaseSmallStatement] = []
        total = len(new_smalls)
        for position, small in enumerate(new_smalls):
            joins_more = position < total - 1 or bool(updated_node.body)
            if joins_more:
                small = small.with_changes(
                    semicolon=cst.Semicolon(whitespace_after=cst.SimpleWhitespace(" "))
                )
            final_smalls.append(small)

        body: list[cst.BaseSmallStatement] = [*final_smalls, *updated_node.body]
        return updated_node.with_changes(body=body)

    def _suppressed(self, need: BindingNeed) -> bool:
        """A preferred from-import already present at module level."""
        return (
            need.mode == "ref"
            and (need.module, need.leaf) in self.a.existing_from_pairs
        )


def _build_import_small_statements(
    needs: list[BindingNeed],
) -> list[cst.BaseSmallStatement]:
    """Materialize prepended import small-statements, merging where possible.

    Consecutive plain-import needs merge into one ``import`` statement;
    consecutive ref needs sharing a parent merge into one from-import.
    Semicolons are attached by the caller based on surrounding context.
    """
    groups: list[cst.BaseSmallStatement] = []
    index = 0
    while index < len(needs):
        need = needs[index]
        if need.mode == "plain":
            modules: list[str] = []
            while index < len(needs) and needs[index].mode == "plain":
                mod = needs[index].module
                if mod not in modules:
                    modules.append(mod)
                index += 1
            aliases = _normalize_commas([_dotted_alias(mod) for mod in modules])
            groups.append(cst.Import(names=aliases))
            continue
        parent = need.module
        leaves: list[str] = []
        while index < len(needs) and needs[index].mode == "ref" and needs[index].module == parent:
            leaf = needs[index].leaf
            if leaf not in leaves:
                leaves.append(leaf)
            index += 1
        alias_list = _normalize_commas(
            [cst.ImportAlias(name=cst.Name(leaf)) for leaf in leaves]
        )
        groups.append(cst.ImportFrom(module=_dotted_expr(parent), names=alias_list))
    return groups

    padded = [pad(g) for g in groups]
    return [cst.SimpleStatementLine(body=[g]) for g in padded]


def _normalize_commas(
    kept: list[cst.ImportAlias],
) -> list[cst.ImportAlias]:
    """Rebuild alias list so only non-final entries carry separators."""
    out: list[cst.ImportAlias] = []
    last = len(kept) - 1
    for index, alias in enumerate(kept):
        if index < last:
            comma = (
                alias.comma
                if isinstance(alias.comma, cst.Comma)
                else cst.Comma(whitespace_after=cst.SimpleWhitespace(" "))
            )
        else:
            comma = cst.MaybeSentinel.DEFAULT
        out.append(alias.with_changes(comma=comma))
    return out


def _final_segment(dotted: str) -> str:
    return dotted.split(".")[-1]


def _dotted_expr(dotted: str) -> cst.BaseExpression:
    parts = dotted.split(".")
    node: cst.BaseExpression = cst.Name(parts[0])
    for part in parts[1:]:
        node = cst.Attribute(value=node, attr=cst.Name(part))
    return node


def _dotted_alias(dotted: str) -> cst.ImportAlias:
    return cst.ImportAlias(name=_dotted_expr(dotted))


def fix_file(
    source: str,
    path: Path,
    *,
    resolver: Resolver,
    config: Config,
    project_roots: list[Path],
) -> FixOutcome:
    try:
        module = cst.parse_module(source)
    except cst.ParserSyntaxError as exc:
        return FixOutcome("error", None, [], [Finding(path, getattr(exc, "editor_line", 0) or 0, 0, CP003, "file does not parse")], None)

    unsafe_wrapper = cst.MetadataWrapper(module, unsafe_skip_copy=True)
    scopes_map = unsafe_wrapper.resolve(cst.metadata.ScopeProvider)
    positions_map = unsafe_wrapper.resolve(cst.metadata.PositionProvider)

    analyzer = _FixAnalyzer(
        module,
        path=path,
        resolver=resolver,
        config=config,
        project_roots=list(project_roots),
        scopes_map=scopes_map,
        positions_map=positions_map,
    )
    analyzer.analyze()

    # Blockers win over plan presence: a file with ANY unsafety stays virgin.
    if analyzer.blockers:
        blocker_findings = [
            Finding(path, max(ln, 0), 0, CP003, f"cannot autofix: {msg}")
            for ln, msg in sorted(set(analyzer.blockers))
        ]
        return FixOutcome("skipped", None, [], blocker_findings, None)

    if not analyzer.plans:
        return FixOutcome("clean", None, [], [], None)

    executor = _PlanExecutor(analyzer)
    try:
        updated_module = unsafe_wrapper.visit(executor)
    except FixAborted as exc:
        return FixOutcome("skipped", None, [], [Finding(path, 0, 0, CP003, f"cannot autofix: {exc}")], None)

    new_source = updated_module.code
    if new_source == source:
        return FixOutcome("clean", None, [], [], None)

    try:
        pyast.parse(new_source)
    except SyntaxError as exc:
        return FixOutcome("error", None, [], [Finding(path, exc.lineno or 0, 0, CP003, "internal error: rewrite failed to parse; reverted")], None)

    diff = "".join(
        difflib.unified_diff(
            source.splitlines(keepends=True),
            new_source.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )
    return FixOutcome("fixed", new_source, [], [], diff)

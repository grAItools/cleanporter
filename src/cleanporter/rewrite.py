"""The fixer: rewrite ``from a.b import C`` -> ``from a import b`` + ``b.C`` uses.

Uses libCST scope metadata so that:

* only the *actual* references to the imported binding are qualified (a local
  variable that shadows the name in some function is left untouched), and
* colliding module tokens get a deterministic alias.

Safety boundary (these are reported by ``check`` but deliberately NOT auto-fixed
because a mechanical rewrite could change runtime behaviour):

* imports that are not at module scope (inside functions/classes),
* imports inside an ``if TYPE_CHECKING:`` block (rewriting could break runtime
  annotations),
* ``from x import *`` and unresolved/unknown names,
* names the resolver could not classify.

Multiple object names sharing one module reuse a single new binding; compliant
names in a mixed statement are kept in place.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

import libcst as cst
from libcst.metadata import GlobalScope, PositionProvider, ScopeProvider

from . import _imports, guards
from .analyze import FileRecord
from .config import Config
from .guards import Hit
from .model import Finding, Status
from .resolver import Resolver


@dataclass
class _Plan:
    line_repl: dict[int, list[cst.BaseStatement]] = field(default_factory=dict)
    name_repl: dict[int, cst.BaseExpression] = field(default_factory=dict)
    fixed: int = 0


def _render_alias(name: str, asname: str | None) -> str:
    return f"{name} as {asname}" if asname else name


def _type_checking_import_ids(tree: cst.Module) -> set[int]:
    """ids of ImportFrom nodes located inside an ``if TYPE_CHECKING:`` block."""
    ids: set[int] = set()

    class V(cst.CSTVisitor):
        def visit_If(self, node: cst.If) -> None:
            test = node.test
            name = ""
            if isinstance(test, cst.Name):
                name = test.value
            elif isinstance(test, cst.Attribute):
                name = test.attr.value
            if name == "TYPE_CHECKING":
                for imp in _collect_import_froms(node.body):
                    ids.add(id(imp))

    tree.visit(V())
    return ids


def _collect_import_froms(node: cst.CSTNode) -> list[cst.ImportFrom]:
    found: list[cst.ImportFrom] = []

    class V(cst.CSTVisitor):
        def visit_ImportFrom(self, n: cst.ImportFrom) -> None:
            found.append(n)

    node.visit(V())
    return found


def _collect_imports(node: cst.CSTNode) -> list[cst.Import]:
    found: list[cst.Import] = []

    class V(cst.CSTVisitor):
        def visit_Import(self, n: cst.Import) -> None:
            found.append(n)

    node.visit(V())
    return found


class _Fixer(cst.CSTTransformer):
    METADATA_DEPENDENCIES = (ScopeProvider, PositionProvider)

    def __init__(self, rec: FileRecord, resolver: Resolver, config: Config) -> None:
        super().__init__()
        self._rec = rec
        self._resolver = resolver
        self._config = config
        self.plan = _Plan()
        self.blockers: list[Hit] = []
        self._module_binding: dict[str, str] = {}  # parent -> bound token
        self._existing: dict[str, str] = {}  # already-imported module -> its name
        self._used_names: set[str] = set()
        self._tc_ids: set[int] = set()
        #: Local names this run would rewrite -- the input to every guard.
        self._fixed_locals: set[str] = set()

    # -- planning ----------------------------------------------------------
    def visit_Module(self, node: cst.Module) -> None:
        self._tc_ids = _type_checking_import_ids(node)
        # names already bound at module scope, to avoid collisions
        for _line, imp in self._import_lines(node):
            scope = self.get_metadata(ScopeProvider, imp, None)
            if isinstance(scope, GlobalScope):
                self._used_names.update(a.name for a in scope.assignments)
        self._build_existing(node)
        for line, imp in self._import_lines(node):
            self._plan_line(line, imp)
        # Guards run last, on the pristine node, before libcst descends into
        # any children (visit_Module runs on the way down). Anything a guard
        # needs to see -- e.g. Task 16's skip_ids for lazy annotations it
        # rewrites itself -- must be computed/planned above, inside this
        # method, not collected incrementally during the child traversal.
        self._run_guards(node)

    def _line_of(self, node: cst.CSTNode) -> int:
        position = self.get_metadata(PositionProvider, node, None)
        return position.start.line if position is not None else 0

    def _run_guards(self, node: cst.Module) -> None:
        if not self._fixed_locals:
            return
        self.blockers.extend(
            guards.find_string_mentions(node, self._fixed_locals, self._line_of)
        )
        self.blockers.extend(
            guards.find_scope_declarations(node, self._fixed_locals, self._line_of)
        )

    def _build_existing(self, node: cst.Module) -> None:
        """Map already-imported modules to the simple name they are bound to."""
        for _line, imp in self._import_lines(node):
            if _imports.is_star(imp) or id(imp) in self._tc_ids:
                continue
            scope = self.get_metadata(ScopeProvider, imp, None)
            if not isinstance(scope, GlobalScope):
                continue
            parent = _imports.resolve_parent(imp, self._rec.base_pkg)
            if parent is None:
                continue
            for name, asname, _alias in _imports.imported_names(imp):
                if self._resolver.is_module(parent, name) is True:
                    self._existing[f"{parent}.{name}"] = asname or name
        # plain ``import a`` / ``import a as z`` (top-level modules only)
        for plain in _collect_imports(node):
            scope = self.get_metadata(ScopeProvider, plain, None)
            if not isinstance(scope, GlobalScope):
                continue
            for alias in plain.names:
                mod = _imports.dotted(alias.name)
                bound = alias.asname.name.value if alias.asname else None
                if bound is not None:
                    self._existing[mod] = bound
                elif "." not in mod:
                    self._existing[mod] = mod

    def _import_lines(self, node: cst.Module) -> list[tuple[cst.SimpleStatementLine, cst.ImportFrom]]:
        pairs: list[tuple[cst.SimpleStatementLine, cst.ImportFrom]] = []

        class V(cst.CSTVisitor):
            def visit_SimpleStatementLine(self, line: cst.SimpleStatementLine) -> None:
                if len(line.body) == 1 and isinstance(line.body[0], cst.ImportFrom):
                    pairs.append((line, line.body[0]))

        node.visit(V())
        return pairs

    def _plan_line(self, line: cst.SimpleStatementLine, imp: cst.ImportFrom) -> None:
        if _imports.is_star(imp) or id(imp) in self._tc_ids:
            return
        scope = self.get_metadata(ScopeProvider, imp, None)
        if not isinstance(scope, GlobalScope):
            return  # only module-level imports are auto-fixed
        parent = _imports.resolve_parent(imp, self._rec.base_pkg)
        if parent is None:
            return

        keep: list[str] = []
        fix: list[tuple[str, str | None]] = []
        for name, asname, _alias in _imports.imported_names(imp):
            if self._config.is_exempt(parent, name):
                keep.append(_render_alias(name, asname))
                continue
            verdict = self._resolver.is_module(parent, name)
            if verdict is True or verdict is None:
                keep.append(_render_alias(name, asname))
            else:
                fix.append((name, asname))
        if not fix:
            return
        self._fixed_locals.update(asname or name for name, asname in fix)

        # new statements: one module import per (deduped) parent, plus kept names
        new_lines: list[cst.BaseStatement] = []
        bind = self._binding_for(parent)
        if bind is not None:  # None => module already bound earlier in file
            new_lines.append(self._module_import_stmt(parent, bind))
        bind = self._module_binding[parent]

        if keep:
            prefix = "." * _imports.relative_level(imp)
            mod = _imports.dotted(imp.module)
            new_lines.append(
                cst.ensure_type(
                    cst.parse_statement(f"from {prefix}{mod} import {', '.join(keep)}"),
                    cst.SimpleStatementLine,
                )
            )

        # qualify references for each fixed name
        for name, asname in fix:
            bound = asname or name
            ours = [a for a in scope[bound] if getattr(a, "node", None) is imp]
            # No BuiltinAssignment filter needed here: libcst only ever puts
            # BuiltinAssignment objects in a BuiltinScope's own assignments,
            # never in a GlobalScope's. GlobalScope.__getitem__ returns its
            # own assignments directly whenever the name is present there at
            # all, and `ours` being non-empty means `bound` is already
            # present -- so a builtin can never show up alongside our import.
            others = [a for a in scope[bound] if getattr(a, "node", None) is not imp]
            if ours and others:
                # libcst's scopes are not flow-sensitive, so accesses of a
                # rebound name list both the import and the assignment as
                # referents. There is no safe subset to rewrite.
                self.blockers.append(
                    (self._line_of(imp), f"local '{bound}' is rebound in the same scope")
                )
                continue
            for assignment in ours:
                for ref in assignment.references:
                    self.plan.name_repl[id(ref.node)] = cst.Attribute(
                        value=cst.Name(bind), attr=cst.Name(name)
                    )
            self.plan.fixed += 1

        # carry the original line's leading comments/blank lines onto the first
        # replacement line (if any); an empty list means the line is removed
        # entirely because the module is already imported and nothing is kept.
        if new_lines:
            first = new_lines[0]
            if isinstance(first, cst.SimpleStatementLine):
                new_lines[0] = first.with_changes(leading_lines=line.leading_lines)
        self.plan.line_repl[id(line)] = new_lines

    def _binding_for(self, parent: str) -> str | None:
        """Resolve the binding token for ``parent``.

        Returns the token of a *new* import to emit, or ``None`` when the module
        is already bound (either earlier in this fix run, or by a pre-existing
        import in the file) so no new import is needed.
        """
        if parent in self._module_binding:
            return None
        # Reuse a module that the file already imports (avoids a duplicate).
        existing = self._existing.get(parent)
        if existing is not None:
            self._module_binding[parent] = existing
            return None
        token = parent.rsplit(".", 1)[-1]
        bind = token
        i = 2
        while bind in self._used_names:
            bind = f"{token}_{i}"
            i += 1
        self._used_names.add(bind)
        self._module_binding[parent] = bind
        return bind

    def _module_import_stmt(self, parent: str, bind: str) -> cst.SimpleStatementLine:
        if "." in parent:
            pkg, token = parent.rsplit(".", 1)
            code = f"from {pkg} import {token}"
        else:
            token = parent
            code = f"import {parent}"
        if bind != token:
            code += f" as {bind}"
        return cst.ensure_type(cst.parse_statement(code), cst.SimpleStatementLine)

    # -- application -------------------------------------------------------
    def leave_SimpleStatementLine(self, original: cst.SimpleStatementLine, updated: cst.SimpleStatementLine):
        repl = self.plan.line_repl.get(id(original))
        if repl is not None:
            return cst.FlattenSentinel(repl) if repl else cst.RemovalSentinel.REMOVE
        return updated

    def leave_Name(self, original: cst.Name, updated: cst.Name):
        repl = self.plan.name_repl.get(id(original))
        return repl if repl is not None else updated

    def leave_Module(self, original: cst.Module, updated: cst.Module) -> cst.Module:
        # All-or-nothing. libcst hands us the pristine original tree, so
        # returning it discards every edit made to the children.
        return original if self.blockers else updated


@dataclass
class FixOutcome:
    """Result of attempting to fix one file.

    ``source`` is always a string -- the resulting source when ``status`` is
    ``"fixed"``, otherwise the unchanged input -- so callers never need to
    branch on ``None``.
    """

    status: str  # "fixed" | "clean" | "skipped" | "error"
    source: str
    blockers: list[Finding] = field(default_factory=list)
    fixed: int = 0


def fix_record(rec: FileRecord, resolver: Resolver, config: Config) -> FixOutcome:
    """Rewrite one file, or leave it exactly as it was and say why."""
    wrapper = cst.MetadataWrapper(rec.tree, unsafe_skip_copy=True)
    fixer = _Fixer(rec, resolver, config)
    new_source = wrapper.visit(fixer).code

    if fixer.blockers:
        return FixOutcome(
            "skipped",
            rec.source,
            [
                Finding(rec.path, line, 0, "?", "?", Status.SKIPPED, reason)
                for line, reason in sorted(set(fixer.blockers))
            ],
        )
    if not fixer.plan.fixed or new_source == rec.source:
        return FixOutcome("clean", rec.source)

    try:
        ast.parse(new_source)
    except SyntaxError as exc:
        # Never hand back source we cannot compile. Keep the original.
        return FixOutcome(
            "error",
            rec.source,
            [
                Finding(
                    rec.path, exc.lineno or 0, 0, "?", "?", Status.SKIPPED,
                    "internal error: the rewrite did not parse; reverted",
                )
            ],
        )

    return FixOutcome("fixed", new_source, [], fixer.plan.fixed)

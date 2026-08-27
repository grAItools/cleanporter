"""The fixer: rewrite ``from a.b import C`` -> ``from a import b`` + ``b.C`` uses.

Uses libCST scope metadata so that:

* only the *actual* references to the imported binding are qualified (a local
  variable that shadows the name in some function is left untouched), and
* colliding module tokens get a deterministic alias.

Imports inside functions/classes are fixed in place too: each scope that
imports a module gets its own binding, tracked independently of any
module-level import of the same module.

Safety boundary (these are reported by ``check`` but deliberately NOT auto-fixed
because a mechanical rewrite could change runtime behaviour):

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
from libcst.metadata import (
    BaseAssignment,
    GlobalScope,
    PositionProvider,
    Scope,
    ScopeProvider,
)

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
        self._module_binding: dict[tuple[Scope, str], str] = {}  # (scope, parent) -> bound token
        self._existing: dict[str, str] = {}  # already-imported module -> its name
        #: Names bound at module scope. Kept *live*: grows as `_binding_for`
        #: allocates new module-level tokens, so a later function scope's
        #: collision check sees them (fix-round-1 Critical 2).
        self._global_names: set[str] = set()
        #: Per-scope cache of `_local_names`, mutated in place by
        #: `_binding_for` for the same reason `_global_names` is live.
        self._scope_locals: dict[Scope, set[str]] = {}
        self._tc_ids: set[int] = set()
        #: Local names this run would rewrite -- the input to every guard.
        self._fixed_locals: set[str] = set()

    # -- planning ----------------------------------------------------------
    def visit_Module(self, node: cst.Module) -> None:
        self._tc_ids = _type_checking_import_ids(node)
        # Names already bound at module scope, seen from *any* scope's
        # `.globals` -- not gated on the import itself being GlobalScope-
        # scoped. A file whose only import is function-local has no
        # GlobalScope-scoped import line at all, and gating on one would
        # silently leave `_global_names` empty (fix-round-1 Critical 1).
        for _line, imp in self._import_lines(node):
            scope = self.get_metadata(ScopeProvider, imp, None)
            if scope is not None:
                self._global_names = {a.name for a in scope.globals.assignments}
                break
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
        if scope is None:
            return
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

        # Resolve each fixed name's rebinding status, and collect the
        # accesses that will actually be qualified, *before* choosing a
        # token: a nested scope that shadows the bound name at one of those
        # access sites must be avoided too, or the qualified reference
        # would silently resolve to the local instead of the new import
        # (fix-round-2 downward shadowing -- the mirror of fix-round-1's
        # ancestor check, but for scopes *below* where the binding lands).
        rewrites: list[tuple[str, list[BaseAssignment]]] = []
        extra_avoid: set[str] = set()
        for name, asname in fix:
            bound = asname or name
            ours = [a for a in scope[bound] if getattr(a, "node", None) is imp]
            # No BuiltinAssignment filter needed here: libcst only ever puts
            # BuiltinAssignment objects in a BuiltinScope's own assignments,
            # never in a GlobalScope's or a LocalScope's (FunctionScope /
            # ClassScope, now that non-module scopes are fixed too).
            # GlobalScope.__getitem__ and LocalScope's (via
            # LocalScope._resolve_scope_for_access) both return the scope's
            # own assignments directly whenever the name is present there at
            # all, and `ours` being non-empty means `bound` is already
            # present in *this* scope's own assignments -- so a builtin can
            # never show up alongside our import, regardless of whether
            # `scope` is global, a function, or a class body.
            others = [a for a in scope[bound] if getattr(a, "node", None) is not imp]
            if ours and others:
                # libcst's scopes are not flow-sensitive, so accesses of a
                # rebound name list both the import and the assignment as
                # referents. There is no safe subset to rewrite.
                self.blockers.append(
                    (self._line_of(imp), f"local '{bound}' is rebound in the same scope")
                )
                continue
            rewrites.append((name, ours))
            for assignment in ours:
                for ref in assignment.references:
                    access_scope: Scope = ref.scope
                    if access_scope is not scope:
                        extra_avoid |= self._shadow_names_between(access_scope, scope)

        # new statements: one module import per (deduped) parent, plus kept names
        new_lines: list[cst.BaseStatement] = []
        bind, need_new_line = self._binding_for(scope, parent, extra_avoid)
        if need_new_line:
            new_lines.append(self._module_import_stmt(parent, bind))

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
        for name, ours in rewrites:
            for assignment in ours:
                for ref in assignment.references:
                    self.plan.name_repl[id(ref.node)] = cst.Attribute(
                        value=cst.Name(bind), attr=cst.Name(name)
                    )
            self.plan.fixed += 1

        # carry the original line's leading comments/blank lines onto the first
        # replacement line, and its trailing comment onto the last (if any);
        # an empty list means the line is removed entirely because the module
        # is already imported and nothing is kept.
        has_trailing_comment = line.trailing_whitespace.comment is not None
        # leading_lines holds both comment lines and plain blank lines
        # (EmptyLine with comment=None); only a real comment should block --
        # otherwise every import preceded by a blank line would block.
        has_leading_comment = any(
            empty_line.comment is not None for empty_line in line.leading_lines
        )
        if new_lines:
            first = new_lines[0]
            if isinstance(first, cst.SimpleStatementLine):
                new_lines[0] = first.with_changes(leading_lines=line.leading_lines)
            # Read new_lines[-1] after the [0] reassignment above: for a
            # single-statement replacement they are the same element, and
            # this lookup must see the already-updated node so both
            # leading_lines and trailing_whitespace land on it.
            last = new_lines[-1]
            if isinstance(last, cst.SimpleStatementLine):
                new_lines[-1] = last.with_changes(
                    trailing_whitespace=line.trailing_whitespace
                )
        elif has_trailing_comment or has_leading_comment:
            # The line disappears entirely (the module is already bound and
            # nothing is kept), so there is nowhere to put the author's
            # comment -- leading or trailing. Silently discarding it is worse
            # than declining to fix the file, so block instead. Anchored to
            # the line itself (not the ``imp`` node used by the rebound-name
            # blocker above) because the comment being lost belongs to the
            # line, not to any one imported name.
            if has_trailing_comment:
                self.blockers.append(
                    (
                        self._line_of(line),
                        "removing this import would discard its trailing comment",
                    )
                )
            if has_leading_comment:
                self.blockers.append(
                    (
                        self._line_of(line),
                        "removing this import would discard its leading comment(s)",
                    )
                )
            return
        self.plan.line_repl[id(line)] = new_lines

    def _local_names(self, scope: Scope) -> set[str]:
        """Names assigned directly in *scope*, ignoring enclosing scopes.

        Cached and mutated in place: `_binding_for` adds each token it
        allocates in *scope* here, so a later lookup in the same scope sees
        it immediately, without waiting for a real AST assignment to back
        it (fix-round-1 Critical 2).
        """
        if scope not in self._scope_locals:
            self._scope_locals[scope] = {a.name for a in scope.assignments}
        return self._scope_locals[scope]

    def _ancestor_local_names(self, scope: Scope) -> set[str]:
        """Names assigned in *scope* or any enclosing function/class scope.

        Walks ``scope.parent`` upward, unioning `_local_names` at each
        level, and stops at (and excludes) the module scope -- those names
        live in `_global_names` instead, which is kept live as
        `_binding_for` allocates module-level tokens. Never continues past
        the module scope into `BuiltinScope`: avoiding a builtin's name
        would alias unnecessarily. `ClassScope` levels are included like
        any other -- harmless over-conservatism (fix-round-1 Critical 3).
        """
        names: set[str] = set()
        current = scope
        while not isinstance(current, GlobalScope):
            names |= self._local_names(current)
            current = current.parent
        return names

    def _names_in_scope(self, scope: Scope) -> set[str]:
        """Names a new binding in *scope* must not collide with: names
        assigned in *scope* itself, in any enclosing function/class scope,
        or at module scope.
        """
        return self._ancestor_local_names(scope) | self._global_names

    def _shadow_names_between(self, access_scope: Scope, upper_scope: Scope) -> set[str]:
        """Names assigned in *access_scope*, or in any scope strictly
        between it and *upper_scope*, walking ``.parent`` upward.

        *upper_scope* itself is excluded -- its own names are handled by
        the ordinary collision check (`_names_in_scope`) at the point the
        binding is allocated there. This answers a different question: a
        binding placed in *upper_scope* is also read from *access_scope*
        (a nested function/class body), and any name that scope -- or one
        between it and *upper_scope* -- assigns for itself would silently
        shadow the qualified reference at that access site (fix-round-2
        downward shadowing, the mirror of `_ancestor_local_names`'s upward
        walk). `access_scope` is always *upper_scope* or a descendant of
        it, because an access can only resolve to an assignment that is
        visible to it, i.e. in its own scope or an enclosing one -- so this
        walk is guaranteed to reach *upper_scope*. The `GlobalScope` check
        is a defensive backstop only, in case that invariant is ever wrong.
        """
        names: set[str] = set()
        current = access_scope
        while current is not upper_scope and not isinstance(current, GlobalScope):
            names |= self._local_names(current)
            current = current.parent
        return names

    def _binding_for(self, scope: Scope, parent: str, extra_avoid: set[str]) -> tuple[str, bool]:
        """Token to qualify *this line's* references through, and whether a
        new import statement must be emitted for it.

        *extra_avoid* is the set of names (from `_shadow_names_between`)
        that a scope nested below *scope* -- specifically, one containing
        an access *this line* is about to qualify -- would resolve to a
        local instead of this binding. It is per-**line**, not per
        ``(scope, parent)``: two import lines sharing a parent can have
        different accesses, and therefore different downward-shadow
        requirements. `_module_binding` is memoized per ``(scope, parent)``
        though, so that distinction has to be re-checked on every call, not
        just the first (fix-round-3 -- the dedup early-return used to
        bypass `extra_avoid` entirely for every line after the first).

        Returns ``(bind, True)`` when this line needs its own new import
        statement: either *parent* has never been bound in *scope* before,
        or it has, but the memoized token collides with *this* line's
        *extra_avoid*. In the latter case a *fresh* token is allocated for
        this line alone -- not reused, not blocked -- since a second import
        of the same module under a different alias is semantically fine;
        the memoized token is left untouched so a later, non-colliding line
        for the same ``(scope, parent)`` still reuses it.

        Returns ``(bind, False)`` when the memoized token, or a
        pre-existing import already in the file, can be reused as-is.
        """
        key = (scope, parent)
        memoized = self._module_binding.get(key)
        if memoized is not None:
            if memoized not in extra_avoid:
                return memoized, False
            return self._allocate_token(scope, parent, extra_avoid), True

        existing = self._existing.get(parent)
        if existing is not None:
            # A module-level import is visible from nested scopes unless
            # *this* scope or an enclosing function/class scope assigns
            # that name itself -- a closure variable shadows it just as
            # surely as a same-scope local does (fix-round-1 Critical 3).
            # It is equally unusable if a scope *below* this one, where one
            # of this import's references actually lives, assigns that name
            # itself (fix-round-2): that check applies regardless of
            # `scope`'s own kind, unlike the ancestor check above.
            shadowed = existing in extra_avoid or (
                not isinstance(scope, GlobalScope) and existing in self._ancestor_local_names(scope)
            )
            if not shadowed:
                self._module_binding[key] = existing
                return existing, False

        bind = self._allocate_token(scope, parent, extra_avoid)
        self._module_binding[key] = bind
        return bind, True

    def _allocate_token(self, scope: Scope, parent: str, extra_avoid: set[str]) -> str:
        """Pick a fresh, collision-free token for a new import of *parent*
        in *scope*.

        Records the choice in the live name set(s) `_names_in_scope` reads
        (`_global_names` for `GlobalScope`, else `_local_names(scope)`), so
        a later allocation in this scope -- or one that sees it as an
        ancestor -- avoids it too. Does *not* touch `_module_binding`: that
        is the caller's job, since a fix-round-3 collision reallocation
        deliberately leaves the original memoized entry alone.
        """
        token = parent.rsplit(".", 1)[-1]
        taken = self._names_in_scope(scope) | extra_avoid
        bind = token
        counter = 2
        while bind in taken:
            bind = f"{token}_{counter}"
            counter += 1
        if isinstance(scope, GlobalScope):
            self._global_names.add(bind)
        else:
            self._local_names(scope).add(bind)
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
    except (SyntaxError, ValueError) as exc:
        # Never hand back source we cannot compile. Keep the original.
        # On CPython <3.12, ValueError is raised for embedded null bytes;
        # SyntaxError covers syntax errors. Both must be caught.
        return FixOutcome(
            "error",
            rec.source,
            [
                Finding(
                    rec.path, getattr(exc, "lineno", None) or 0, 0, "?", "?", Status.SKIPPED,
                    "internal error: the rewrite did not parse; reverted",
                )
            ],
        )

    return FixOutcome("fixed", new_source, [], fixer.plan.fixed)

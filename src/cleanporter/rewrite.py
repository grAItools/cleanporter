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

* imports inside an ``if TYPE_CHECKING:`` block, *unless* the file has
  ``from __future__ import annotations`` -- with it, annotations are strings
  at runtime, so both the import and any lazy string annotation mentioning
  the name are rewritten together; without it, rewriting risks ``NameError``,
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
    ClassScope,
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
    string_repl: dict[int, str] = field(default_factory=dict)
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


def _deleted_names(tree: cst.Module) -> set[str]:
    """Simple names that appear as a ``del`` target anywhere in the file.

    libcst's scope analysis records ``del x`` as an *access* of ``x``, not as
    an assignment, so a deleted name sails straight past the rebinding guard
    and lands in ``plan.name_repl`` like any other reference -- turning
    ``del Thing`` into ``del mod.Thing``, which does not unbind a local at
    all: it deletes the attribute from ``sys.modules['a.mod']``, silently
    breaking every *other* importer of that module. ``del name`` right after
    an import is a real ``__init__.py`` cleanup idiom, so this is not
    theoretical.

    Only bare names (including inside a ``del a, b`` tuple/list target)
    count. ``del obj.attr`` / ``del obj[k]`` rebind nothing, so descending
    into an ``Attribute``/``Subscript`` would only cause spurious blocks.
    """
    names: set[str] = set()

    def absorb(target: cst.BaseExpression) -> None:
        if isinstance(target, cst.Name):
            names.add(target.value)
        elif isinstance(target, (cst.Tuple, cst.List)):
            for element in target.elements:
                absorb(element.value)

    class V(cst.CSTVisitor):
        def visit_Del(self, node: cst.Del) -> None:
            absorb(node.target)

    tree.visit(V())
    return names


def _interior_comments(node: cst.CSTNode) -> bool:
    """Any comment sitting *inside* this node's own whitespace.

    The kept-names line is regenerated from text via `cst.parse_statement`,
    which cannot carry interior trivia across, so a comment inside a
    parenthesized multi-line import -- including a per-name ``# noqa:`` or
    ``# type: ignore`` -- would be silently dropped. Line-level trivia
    (``leading_lines`` / ``trailing_whitespace``) belongs to the enclosing
    `SimpleStatementLine`, is carried over explicitly, and is checked
    separately; this looks only at what the regenerated statement would
    lose.
    """
    found = False

    class V(cst.CSTVisitor):
        def visit_Comment(self, comment: cst.Comment) -> None:
            nonlocal found
            found = True

    node.visit(V())
    return found


def _collect_imports(node: cst.CSTNode) -> list[cst.Import]:
    found: list[cst.Import] = []

    class V(cst.CSTVisitor):
        def visit_Import(self, n: cst.Import) -> None:
            found.append(n)

    node.visit(V())
    return found


#: Subscript bases whose slice contents are not type references, so a
#: string sitting inside them must never be treated as an annotation for
#: renaming purposes (matched on the final attribute name, so both
#: ``Literal[...]`` and ``typing.Literal[...]`` are caught).
_OPAQUE_SUBSCRIPT_BASES = frozenset({"Literal", "Annotated"})


def _subscript_base_name(node: cst.Subscript) -> str:
    value = node.value
    if isinstance(value, cst.Name):
        return value.value
    if isinstance(value, cst.Attribute):
        return value.attr.value
    return ""


def _annotation_strings(tree: cst.Module) -> dict[int, cst.SimpleString]:
    """String literals sitting in a genuine annotation slot.

    Under ``from __future__ import annotations`` these are never evaluated at
    runtime, so a textual rename inside them is safe.

    ``Literal[...]`` arguments are values, not type references, so its whole
    slice is skipped. ``Annotated[T, ...]`` mixes a real type (the first
    slice element) with arbitrary metadata (the rest); only that first
    element is descended into, so a string sitting in the metadata -- which
    might coincidentally contain the renamed name as prose -- is never
    mistaken for a type reference. ``Optional['Thing']``, ``list['Thing']``
    and ``dict[str, 'Thing']`` are ordinary subscripts and are walked in
    full, since every slice element there is a genuine type.
    """
    found: dict[int, cst.SimpleString] = {}

    def absorb(annotation: cst.Annotation | None) -> None:
        if annotation is None:
            return
        stack: list[cst.CSTNode] = [annotation.annotation]
        while stack:
            node = stack.pop()
            if isinstance(node, cst.SimpleString):
                found[id(node)] = node
                continue
            if isinstance(node, cst.Subscript):
                base = _subscript_base_name(node)
                if base in _OPAQUE_SUBSCRIPT_BASES:
                    if base == "Annotated" and node.slice:
                        first = node.slice[0].slice
                        if isinstance(first, cst.Index):
                            stack.append(first.value)
                    continue
            stack.extend(node.children)

    class V(cst.CSTVisitor):
        def visit_FunctionDef(self, node: cst.FunctionDef) -> None:
            params = node.params
            for param in (
                list(params.params)
                + list(params.posonly_params)
                + list(params.kwonly_params)
                + ([params.star_arg] if isinstance(params.star_arg, cst.Param) else [])
                + ([params.star_kwarg] if params.star_kwarg is not None else [])
            ):
                absorb(param.annotation)
            absorb(node.returns)

        def visit_AnnAssign(self, node: cst.AnnAssign) -> None:
            absorb(node.annotation)

    tree.visit(V())
    return found


class _UnrenderableAnnotation(Exception):
    """A (possibly nested) forward-reference string cannot be safely
    re-rendered -- either its content does not parse as an expression, or
    the rewritten result, re-wrapped in the original prefix/quote, does not
    round-trip back to exactly that result (fix-round-4: re-wrapping a
    decoded, unescaped render raw in the original quote character can
    change what the string means -- an escaped quote, a newline, or a
    quote landing adjacent to a triple-quote boundary -- so the wrapped
    text is re-parsed and compared rather than assumed safe).

    Always caught at the single outermost `_rewrite_string_content` call --
    the one `_plan_annotation_strings` makes directly -- never partway
    through a nested structure. A failure anywhere inside a candidate
    string (at any depth) must abort that *entire* candidate's rewrite
    rather than record a partially-rewritten value with an unclassifiable
    leftover fragment nobody checked (fix-round-3 New 2: a `dict['Thing',
    'Thing[']` where the second element fails to parse must not let the
    first element's successful rename slip into `plan.string_repl` on its
    own -- the surviving, untouched `'Thing['` would then be hidden from
    the string-mention guard by the very `skip_ids` entry that rename
    produced).
    """


def _rewrite_type_expr(
    expr: cst.BaseExpression, targets: dict[str, str]
) -> tuple[cst.BaseExpression, bool]:
    """Rename bare ``Name`` references to a rewritten local anywhere inside
    a *parsed* type expression.

    This is a structural walk, not a text substitution, so it cannot be
    fooled by a name that merely appears as a substring or after a ``.``:
    only an actual ``Name`` node matching a target is ever replaced, and
    only when it sits in a genuine reference position.

    * The ``.attr`` half of an ``Attribute`` (``other.Thing``'s ``Thing``)
      is a syntax slot, not an independent reference, and is never visited.
    * ``Literal[...]``'s slice holds values, not types, and is skipped
      whole; ``Annotated[T, ...]`` mixes one real type (the first slice
      element) with arbitrary metadata (the rest), so only that first
      element is walked -- the same opacity rules ``_annotation_strings``
      applies at the CST level, reapplied here because a *parsed* string's
      content can contain a ``Literal``/``Annotated`` subscript that was
      never visible as a CST node to that first pass (fix-round-2 Critical
      2: a fully-stringified annotation like ``"Literal['Thing']"`` has no
      ``Subscript`` node until its content is parsed).
    * A nested ``SimpleString`` sitting in an otherwise genuine type
      position (e.g. the ``'Thing'`` in ``list['Thing']``, itself possibly
      reached only after parsing an outer string) is a forward reference in
      its own right and is recursed into via `_rewrite_string_content`, so
      a doubly-stringified annotation is handled exactly like a
      singly-stringified one. Its failure -- `_UnrenderableAnnotation` --
      is deliberately not caught here: it must propagate past every
      enclosing ``Subscript``/``Attribute``/... frame uncaught, all the way
      to the one call site that is allowed to swallow it.

    Returns ``(expr, False)`` unchanged when nothing needed renaming, so a
    caller can tell "nothing to do" from "rewrote to something identical".
    """
    if isinstance(expr, cst.Name):
        target = targets.get(expr.value)
        if target is None:
            return expr, False
        bind, _dot, symbol = target.partition(".")
        return cst.Attribute(value=cst.Name(bind), attr=cst.Name(symbol)), True

    if isinstance(expr, cst.Attribute):
        new_value, changed = _rewrite_type_expr(expr.value, targets)
        return (expr.with_changes(value=new_value), True) if changed else (expr, False)

    if isinstance(expr, cst.SimpleString):
        rewritten = _rewrite_string_content(expr, targets)
        return (rewritten, True) if rewritten is not None else (expr, False)

    if isinstance(expr, cst.BinaryOperation):
        new_left, left_changed = _rewrite_type_expr(expr.left, targets)
        new_right, right_changed = _rewrite_type_expr(expr.right, targets)
        if not (left_changed or right_changed):
            return expr, False
        return expr.with_changes(left=new_left, right=new_right), True

    if isinstance(expr, (cst.Tuple, cst.List)):
        changed_any = False
        new_elements = []
        for element in expr.elements:
            new_value, changed = _rewrite_type_expr(element.value, targets)
            if changed:
                changed_any = True
                element = element.with_changes(value=new_value)
            new_elements.append(element)
        if not changed_any:
            return expr, False
        return expr.with_changes(elements=new_elements), True

    if isinstance(expr, cst.Subscript):
        base = _subscript_base_name(expr)
        new_base, base_changed = _rewrite_type_expr(expr.value, targets)
        slice_changed = False
        new_slice: list[cst.SubscriptElement] = list(expr.slice)
        if base in _OPAQUE_SUBSCRIPT_BASES:
            # "Literal": the whole slice holds opaque values, never
            # touched. "Annotated": only the first slice element (the real
            # type) is walked; the rest (metadata) is left alone.
            if base == "Annotated" and expr.slice:
                first = expr.slice[0]
                if isinstance(first.slice, cst.Index):
                    new_value, changed = _rewrite_type_expr(first.slice.value, targets)
                    if changed:
                        slice_changed = True
                        new_slice[0] = first.with_changes(
                            slice=first.slice.with_changes(value=new_value)
                        )
        else:
            for i, slice_element in enumerate(expr.slice):
                if isinstance(slice_element.slice, cst.Index):
                    new_value, changed = _rewrite_type_expr(slice_element.slice.value, targets)
                    if changed:
                        slice_changed = True
                        new_slice[i] = slice_element.with_changes(
                            slice=slice_element.slice.with_changes(value=new_value)
                        )
        if not (base_changed or slice_changed):
            return expr, False
        return (
            expr.with_changes(
                value=new_base if base_changed else expr.value,
                slice=new_slice,
            ),
            True,
        )

    return expr, False


def _rewrite_string_content(
    node: cst.SimpleString, targets: dict[str, str]
) -> cst.SimpleString | None:
    """Re-parse *node*'s content as a type expression and rename it via
    `_rewrite_type_expr`.

    Returns ``None`` when the content parses fine but nothing in it needed
    renaming (e.g. it is a `Literal`/`Annotated` payload, or mentions the
    name only after a ``.``) -- a genuinely inert string, safe to leave
    exactly as it is.

    Raises `_UnrenderableAnnotation` -- never caught here, only by the
    single outermost call -- when the content cannot be safely classified
    or re-rendered at all: it is not decodable text (e.g. a bytes literal),
    it does not parse as an expression, or the rewritten result re-wrapped
    in the original prefix/quote does not round-trip back to exactly that
    result (fix-round-4, subsuming fix-round-3 New 1). "Never guess:
    unclassifiable means reported, not rewritten" applies here exactly as
    it does to an import the resolver cannot classify.
    """
    content = node.evaluated_value
    if not isinstance(content, str):
        raise _UnrenderableAnnotation("non-text string content (e.g. bytes)")
    try:
        parsed = cst.parse_expression(content)
    except cst.ParserSyntaxError as exc:
        raise _UnrenderableAnnotation("content does not parse as an expression") from exc
    new_expr, changed = _rewrite_type_expr(parsed, targets)
    if not changed:
        return None
    # Rendering a parsed expression drops anything the parse did not attach
    # to a node -- notably a trailing comment: `'Thing  # note'` parses to a
    # bare `Name` and renders back as `'Thing'`, silently deleting the
    # author's note. Rather than enumerate what can be lost, re-render the
    # *original* parse and require it to reproduce the content exactly; if
    # it cannot, this string is unrenderable and blocks, exactly as it would
    # if it had failed to parse. (Same defect family as the interior-comment
    # check on import lines: content loss is worse than declining the fix.)
    if cst.Module(body=[]).code_for_node(parsed) != content:
        raise _UnrenderableAnnotation("content carries trivia that re-rendering would drop")
    rendered = cst.Module(body=[]).code_for_node(new_expr)
    # `evaluated_value` *decodes* the content, so any escape it resolved is
    # gone by the time `rendered` exists: an escaped occurrence of the outer
    # quote character, a `\n` that is now a real newline, a quote that lands
    # adjacent to a triple-quote boundary. Re-wrapping raw in `node.quote`
    # can therefore produce text that no longer means what it did. Rather
    # than enumerate which characters are unsafe -- a moving target that has
    # been under-approximated twice -- verify that the re-wrapped value
    # actually round-trips: it must parse, parse *as a string*, and carry
    # exactly the content we intended (fix-round-4). Anything else is
    # unrenderable and falls back to the ordinary string-mention guard.
    new_value = f"{node.prefix}{node.quote}{rendered}{node.quote}"
    try:
        check = cst.parse_expression(new_value)
    except cst.ParserSyntaxError as exc:
        raise _UnrenderableAnnotation("re-wrapped value does not parse") from exc
    if not isinstance(check, cst.SimpleString) or check.evaluated_value != rendered:
        raise _UnrenderableAnnotation("re-wrapped value does not round-trip")
    return node.with_changes(value=new_value)


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
        #: Names appearing as a ``del`` target (see `_deleted_names`).
        self._del_names: set[str] = set()
        #: Local names this run would rewrite -- the input to every guard.
        self._fixed_locals: set[str] = set()
        self._future_annotations = False
        #: (binding scope) -> {local name -> "token.name"}, for lazy string
        #: annotations (Task 16). Keyed by scope, because a rename is only
        #: valid for annotation strings that can actually *see* that binding.
        self._string_targets: dict[Scope, dict[str, str]] = {}

    # -- planning ----------------------------------------------------------
    def visit_Module(self, node: cst.Module) -> None:
        self._tc_ids = _type_checking_import_ids(node)
        self._del_names = _deleted_names(node)
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
        self._future_annotations = any(
            _imports.resolve_parent(imp, self._rec.base_pkg) == "__future__"
            and any(n == "annotations" for n, _a, _x in _imports.imported_names(imp))
            for _line, imp in self._import_lines(node)
        )
        # Runtime (non-TYPE_CHECKING) lines are planned before TYPE_CHECKING
        # ones, regardless of their textual order in the file. `_binding_for`
        # memoizes one token per (scope, parent) and happily reuses it for a
        # later line -- reusing a *runtime* binding from inside a
        # TYPE_CHECKING block is safe (the name exists at runtime either
        # way), but the reverse is not: a TYPE_CHECKING block is GlobalScope
        # to libcst just like the module body, so a binding created *there*
        # would get memoized first and then wrongly reused -- and its own
        # import line deleted -- by a later runtime import of the same
        # parent, leaving the only binding inside a block that never runs
        # (fix-round-1 Critical 1). Planning runtime lines first guarantees
        # any shared parent's binding is the runtime one.
        import_lines = self._import_lines(node)
        for line, imp in import_lines:
            if id(imp) not in self._tc_ids:
                self._plan_line(line, imp)
        for line, imp in import_lines:
            if id(imp) in self._tc_ids:
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
        skip_ids = self._plan_annotation_strings(node)
        self.blockers.extend(
            guards.find_string_mentions(
                node, self._fixed_locals, self._line_of, skip_ids=skip_ids
            )
        )
        self.blockers.extend(
            guards.find_scope_declarations(node, self._fixed_locals, self._line_of)
        )

    def _plan_annotation_strings(self, node: cst.Module) -> frozenset[int]:
        """Rename locals inside lazy string annotations; return their ids.

        Each candidate string's *content* is parsed as an expression and
        walked structurally by `_rewrite_type_expr`, rather than pattern-
        matched as raw text: a regex substitution over an unparsed token
        cannot tell a type reference from a string payload (a `Literal`
        value, `Annotated` metadata, or a dotted name it does not own), so
        it can only ever be patched for one more shape at a time
        (fix-round-1 added a lookbehind for ``other.Thing``, fix-round-2
        found ``"Literal['Thing']"`` still slipped through because that
        whole annotation is one string with no `Subscript` node for the
        earlier CST-level narrowing to see). Parsing sidesteps the whole
        class of bugs: a string that fails to parse is never guessed at --
        it is left for the ordinary string-mention guard to block.
        """
        if not (self._future_annotations and self._string_targets):
            return frozenset()
        for ident, string_node in _annotation_strings(node).items():
            # Only renames the *enclosing scope of this string* can actually
            # see. `plan.name_repl` has always been scope-aware; this path
            # was not, and a flat table let a function-local alias be
            # written into a module-level annotation, naming a binding that
            # does not exist there (final review Critical 4). An empty
            # target set leaves the string untouched, which is the safe
            # direction: it then stays visible to the string-mention guard.
            targets = self._targets_visible_from(
                self.get_metadata(ScopeProvider, string_node, None)
            )
            if not targets:
                continue
            # This is the one call site allowed to swallow
            # `_UnrenderableAnnotation`: it means *this* candidate string
            # (possibly via a nested one, arbitrarily deep) could not be
            # safely classified or re-rendered at all, so it is left
            # exactly as it is, for the ordinary string-mention guard to
            # judge on its own.
            try:
                rewritten = _rewrite_string_content(string_node, targets)
            except _UnrenderableAnnotation:
                continue
            if rewritten is not None:
                self.plan.string_repl[ident] = rewritten.value
        return frozenset(self.plan.string_repl)

    def _targets_visible_from(self, scope: Scope | None) -> dict[str, str]:
        """Merge the string-rename tables of every scope *scope* can read.

        Walks outward from *scope* to module scope, nearer bindings winning,
        mirroring ordinary Python name resolution: a class body's names are
        *not* visible to scopes nested inside it, so a `ClassScope` only
        contributes when it is the string's own scope. A `None` scope (no
        metadata) contributes nothing, so the string is left alone.
        """
        merged: dict[str, str] = {}
        current = scope
        own = True
        while current is not None:
            if own or not isinstance(current, ClassScope):
                for local, target in self._string_targets.get(current, {}).items():
                    merged.setdefault(local, target)
            if isinstance(current, GlobalScope):
                break
            own = False
            current = current.parent
        return merged

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
        if _imports.is_star(imp):
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
        if _interior_comments(imp):
            # The kept-names line is regenerated from text and the whole
            # statement is replaced, so any comment *inside* the import
            # would vanish. Same ruling as the line-level comment check
            # below: silently discarding an author's comment is worse than
            # declining to fix the file.
            self.blockers.append(
                (
                    self._line_of(imp),
                    "rewriting this import would discard a comment inside it",
                )
            )
            return
        if id(imp) in self._tc_ids and not self._future_annotations:
            self.blockers.append(
                (
                    self._line_of(imp),
                    "TYPE_CHECKING-gated import; rewriting it without "
                    "`from __future__ import annotations` risks NameError",
                )
            )
            return

        # Resolve each fixed name's rebinding status, and collect the
        # accesses that will actually be qualified, *before* choosing a
        # token: a nested scope that shadows the bound name at one of those
        # access sites must be avoided too, or the qualified reference
        # would silently resolve to the local instead of the new import
        # (fix-round-2 downward shadowing -- the mirror of fix-round-1's
        # ancestor check, but for scopes *below* where the binding lands).
        rewrites: list[tuple[str, str, list[BaseAssignment]]] = []
        extra_avoid: set[str] = set()
        for name, asname in fix:
            bound = asname or name
            if bound in self._del_names:
                # `del bound` reads as an access, not an assignment, so the
                # `others` check below cannot see it (see `_deleted_names`).
                self.blockers.append(
                    (
                        self._line_of(imp),
                        f"local '{bound}' is unbound with `del`; qualifying it would "
                        "delete an attribute of the imported module",
                    )
                )
                continue
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
            rewrites.append((name, bound, ours))
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
        for name, bound, ours in rewrites:
            for assignment in ours:
                for ref in assignment.references:
                    self.plan.name_repl[id(ref.node)] = cst.Attribute(
                        value=cst.Name(bind), attr=cst.Name(name)
                    )
            self._string_targets.setdefault(scope, {})[bound] = f"{bind}.{name}"
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
            # And it is unusable if the name is *rebound* (or deleted) at
            # module scope, which is the same `ours and others` test
            # `_plan_line` applies to the imported name, applied here to
            # the name we are about to reuse: `from os import path` +
            # `path = "/data"` must not turn `join(path, "x")` into
            # `path.join(path, "x")` (final review Critical 2). Only
            # `_allocate_token` avoided module-level names; this path
            # bypassed that entirely.
            shadowed = (
                existing in extra_avoid
                or existing in self._del_names
                or self._rebound_at_module_scope(scope, existing)
                or (
                    not isinstance(scope, GlobalScope)
                    and existing in self._ancestor_local_names(scope)
                )
            )
            if not shadowed:
                self._module_binding[key] = existing
                return existing, False

        bind = self._allocate_token(scope, parent, extra_avoid)
        self._module_binding[key] = bind
        return bind, True

    def _rebound_at_module_scope(self, scope: Scope, name: str) -> bool:
        """True when *name* has more than one assignment at module scope.

        Every entry in `_existing` is bound at module scope (`_build_existing`
        only records `GlobalScope` imports), so that is where a competing
        assignment would live. More than one assignment means libcst cannot
        say which one a reference resolves to -- exactly the condition that
        makes an imported name unrewritable -- so the binding is unusable.
        """
        return len(list(scope.globals[name])) > 1

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

    def leave_SimpleString(
        self, original: cst.SimpleString, updated: cst.SimpleString
    ) -> cst.BaseExpression:
        replacement = self.plan.string_repl.get(id(original))
        return updated if replacement is None else updated.with_changes(value=replacement)

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

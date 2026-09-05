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

* anything a ``[tool.cleanporter.skip]`` rule covers, and any binding whose
  name appears inside a skipped region -- see `cleanporter.skip`,
* a name nothing in the file reads: rewriting it removes a violation with no
  use site and deletes a binding that something outside the file may need
  (`_Fixer._unread_names`),
* an import whose *replacement* cannot be shown to bind the module it names,
  because the parent package's ``__init__`` binds that name to something else
  (`resolver.Resolver.replacement_unreachable`),
* imports inside an ``if TYPE_CHECKING:`` block, *unless* the file has
  ``from __future__ import annotations`` -- with it, annotations are strings
  at runtime, so both the import and any lazy string annotation mentioning
  the name are rewritten together; without it, rewriting risks ``NameError``,
* ``from x import *`` and unresolved/unknown names,
* names the resolver could not classify.

Multiple object names sharing one module reuse a single new binding; compliant
names in a mixed statement are kept in place.

Two phases inside one traversal, and the split is the safety model: on the way
down `_Fixer` fills a single `_Plan` -- node id to replacement, for statements,
references and lazy annotation strings -- and the ``leave_*`` hooks apply it on
the way out. Anything that makes a rename unprovable is recorded as a blocker
instead, and one is enough to throw the whole rewrite away: `leave_Module`
returns libcst's pristine original tree, which discards every edit made to its
children, and `fix_record` hands back the original source. Output that does not
re-parse is discarded the same way.
"""

from __future__ import annotations

import ast
import dataclasses
import re

import libcst as cst
from libcst import metadata

from cleanporter import analyze, config, model, resolver, skip

from . import _imports, guards


@dataclasses.dataclass
class _Plan:
    line_repl: dict[int, list[cst.BaseStatement]] = dataclasses.field(default_factory=dict)
    name_repl: dict[int, cst.BaseExpression] = dataclasses.field(default_factory=dict)
    string_repl: dict[int, str] = dataclasses.field(default_factory=dict)
    fixed: int = 0


def _render_alias(name: str, asname: str | None) -> str:
    return f"{name} as {asname}" if asname else name


def _type_checking_import_ids(tree: cst.Module) -> set[int]:
    """Ids of import nodes located inside an ``if TYPE_CHECKING:`` block.

    Both ``ImportFrom`` *and* plain ``Import``. The plain ones matter to
    `_build_existing`, which harvests already-imported modules to bind
    rewritten references through: a ``TYPE_CHECKING`` block is `GlobalScope`
    to libcst exactly like the module body, so ``import unittest`` in one
    looked like a perfectly good runtime binding. Reusing it emitted no new
    import at all and rewrote ``TestCase`` to ``unittest.TestCase``, which
    raises ``NameError`` the moment it runs. Found by running ``_pytest``'s
    own test suite against a rewritten copy.
    """
    ids: set[int] = set()
    aliases = _type_checking_aliases(tree)

    class V(cst.CSTVisitor):
        def visit_If(self, node: cst.If) -> None:
            # For `if TYPE_CHECKING:` the body is the type-checking-only half;
            # for `if not TYPE_CHECKING:` it is the `else`. Anything else is
            # ordinary runtime code.
            branch: cst.CSTNode | None = None
            if _type_checking_only(node.test, aliases):
                branch = node.body
            elif _is_negated_type_checking(node.test, aliases) and node.orelse is not None:
                branch = node.orelse
            if branch is None:
                return
            for imp in _collect_import_froms(branch):
                ids.add(id(imp))
            for plain in _collect_imports(branch):
                ids.add(id(plain))

    tree.visit(V())
    return ids


def _type_checking_aliases(tree: cst.Module) -> set[str]:
    """Local names bound to ``typing.TYPE_CHECKING``, including its own.

    ``from typing import TYPE_CHECKING as TC`` then ``if TC:`` is the same
    guard spelled differently, and matching the identifier alone missed it --
    the block's imports were harvested as runtime bindings.
    """
    names = {"TYPE_CHECKING"}

    class V(cst.CSTVisitor):
        def visit_ImportFrom(self, node: cst.ImportFrom) -> None:
            for name, asname, _alias in _imports.imported_names(node):
                if name == "TYPE_CHECKING" and asname:
                    names.add(asname)

    tree.visit(V())
    return names


def _is_type_checking_name(node: cst.BaseExpression, aliases: set[str]) -> bool:
    """One of *aliases*, or ``<anything>.TYPE_CHECKING``."""
    if isinstance(node, cst.Name):
        return node.value in aliases
    return isinstance(node, cst.Attribute) and node.attr.value == "TYPE_CHECKING"


def _is_negated_type_checking(test: cst.BaseExpression, aliases: set[str]) -> bool:
    """Exactly ``not TYPE_CHECKING`` -- the one guard that always runs.

    Only this precise shape. ``not (TYPE_CHECKING or X)`` depends on ``X``,
    and ``not DEBUG`` has nothing to do with type checking at all -- reading
    every ``not`` as this idiom declined perfectly ordinary files with a
    reason that was false about them.
    """
    return (
        isinstance(test, cst.UnaryOperation)
        and isinstance(test.operator, cst.Not)
        and _is_type_checking_name(test.expression, aliases)
    )


def _type_checking_only(test: cst.BaseExpression, aliases: set[str]) -> bool:
    """True when a block guarded by *test* may not run at run time.

    Matching only a bare ``if TYPE_CHECKING:`` was not enough. A test that
    merely *mentions* ``TYPE_CHECKING`` is equally not guaranteed --
    ``if TYPE_CHECKING or not install_lazy_importer():`` and
    ``if sys.version_info >= (3, 11) or TYPE_CHECKING:`` are both real, and
    both left an import that a rewrite then leaned on as though it always
    existed, producing ``NameError``.

    The one shape that *is* guaranteed is ``if not TYPE_CHECKING:``: at run
    time ``TYPE_CHECKING`` is false, so the body always executes and its
    imports are ordinary runtime bindings. Only that exact shape is excluded
    -- ``if not (TYPE_CHECKING or X):`` is a different expression whose value
    depends on ``X``, so it stays conservative.
    """
    if _is_negated_type_checking(test, aliases):
        return False
    mentioned = False

    class V(cst.CSTVisitor):
        def visit_Name(self, node: cst.Name) -> None:
            nonlocal mentioned
            if node.value in aliases:
                mentioned = True

    test.visit(V())
    return mentioned


def _collect_import_froms(node: cst.CSTNode) -> list[cst.ImportFrom]:
    found: list[cst.ImportFrom] = []

    class V(cst.CSTVisitor):
        def visit_ImportFrom(self, node: cst.ImportFrom) -> None:
            found.append(node)

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
        # libcst dispatches to this exact signature, so the node parameter
        # has to stay even though only its existence matters here.
        def visit_Comment(self, node: cst.Comment) -> None:  # noqa: ARG002
            nonlocal found
            found = True

    node.visit(V())
    return found


def _collect_imports(node: cst.CSTNode) -> list[cst.Import]:
    found: list[cst.Import] = []

    class V(cst.CSTVisitor):
        def visit_Import(self, node: cst.Import) -> None:
            found.append(node)

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


def _dunder_all_strings(tree: cst.Module) -> dict[int, cst.SimpleString]:
    """Every string literal inside an ``__all__`` declaration.

    ``__all__`` is a list of *names*, by definition, so a string in it is an
    identifier reference however it is spelled -- and it need not be a plain
    literal list. ``__all__ = "Widget helper".split()`` is a real idiom whose
    strings do not parse as Python, so content inspection alone reads them as
    prose and clears them; the rewrite then removes names the module still
    advertises. Like an annotation slot, this is a context the caller knows
    is code, so `guards.find_string_mentions` is told so directly.

    Covers assignment (``=``, ``+=``, annotated), the mutation idioms
    ``__all__.extend([...])`` / ``.append(...)``, and one level of
    indirection: ``__all__ = _EXPORTS`` also pulls in the strings assigned to
    ``_EXPORTS``. One level, not a general dataflow analysis -- a name is
    followed only when ``__all__`` is assigned it directly, which is as far as
    the idiom actually goes.
    """
    found: dict[int, cst.SimpleString] = {}
    assigned: dict[str, list[cst.BaseExpression]] = {}
    indirect: set[str] = set()

    def absorb(node: cst.CSTNode | None) -> None:
        if node is None:
            return
        for string in _collect_strings(node):
            found[id(string)] = string
        if isinstance(node, cst.Name):
            indirect.add(node.value)

    def targets_dunder_all(node: cst.BaseExpression) -> bool:
        return isinstance(node, cst.Name) and node.value == "__all__"

    class V(cst.CSTVisitor):
        def visit_Assign(self, node: cst.Assign) -> None:
            if any(targets_dunder_all(t.target) for t in node.targets):
                absorb(node.value)
            for target in node.targets:
                if isinstance(target.target, cst.Name):
                    assigned.setdefault(target.target.value, []).append(node.value)

        def visit_AugAssign(self, node: cst.AugAssign) -> None:
            if targets_dunder_all(node.target):
                absorb(node.value)

        def visit_AnnAssign(self, node: cst.AnnAssign) -> None:
            if targets_dunder_all(node.target):
                absorb(node.value)

        def visit_Call(self, node: cst.Call) -> None:
            func = node.func
            if isinstance(func, cst.Attribute) and targets_dunder_all(func.value):
                for arg in node.args:
                    absorb(arg.value)

    tree.visit(V())
    # `__all__ = _EXPORTS`: whatever built `_EXPORTS` is the name list.
    for name in indirect:
        for value in assigned.get(name, ()):
            for string in _collect_strings(value):
                found[id(string)] = string
    return found


def _collect_strings(node: cst.CSTNode) -> list[cst.SimpleString]:
    found: list[cst.SimpleString] = []

    class V(cst.CSTVisitor):
        def visit_SimpleString(self, node: cst.SimpleString) -> None:
            found.append(node)

    node.visit(V())
    return found


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


class _UnrenderableAnnotationError(Exception):
    """A (possibly nested) forward-reference string cannot be safely re-rendered.

    Either its content does not parse as an expression, or the rewritten
    result, re-wrapped in the original prefix/quote, does not round-trip
    back to exactly that result (fix-round-4: re-wrapping a decoded,
    unescaped render raw in the original quote character can change what
    the string means -- an escaped quote, a newline, or a quote landing
    adjacent to a triple-quote boundary -- so the wrapped text is
    re-parsed and compared rather than assumed safe).

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
    """Rename bare ``Name`` references to a rewritten local anywhere in a *parsed* type expression.

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
      singly-stringified one. Its failure -- `_UnrenderableAnnotationError` --
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
            new_element = element
            if changed:
                changed_any = True
                new_element = element.with_changes(value=new_value)
            new_elements.append(new_element)
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
    """Re-parse *node*'s content as a type expression and rename it via `_rewrite_type_expr`.

    Returns ``None`` when the content parses fine but nothing in it needed
    renaming (e.g. it is a `Literal`/`Annotated` payload, or mentions the
    name only after a ``.``) -- a genuinely inert string, safe to leave
    exactly as it is.

    Raises `_UnrenderableAnnotationError` -- never caught here, only by the
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
        raise _UnrenderableAnnotationError("non-text string content (e.g. bytes)")
    try:
        parsed = cst.parse_expression(content)
    except cst.ParserSyntaxError as exc:
        raise _UnrenderableAnnotationError("content does not parse as an expression") from exc
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
        raise _UnrenderableAnnotationError("content carries trivia that re-rendering would drop")
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
        raise _UnrenderableAnnotationError("re-wrapped value does not parse") from exc
    if not isinstance(check, cst.SimpleString) or check.evaluated_value != rendered:
        raise _UnrenderableAnnotationError("re-wrapped value does not round-trip")
    return node.with_changes(value=new_value)


class _Fixer(cst.CSTTransformer):
    METADATA_DEPENDENCIES = (metadata.ScopeProvider, metadata.PositionProvider)

    def __init__(
        self, rec: analyze.FileRecord, resolver: resolver.Resolver, config: config.Config
    ) -> None:
        super().__init__()
        self._rec = rec
        self._resolver = resolver
        self._config = config
        self.plan = _Plan()
        self.blockers: list[guards.Hit] = []
        self._module_binding: dict[
            tuple[metadata.Scope, str], str
        ] = {}  # (scope, parent) -> bound token
        self._existing: dict[str, str] = {}  # already-imported module -> its name
        #: Names bound at module scope. Kept *live*: grows as `_binding_for`
        #: allocates new module-level tokens, so a later function scope's
        #: collision check sees them (fix-round-1 Critical 2).
        self._global_names: set[str] = set()
        #: Per-scope cache of `_local_names`, mutated in place by
        #: `_binding_for` for the same reason `_global_names` is live.
        self._scope_locals: dict[metadata.Scope, set[str]] = {}
        self._tc_ids: set[int] = set()
        #: Names appearing as a ``del`` target (see `_deleted_names`).
        self._del_names: set[str] = set()
        #: Local names this run would rewrite -- the input to every guard.
        self._fixed_locals: set[str] = set()
        #: Leaf names of this module's own submodules. Non-empty only when
        #: this file is a package ``__init__`` (a plain module has no
        #: children), which is exactly when a module-level name here is also
        #: an attribute of that package -- see `_allocate_token`.
        self._sibling_modules: frozenset[str] = (
            resolver.submodules(rec.qualname) if rec.qualname else frozenset()
        )
        self._future_annotations = False
        #: (binding scope) -> {local name -> "token.name"}, for lazy string
        #: annotations (Task 16). Keyed by scope, because a rename is only
        #: valid for annotation strings that can actually *see* that binding.
        self._string_targets: dict[metadata.Scope, dict[str, str]] = {}
        #: What `[tool.cleanporter.skip]` takes out of this file, computed by
        #: the record so `analyze` and this class cannot answer it differently.
        self._skipped = rec.skipped
        #: Names kept because nothing in this file reads them. Reported by
        #: `analyze.analyze_record`, which cannot work them out on its own.
        self.unread: set[str] = set()

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
            scope = self.get_metadata(metadata.ScopeProvider, imp, None)
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
        position = self.get_metadata(metadata.PositionProvider, node, None)
        return position.start.line if position is not None else 0

    def _run_guards(self, node: cst.Module) -> None:
        if not self._fixed_locals:
            return
        skip_ids = self._plan_annotation_strings(node)
        # Two contexts are *code by declaration*, whether or not their
        # contents parse: an annotation slot and an `__all__` list. That is
        # the difference between prose the string guard can now clear
        # (`"expected Type, got int"`) and a name it must still block
        # (`"Thing["` in an annotation, `"Widget helper".split()` in
        # `__all__`), which no amount of inspecting the content alone can
        # tell apart. Anything already rewritten by
        # `_plan_annotation_strings` is in `skip_ids` and never reaches the
        # word match, so passing the whole annotation set here is exactly
        # "the annotation strings we could not rewrite".
        strict_ids = frozenset(_annotation_strings(node)) | frozenset(_dunder_all_strings(node))
        self.blockers.extend(
            guards.find_string_mentions(
                node,
                self._fixed_locals,
                self._line_of,
                skip_ids=skip_ids,
                strict_ids=strict_ids,
            )
        )
        self.blockers.extend(
            guards.find_scope_declarations(node, self._fixed_locals, self._line_of)
        )
        self.blockers.extend(guards.find_match_captures(node, self._fixed_locals, self._line_of))

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
            if self._skipped.covers(self._line_of(string_node)) is not None:
                # Belt to the pin's braces. `skip._names_in` already harvests
                # the names a string in a skipped region refers to, so this
                # should be unreachable -- but that harvest reads content that
                # parses, and is documented as incomplete. The promise that
                # nothing inside a region is edited is worth enforcing where
                # the edit is actually made, not only where it is predicted.
                continue
            # Only renames the *enclosing scope of this string* can actually
            # see. `plan.name_repl` has always been scope-aware; this path
            # was not, and a flat table let a function-local alias be
            # written into a module-level annotation, naming a binding that
            # does not exist there (final review Critical 4). An empty
            # target set leaves the string untouched, which is the safe
            # direction: it then stays visible to the string-mention guard.
            targets = self._targets_visible_from(
                self.get_metadata(metadata.ScopeProvider, string_node, None)
            )
            if not targets:
                continue
            # This is the one call site allowed to swallow
            # `_UnrenderableAnnotationError`: it means *this* candidate string
            # (possibly via a nested one, arbitrarily deep) could not be
            # safely classified or re-rendered at all, so it is left
            # exactly as it is, for the ordinary string-mention guard to
            # judge on its own.
            try:
                rewritten = _rewrite_string_content(string_node, targets)
            except _UnrenderableAnnotationError:
                continue
            if rewritten is not None:
                self.plan.string_repl[ident] = rewritten.value
        return frozenset(self.plan.string_repl)

    def _targets_visible_from(self, scope: metadata.Scope | None) -> dict[str, str]:
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
            if own or not isinstance(current, metadata.ClassScope):
                for local, target in self._string_targets.get(current, {}).items():
                    merged.setdefault(local, target)
            if isinstance(current, metadata.GlobalScope):
                break
            own = False
            current = current.parent
        return merged

    def _build_existing(self, node: cst.Module) -> None:
        """Map already-imported modules to the simple name they are bound to."""
        for _line, imp in self._import_lines(node):
            if _imports.is_star(imp) or id(imp) in self._tc_ids:
                continue
            scope = self.get_metadata(metadata.ScopeProvider, imp, None)
            if not isinstance(scope, metadata.GlobalScope):
                continue
            parent = _imports.resolve_parent(imp, self._rec.base_pkg)
            if parent is None:
                continue
            for name, asname, _alias in _imports.imported_names(imp):
                if self._resolver.is_module(parent, name) is True:
                    self._existing[f"{parent}.{name}"] = asname or name
        # plain ``import a`` / ``import a as z`` (top-level modules only)
        for plain in _collect_imports(node):
            scope = self.get_metadata(metadata.ScopeProvider, plain, None)
            # `_tc_ids` covers plain imports too: one inside `if TYPE_CHECKING:`
            # is GlobalScope-scoped like any other, but does not exist at
            # runtime, so it is not a binding anything may be rewritten
            # through.
            if not isinstance(scope, metadata.GlobalScope) or id(plain) in self._tc_ids:
                continue
            for alias in plain.names:
                mod = _imports.dotted(alias.name)
                # `AsName.name` is `Name | Tuple | List` because the node is
                # shared with `with`/`except` as-clauses; an import as-clause
                # is always a plain `Name`. Same narrowing as
                # `_imports.imported_names`.
                as_node = alias.asname.name if alias.asname else None
                bound = as_node.value if isinstance(as_node, cst.Name) else None
                if bound is not None:
                    self._existing[mod] = bound
                elif "." not in mod:
                    self._existing[mod] = mod

    def _import_lines(
        self, node: cst.Module
    ) -> list[tuple[cst.SimpleStatementLine, cst.ImportFrom]]:
        pairs: list[tuple[cst.SimpleStatementLine, cst.ImportFrom]] = []

        class V(cst.CSTVisitor):
            def visit_SimpleStatementLine(self, node: cst.SimpleStatementLine) -> None:
                if len(node.body) == 1 and isinstance(node.body[0], cst.ImportFrom):
                    pairs.append((node, node.body[0]))

        node.visit(V())
        return pairs

    def _partition(
        self, imp: cst.ImportFrom, parent: str, scope: metadata.Scope
    ) -> tuple[list[str], list[tuple[str, str | None]]]:
        """Split this import's names into the ones kept and the ones rewritten.

        Kept: names a `[tool.cleanporter.skip]` rule covers or pins, exempt
        names, modules, anything the resolver could not classify, re-exports,
        names nothing in this file reads, and -- for the whole line at once --
        every name whose replacement import cannot be shown to bind the module
        it names (`resolver.Resolver.replacement_unreachable`).

        A re-export -- declared as
        ``S as S``, or inferred from another analysed file importing ``S``
        from *this* module -- means this very import line is what makes
        ``<this module>.S`` exist for somebody else, so rewriting it would
        delete an attribute they read. Kept in place rather than blocking the
        file, exactly as an unresolved name is, so the file's other rewrites
        still happen.

        The order of the chain is the reported order: a skip comes first
        because it is the author overriding everything the tool could work
        out, the two line-wide reasons (that skip and an unreachable
        replacement) precede the per-name ones, and `_note_unread` comes
        *last* because reaching it means every other reason to keep the name
        was already false -- which is what makes the set it records exactly
        the set `analyze` would otherwise report as `CP001`.
        """
        keep: list[str] = []
        fix: list[tuple[str, str | None]] = []
        unreachable = self._resolver.replacement_unreachable(parent) is not None
        skipped_line = self._skipped.covers(self._line_of(imp)) is not None
        never_read = self._unread_names(imp, scope)
        for name, asname, _alias in _imports.imported_names(imp):
            bound = asname or name
            if (
                skipped_line
                or unreachable
                or self._skipped.pin(bound) is not None
                or self._config.is_exempt(parent, name)
                or _imports.is_explicit_reexport(name, asname)
                or (
                    self._rec.qualname and self._resolver.is_load_bearing(self._rec.qualname, bound)
                )
                or self._resolver.is_module(parent, name) is not False
                or self._note_unread(bound, never_read)
            ):
                keep.append(_render_alias(name, asname))
            else:
                fix.append((name, asname))
        return keep, fix

    def _unread_names(self, imp: cst.ImportFrom, scope: metadata.Scope) -> frozenset[str]:
        """Names *imp* binds that nothing in this file reads.

        Rewriting one of those removes a violation with no use site, and its
        only other effect is to delete the binding -- which is exactly how a
        pytest fixture pulled in by name disappears. Nothing to gain, a
        namespace to lose, so the import is kept.

        libcst's scope analysis is what makes this answerable at all. The
        cheap version -- "does this identifier appear anywhere else in the
        file" -- gets ``def test_x(extension_source_example)`` wrong twice
        over: the parameter is a `Name`, and so is every read of it in the
        body, yet both belong to the *parameter's* binding and neither reaches
        the import.
        """
        out: set[str] = set()
        for name, asname, _alias in _imports.imported_names(imp):
            bound = asname or name
            ours = [a for a in scope[bound] if getattr(a, "node", None) is imp]
            if ours and not any(a.references for a in ours):
                out.add(bound)
        return frozenset(out)

    def _note_unread(self, bound: str, never_read: frozenset[str]) -> bool:
        """Last link in `_partition`'s keep chain; records what it keeps."""
        if bound not in never_read:
            return False
        self.unread.add(bound)
        return True

    def _plan_line(self, line: cst.SimpleStatementLine, imp: cst.ImportFrom) -> None:
        if _imports.is_star(imp):
            return
        scope = self.get_metadata(metadata.ScopeProvider, imp, None)
        if scope is None:
            return
        parent = _imports.resolve_parent(imp, self._rec.base_pkg)
        if parent is None:
            return

        keep, fix = self._partition(imp, parent, scope)
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
                    (
                        "TYPE_CHECKING-gated import; rewriting it without "
                        "`from __future__ import annotations` risks NameError"
                    ),
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
        rewrites: list[tuple[str, str, list[metadata.BaseAssignment]]] = []
        extra_avoid: set[str] = set()
        for name, asname in fix:
            bound = asname or name
            if bound in self._del_names:
                # `del bound` reads as an access, not an assignment, so the
                # `others` check below cannot see it (see `_deleted_names`).
                self.blockers.append(
                    (
                        self._line_of(imp),
                        (
                            f"local '{bound}' is unbound with `del`; qualifying it would "
                            "delete an attribute of the imported module"
                        ),
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
                    access_scope: metadata.Scope = ref.scope
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
                new_lines[-1] = last.with_changes(trailing_whitespace=line.trailing_whitespace)
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

    def _local_names(self, scope: metadata.Scope) -> set[str]:
        """Names assigned directly in *scope*, ignoring enclosing scopes.

        Cached and mutated in place: `_binding_for` adds each token it
        allocates in *scope* here, so a later lookup in the same scope sees
        it immediately, without waiting for a real AST assignment to back
        it (fix-round-1 Critical 2).
        """
        if scope not in self._scope_locals:
            self._scope_locals[scope] = {a.name for a in scope.assignments}
        return self._scope_locals[scope]

    def _ancestor_local_names(self, scope: metadata.Scope) -> set[str]:
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
        while not isinstance(current, metadata.GlobalScope):
            names |= self._local_names(current)
            current = current.parent
        return names

    def _names_in_scope(self, scope: metadata.Scope) -> set[str]:
        """Names a new binding in *scope* must not collide with.

        That is, names assigned in *scope* itself, in any enclosing
        function/class scope, or at module scope.
        """
        return self._ancestor_local_names(scope) | self._global_names

    def _shadow_names_between(
        self, access_scope: metadata.Scope, upper_scope: metadata.Scope
    ) -> set[str]:
        """Names assigned in *access_scope*, or strictly between it and *upper_scope*.

        The scopes are walked ``.parent`` upward.

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
        while current is not upper_scope and not isinstance(current, metadata.GlobalScope):
            names |= self._local_names(current)
            current = current.parent
        return names

    def _binding_for(
        self, scope: metadata.Scope, parent: str, extra_avoid: set[str]
    ) -> tuple[str, bool]:
        """Token to qualify *this line's* references through.

        Also reports whether a new import statement must be emitted for it.

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
            # A binding the *author* already made under a sibling submodule's
            # name is no more durable than one this run would allocate there
            # (`_submodule_slots`): the first `import P.<name>` replaces it.
            # It was harmless while nothing depended on it; qualifying
            # references through it is what would make it load-bearing. Fall
            # through to a fresh alias instead -- their import stays, this
            # run just does not lean on it.
            shadowed = (
                existing in extra_avoid
                or existing in self._del_names
                or existing in self._submodule_slots(parent)
                or self._rebound_at_module_scope(scope, existing)
                or (
                    not isinstance(scope, metadata.GlobalScope)
                    and existing in self._ancestor_local_names(scope)
                )
            )
            if not shadowed:
                self._module_binding[key] = existing
                return existing, False

        bind = self._allocate_token(scope, parent, extra_avoid)
        self._module_binding[key] = bind
        return bind, True

    def _submodule_slots(self, parent: str) -> set[str]:
        """Names a module-scope binding of *parent* must not occupy.

        Inside ``P/__init__.py`` a module-level name *is* the attribute
        ``P.<name>``, so a binding named after one of ``P``'s own submodules
        sits in that submodule's slot -- and the first ``import P.<name>``
        anywhere, in any file, replaces it. Whatever this file bound there is
        then gone, and its own qualified references start resolving against
        the submodule instead. Nothing raises where the mistake is.

        Binding a submodule under *its own* name is the one case with nothing
        to collide: the global and the attribute hold the same object, which
        is what the import system puts there anyway. Excluding it keeps
        ``from pkg import serialization`` inside ``pkg/__init__.py`` spelled
        without a needless alias.

        Empty for every file that is not a package ``__init__``, since a plain
        module has no submodules and its globals are nobody's attributes.
        """
        return {
            sibling
            for sibling in self._sibling_modules
            if f"{self._rec.qualname}.{sibling}" != parent
        }

    def _rebound_at_module_scope(self, scope: metadata.Scope, name: str) -> bool:
        """True when *name* has more than one assignment at module scope.

        Every entry in `_existing` is bound at module scope (`_build_existing`
        only records `GlobalScope` imports), so that is where a competing
        assignment would live. More than one assignment means libcst cannot
        say which one a reference resolves to -- exactly the condition that
        makes an imported name unrewritable -- so the binding is unusable.
        """
        return len(list(scope.globals[name])) > 1

    def _allocate_token(self, scope: metadata.Scope, parent: str, extra_avoid: set[str]) -> str:
        """Pick a fresh, collision-free token for a new import of *parent* in *scope*.

        Records the choice in the live name set(s) `_names_in_scope` reads
        (`_global_names` for `GlobalScope`, else `_local_names(scope)`), so
        a later allocation in this scope -- or one that sees it as an
        ancestor -- avoids it too. Does *not* touch `_module_binding`: that
        is the caller's job, since a fix-round-3 collision reallocation
        deliberately leaves the original memoized entry alone.

        A module-scope token in ``P/__init__.py`` must additionally avoid the
        names of ``P``'s *own submodules* (`_sibling_modules`), because there
        a global is not merely a name: it is the attribute ``P.<name>``.
        Rewriting ``from kombu.serialization import loads`` to ``from kombu
        import serialization`` inside ``celery/security/__init__.py`` put
        ``kombu.serialization`` in the slot belonging to
        ``celery.security.serialization``, and nothing complains until the
        first ``import celery.security.serialization`` anywhere replaces the
        attribute -- after which this file's own ``serialization.loads``
        resolves against the wrong module. It also made the very next
        rewritten line read that slot instead of importing the submodule
        (`resolver.Resolver.replacement_unreachable`), which is how the corpus
        found it.

        The avoidance applies at `GlobalScope` only: a function-local or class
        body name is not an attribute of the module.
        """
        token = parent.rsplit(".", 1)[-1]
        taken = self._names_in_scope(scope) | extra_avoid
        if isinstance(scope, metadata.GlobalScope):
            taken = taken | self._submodule_slots(parent)
        bind = token
        counter = 2
        while bind in taken:
            bind = f"{token}_{counter}"
            counter += 1
        if isinstance(scope, metadata.GlobalScope):
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
    def leave_SimpleStatementLine(
        self,
        original_node: cst.SimpleStatementLine,
        updated_node: cst.SimpleStatementLine,
    ) -> cst.BaseStatement | cst.FlattenSentinel[cst.BaseStatement] | cst.RemovalSentinel:
        repl = self.plan.line_repl.get(id(original_node))
        if repl is not None:
            return cst.FlattenSentinel(repl) if repl else cst.RemovalSentinel.REMOVE
        return updated_node

    def leave_Name(self, original_node: cst.Name, updated_node: cst.Name) -> cst.BaseExpression:
        repl = self.plan.name_repl.get(id(original_node))
        return repl if repl is not None else updated_node

    def leave_SimpleString(
        self, original_node: cst.SimpleString, updated_node: cst.SimpleString
    ) -> cst.BaseExpression:
        replacement = self.plan.string_repl.get(id(original_node))
        return updated_node if replacement is None else updated_node.with_changes(value=replacement)

    def leave_Module(self, original_node: cst.Module, updated_node: cst.Module) -> cst.Module:
        # All-or-nothing. libcst hands us the pristine original tree, so
        # returning it discards every edit made to the children.
        return original_node if self.blockers else updated_node


@dataclasses.dataclass
class FixOutcome:
    """Result of attempting to fix one file.

    ``source`` is always a string -- the resulting source when ``status`` is
    ``"fixed"``, otherwise the unchanged input -- so callers never need to
    branch on ``None``.
    """

    status: str  # "fixed" | "clean" | "skipped" | "error"
    source: str
    blockers: list[model.Finding] = dataclasses.field(default_factory=list)
    fixed: int = 0
    #: Names kept because nothing in the file reads them; see
    #: `_Fixer._unread_names`. Passed back to `analyze.analyze_record` so the
    #: post-fix report says why they were left alone. Names, not
    #: ``(scope, name)`` pairs: a file that imports the same name at module
    #: scope and inside a function, one read and one not, gets the explanation
    #: on both. That costs a slightly wrong *message* and never a wrong
    #: rewrite, since the fixer itself decides per scope.
    unread: frozenset[str] = frozenset()


def fix_record(
    rec: analyze.FileRecord, resolver: resolver.Resolver, config: config.Config
) -> FixOutcome:
    """Rewrite one file, or leave it exactly as it was and say why."""
    if rec.skipped.whole_file:
        # Nothing to weigh: a `skip` rule took the file whole. Bail before the
        # metadata resolution, which is the expensive part.
        return FixOutcome("clean", rec.source)
    wrapper = cst.MetadataWrapper(rec.tree, unsafe_skip_copy=True)
    fixer = _Fixer(rec, resolver, config)
    new_source = wrapper.visit(fixer).code

    if fixer.blockers:
        return FixOutcome(
            "skipped",
            rec.source,
            [
                model.Finding(rec.path, line, 0, "?", "?", model.Status.SKIPPED, reason)
                for line, reason in sorted(set(fixer.blockers))
            ],
            unread=frozenset(fixer.unread),
        )
    if not fixer.plan.fixed or new_source == rec.source:
        return FixOutcome("clean", rec.source, unread=frozenset(fixer.unread))

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
                model.Finding(
                    rec.path,
                    getattr(exc, "lineno", None) or 0,
                    0,
                    "?",
                    "?",
                    model.Status.SKIPPED,
                    "internal error: the rewrite did not parse; reverted",
                )
            ],
            unread=frozenset(fixer.unread),
        )

    stale = _region_the_rewrite_created(rec, new_source, config)
    if stale is not None:
        return FixOutcome("skipped", rec.source, [stale], unread=frozenset(fixer.unread))

    return FixOutcome("fixed", new_source, [], fixer.plan.fixed, frozenset(fixer.unread))


def _source_lines(source: str) -> list[str]:
    r"""*source* split the way libCST counts lines, so an index means the same.

    ``str.splitlines`` also breaks on ``\x0b``, ``\x0c``, ``\x1c``-``\x1e``,
    ``\x85``, ``\u2028`` and ``\u2029``; libCST breaks on ``\n``, ``\r\n``
    and ``\r`` alone. A form feed inside a string literal was enough to shift
    every line number after it, so a region's text was read from the wrong
    place -- reported as a self-created region that was nothing of the kind,
    and, with the shift the other way, capable of missing a real one.
    """
    return re.split(r"\r\n|\r|\n", source)


def _first_difference(before: list[str], after: list[str]) -> int:
    """1-based line where two versions of a file first differ (0 if never).

    The line a self-created region *starts* at is a line in the rewritten
    output, and the rewrite is about to be thrown away -- pointing a reader
    at it points them at a file that will not exist. The first line the fix
    would have changed is in the file they actually have, and it is where the
    trouble begins.
    """
    for index, (old, new) in enumerate(zip(before, after, strict=False), start=1):
        if old != new:
            return index
    return min(len(before), len(after)) + 1 if before != after else 0


def _region_the_rewrite_created(
    rec: analyze.FileRecord, new_source: str, config: config.Config
) -> model.Finding | None:
    r"""A `skip` region that only exists *because of* this rewrite, or None.

    A rule can match code the fixer is about to write. ``decorator =
    'gtx\\.field_operator'`` matches nothing in a file that spells the import
    ``from gt4py.next import field_operator`` and writes ``@field_operator``
    -- so the body is not skipped, the fixer rewrites it *and* the decorator,
    and the resulting ``@gtx.field_operator`` is a region the config declares
    off-limits, covering code that has just been edited. The user's rule is
    then honoured on every subsequent run over code it never got to protect,
    which is the one failure mode this feature must not have.

    So: recompute the regions on the output, and require every one of them to
    appear in the input verbatim. Line numbers move under a rewrite, so the
    comparison is on the block of text, not on its position. Declining is the
    right answer rather than re-running the fixer, both because it is the
    conservative one and because a fixer that chased its own configuration to
    a fixed point could not promise to terminate.
    """
    if not config.skip:
        return None
    tree = cst.parse_module(new_source)
    positions = metadata.MetadataWrapper(tree, unsafe_skip_copy=True).resolve(
        metadata.PositionProvider
    )
    # Spans only: which regions the output has. What they would pin is a
    # second tree walk this never reads.
    after = skip.region_spans(
        tree,
        positions,
        config.skip,
        skip.file_candidates(rec.path, config.root),
        rec.qualname,
    )
    before_lines = _source_lines(rec.source)
    new_lines = _source_lines(new_source)
    # Where each line of the input occurs, so a region is looked up by its
    # first line rather than slid along the whole file.
    starts: dict[str, list[int]] = {}
    for offset, text in enumerate(before_lines):
        starts.setdefault(text, []).append(offset)
    for start, end, rule in after.spans:
        block = new_lines[start - 1 : end]
        if not block:
            continue
        if not any(
            before_lines[offset : offset + len(block)] == block
            for offset in starts.get(block[0], ())
        ):
            return model.Finding(
                rec.path,
                _first_difference(before_lines, new_lines),
                0,
                "?",
                "?",
                model.Status.SKIPPED,
                f"the rewrite would edit code that {rule.describe()} then covers; "
                "the rule matches the rewritten spelling but not the original, so "
                "applying it would skip this region only after changing it",
            )
    return None

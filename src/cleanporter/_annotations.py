"""String literals the fixer must treat as code: lazy annotations and ``__all__``.

Two jobs, both about strings whose contents name identifiers:

* *finding* them -- `annotation_strings` for strings in a genuine annotation
  slot, `dunder_all_strings` for the names a module advertises in
  ``__all__`` -- so `guards.find_string_mentions` can judge them as code
  rather than prose;
* *rewriting* a lazy annotation -- `rewrite_string_content` parses the
  string's content and renames references structurally, never textually,
  and raises `UnrenderableAnnotationError` for anything it cannot classify or
  re-render exactly, so the string is left for the string-mention guard to
  block instead of being guessed at.

Rewriting is only safe under ``from __future__ import annotations``, which is
the fixer's precondition for asking: there, annotation strings are never
evaluated at run time.
"""

from __future__ import annotations

import libcst as cst

from . import _nodes

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


def dunder_all_strings(tree: cst.Module) -> dict[int, cst.SimpleString]:
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
        for string in _nodes.strings(node):
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
            for string in _nodes.strings(value):
                found[id(string)] = string
    return found


def annotation_strings(tree: cst.Module) -> dict[int, cst.SimpleString]:
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


class UnrenderableAnnotationError(Exception):
    """A (possibly nested) forward-reference string cannot be safely re-rendered.

    Either its content does not parse as an expression, or the rewritten
    result, re-wrapped in the original prefix/quote, does not round-trip
    back to exactly that result (fix-round-4: re-wrapping a decoded,
    unescaped render raw in the original quote character can change what
    the string means -- an escaped quote, a newline, or a quote landing
    adjacent to a triple-quote boundary -- so the wrapped text is
    re-parsed and compared rather than assumed safe).

    Always caught at the single outermost `rewrite_string_content` call --
    the one `rewrite._Fixer._plan_annotation_strings` makes directly --
    never partway through a nested structure. A failure anywhere inside a
    candidate string (at any depth) must abort that *entire* candidate's
    rewrite rather than record a partially-rewritten value with an
    unclassifiable leftover fragment nobody checked (fix-round-3 New 2: a
    `dict['Thing', 'Thing[']` where the second element fails to parse must
    not let the first element's successful rename slip into
    `plan.string_repl` on its own -- the surviving, untouched `'Thing['`
    would then be hidden from the string-mention guard by the very
    `skip_ids` entry that rename produced).
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
      element is walked -- the same opacity rules ``annotation_strings``
      applies at the CST level, reapplied here because a *parsed* string's
      content can contain a ``Literal``/``Annotated`` subscript that was
      never visible as a CST node to that first pass (fix-round-2 Critical
      2: a fully-stringified annotation like ``"Literal['Thing']"`` has no
      ``Subscript`` node until its content is parsed).
    * A nested ``SimpleString`` sitting in an otherwise genuine type
      position (e.g. the ``'Thing'`` in ``list['Thing']``, itself possibly
      reached only after parsing an outer string) is a forward reference in
      its own right and is recursed into via `rewrite_string_content`, so
      a doubly-stringified annotation is handled exactly like a
      singly-stringified one. Its failure -- `UnrenderableAnnotationError` --
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
        # ``token.name`` qualifies a rewritten from-import; a bare name is a
        # renamed module binding (`rewrite._Fixer._plan_rename`).
        bind, _dot, symbol = target.partition(".")
        renamed: cst.BaseExpression = (
            cst.Attribute(value=cst.Name(bind), attr=cst.Name(symbol)) if symbol else cst.Name(bind)
        )
        return renamed, True

    if isinstance(expr, cst.Attribute):
        new_value, changed = _rewrite_type_expr(expr.value, targets)
        return (expr.with_changes(value=new_value), True) if changed else (expr, False)

    if isinstance(expr, cst.SimpleString):
        rewritten = rewrite_string_content(expr, targets)
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


def rewrite_string_content(
    node: cst.SimpleString, targets: dict[str, str]
) -> cst.SimpleString | None:
    """Re-parse *node*'s content as a type expression and rename it via `_rewrite_type_expr`.

    Returns ``None`` when the content parses fine but nothing in it needed
    renaming (e.g. it is a `Literal`/`Annotated` payload, or mentions the
    name only after a ``.``) -- a genuinely inert string, safe to leave
    exactly as it is.

    Raises `UnrenderableAnnotationError` -- never caught here, only by the
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
        raise UnrenderableAnnotationError("non-text string content (e.g. bytes)")
    try:
        parsed = cst.parse_expression(content)
    except cst.ParserSyntaxError as exc:
        raise UnrenderableAnnotationError("content does not parse as an expression") from exc
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
        raise UnrenderableAnnotationError("content carries trivia that re-rendering would drop")
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
        raise UnrenderableAnnotationError("re-wrapped value does not parse") from exc
    if not isinstance(check, cst.SimpleString) or check.evaluated_value != rendered:
        raise UnrenderableAnnotationError("re-wrapped value does not round-trip")
    return node.with_changes(value=new_value)

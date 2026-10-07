"""Top-level name bindings of a module, read with ``ast``.

Two questions are answered here, both by parsing only -- nothing is imported:

* which names a package ``__init__`` binds at top level, so a binding that
  shadows a real submodule on disk can be reported ambiguous rather than
  guessed at (`top_level_bindings`), and
* which of a module's top-level names it merely *re-exports* -- binds by
  importing them from somewhere else rather than defining them
  (`import_bound_names`), and
* what a module's namespace is *made of*, so the first-party layer can claim
  ``PARENT.NAME`` is an object only on positive evidence that ``PARENT``
  binds it (`namespace`).
"""

from __future__ import annotations

import ast
import dataclasses
import functools
import pathlib
from collections.abc import Iterator, Mapping


def _declared_name(
    stmt: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.TypeAlias,
) -> str:
    """The name *stmt* declares itself under: ``def``, ``class`` and PEP 695 ``type``.

    All three bind that name at run time to an object -- a function, the
    class, a ``typing.TypeAliasType`` -- which is why they are read alike
    here. The ``def``/``class`` grammar carries the name as a ``str``;
    ``ast.TypeAlias`` carries it as a plain ``ast.Name``, the only target
    its grammar allows.
    """
    return stmt.name.id if isinstance(stmt, ast.TypeAlias) else stmt.name


def _collect(
    body: list[ast.stmt],
    defined: set[str],
    imported: set[str],
    submodule_imports: set[str],
    self_package: str | None = None,
) -> None:
    """Absorb *body*'s top-level bindings, split by *how* each one was bound.

    ``defined`` gets names a statement creates here (a ``def``, a ``class``,
    a PEP 695 ``type`` alias, an assignment); ``imported`` gets names an
    ``import`` brings in. The two are collected separately rather than derived
    from one another because a name can be both -- ``try: from x import y``
    with a fallback ``def y`` in the ``except`` is bound either way, so it
    survives a rewrite of the import and is not a re-export.
    """
    for stmt in body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.TypeAlias)):
            defined.add(_declared_name(stmt))
        elif isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                defined.update(n.id for n in ast.walk(target) if isinstance(n, ast.Name))
        elif isinstance(stmt, ast.AnnAssign):
            # A bare ``mod: Type`` annotation (no ``=``) only populates
            # ``__annotations__``; it does not bind ``mod`` at runtime.
            if stmt.value is not None and isinstance(stmt.target, ast.Name):
                defined.add(stmt.target.id)
        elif isinstance(stmt, ast.AugAssign):
            if isinstance(stmt.target, ast.Name):
                defined.add(stmt.target.id)
        elif isinstance(stmt, ast.Import):
            for alias in stmt.names:
                imported.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(stmt, ast.ImportFrom):
            for alias in stmt.names:
                if alias.name == "*":
                    continue
                bound = alias.asname or alias.name
                # ``from . import mod`` binds the submodule itself, so it is
                # not a shadowing binding -- but only at level 1 (the
                # package's own submodule) and only when unaliased: ``from .
                # import mod as m`` binds ``m``, which is *not* auto-populated
                # as an attribute, so it can still shadow a real ``pkg/m.py``.
                # ``from .. import Y`` (level 2+) names a submodule of an
                # *ancestor* package, not of this one, so it is a genuine
                # (possibly shadowing) binding here too.
                #
                # ``from pkg import mod`` *inside* ``pkg/__init__.py`` is the
                # same statement spelled absolutely -- same package, same
                # fallback to importing the submodule when the attribute is
                # absent, same module bound -- and django's
                # ``db/models/__init__.py`` writes it that way. Reading only
                # the relative spelling called every consumer of
                # ``django.db.models.signals`` ambiguous. It is only the same
                # statement when *self_package* really is this file's package,
                # which is why the caller has to say so; a binding made
                # earlier in the file still lands in ``defined``/``imported``
                # and keeps the name ambiguous, exactly as in the relative
                # case.
                #
                # The name is kept out of *imported* rather than subtracted
                # from it afterwards, so that a second, competing binding --
                # ``from . import mod`` followed by ``mod = wrap(mod)``, or by
                # a ``from elsewhere import mod`` -- puts it back. Subtracting
                # at the end discarded those too, and they are exactly the
                # bindings that win the attribute lookup this discount
                # assumes nothing wins.
                if alias.asname is None and (
                    (stmt.level == 1 and stmt.module is None)
                    or (
                        stmt.level == 0 and self_package is not None and stmt.module == self_package
                    )
                ):
                    submodule_imports.add(bound)
                else:
                    imported.add(bound)
        elif isinstance(stmt, ast.If):
            _collect(stmt.body, defined, imported, submodule_imports, self_package)
            _collect(stmt.orelse, defined, imported, submodule_imports, self_package)
        elif isinstance(stmt, (ast.For, ast.AsyncFor)):
            defined.update(n.id for n in ast.walk(stmt.target) if isinstance(n, ast.Name))
            _collect(stmt.body, defined, imported, submodule_imports, self_package)
            _collect(stmt.orelse, defined, imported, submodule_imports, self_package)
        elif isinstance(stmt, ast.While):
            _collect(stmt.body, defined, imported, submodule_imports, self_package)
            _collect(stmt.orelse, defined, imported, submodule_imports, self_package)
        elif isinstance(stmt, ast.Match):
            # Capture-pattern bindings (e.g. ``case Foo(x=y):``) are not
            # extracted -- a module-level ``match`` in an ``__init__.py``
            # binding a name that shadows a submodule is vanishingly rare,
            # and this limit is stated rather than silently assumed.
            for case in stmt.cases:
                _collect(case.body, defined, imported, submodule_imports, self_package)
        elif isinstance(stmt, (ast.Try, ast.TryStar)):
            _collect(stmt.body, defined, imported, submodule_imports, self_package)
            _collect(stmt.orelse, defined, imported, submodule_imports, self_package)
            _collect(stmt.finalbody, defined, imported, submodule_imports, self_package)
            for handler in stmt.handlers:
                _collect(handler.body, defined, imported, submodule_imports, self_package)
        elif isinstance(stmt, (ast.With, ast.AsyncWith)):
            _collect(stmt.body, defined, imported, submodule_imports, self_package)


def _parse(path: str) -> ast.Module | None:
    """*path* parsed, or ``None`` when it cannot be read or parsed.

    Parsed from *bytes*, so that ``ast`` applies the source's own encoding --
    a PEP 263 ``# -*- coding: latin-1 -*-`` cookie or a UTF-8 BOM -- exactly as
    the interpreter would. Decoding as UTF-8 up front made every such file
    look like it bound nothing at all.

    ``None`` is not "binds nothing": every caller has to treat it as the
    answer that cannot hurt, because nothing about the file is known.
    """
    try:
        return ast.parse(pathlib.Path(path).read_bytes())
    except (OSError, SyntaxError, ValueError):
        return None


@functools.lru_cache(maxsize=1024)
def top_level_bindings(path: str, package: str) -> frozenset[str] | None:
    """Names bound at the top level of *path*, excluding self-submodule imports.

    *package* is the dotted name of the package *path* is the ``__init__``
    of, so that ``from <package> import mod`` can be recognised as the
    absolute spelling of ``from . import mod`` and discounted with it.

    Cached on ``(path, package)``, with no mtime check. That is only safe because
    nothing in a single run rewrites a scanned ``__init__.py``'s plain
    assignments before this cache is consulted again -- the fixer only
    rewrites ``ImportFrom`` nodes, and the one self-referential pattern it
    could otherwise introduce is already excluded. This is an assumption
    about the fixer's current narrow scope, not an enforced invariant.

    ``None`` when the file cannot be read or parsed (see `_parse`).
    """
    tree = _parse(path)
    if tree is None:
        return None
    defined: set[str] = set()
    imported: set[str] = set()
    submodule_imports: set[str] = set()
    _collect(tree.body, defined, imported, submodule_imports, package)
    return frozenset(defined | imported)


@functools.lru_cache(maxsize=1024)
def import_bound_names(path: str) -> frozenset[str] | None:
    """Top-level names *path* binds only by importing them -- its re-exports.

    A name a module *imports* rather than *defines* is an attribute of that
    module only for as long as the import is written the way it is. That
    matters here because the fixer may rewrite that very module in the same
    run: turning ``from .display import dump`` in ``libcst/tool.py`` into
    ``from libcst import display`` is a correct fix for that file, and it
    deletes ``libcst.tool.dump``, which some *other* file was importing.
    Both rewrites are right on their own and wrong together.

    Only names bound purely by an import count. A name that is also defined
    in the module (imported under a ``try`` and given a fallback definition
    in the ``except``, say) survives the rewrite, so it is not reported here.

    Cached on ``path`` alone, with the same no-mtime-check caveat as
    `top_level_bindings` (which also keys on the package name; this one has
    no package to key on).

    No *package* is passed to `_collect`, and the asymmetry is deliberate:
    the question here is not "does this binding shadow the submodule" but
    "does this attribute exist only because of this import statement", and
    for a package importing its own submodule absolutely the answer to the
    second one is still yes.

    ``None`` when the file cannot be read or parsed (see `_parse`).
    """
    tree = _parse(path)
    if tree is None:
        return None
    defined: set[str] = set()
    imported: set[str] = set()
    submodule_imports: set[str] = set()
    _collect(tree.body, defined, imported, submodule_imports)
    return frozenset(imported - defined - submodule_imports)


#: Where a ``from M import X [as NAME]`` gets ``NAME`` from: ``(level, M, X)``,
#: with ``M`` ``None`` for ``from . import X``.
Origin = tuple[int, str | None, str]


@dataclasses.dataclass(frozen=True)
class Namespace:
    """What a module's top-level namespace is built from, as far as a parse can tell.

    Every binding here is read with `_collect`'s descent, so it counts wherever
    `top_level_bindings` would count it -- inside ``if`` / ``try`` / ``for`` /
    ``while`` / ``with`` / ``match`` bodies included -- and a name bound by
    several statements (``try: from ._speedups import f`` / ``except: def
    f``) appears under each of them.
    """

    #: Names a statement creates here: ``def``, ``class``, a PEP 695 ``type``
    #: alias, an assignment, a loop target.
    defined: frozenset[str]
    #: Names bound by a plain ``import X`` / ``import X as Y``. Such a binding
    #: always holds a *module*.
    module_imports: frozenset[str]
    #: Every name a ``from ... import`` binds, with every place it is bound
    #: from. Self-submodule imports (``from . import NAME``) are included: the
    #: caller follows them like any other, to the submodule on disk or not.
    from_imports: Mapping[str, tuple[Origin, ...]]
    #: Every ``from M import *``, as ``(level, M)`` with ``M`` ``None`` for
    #: ``from . import *``.
    stars: tuple[tuple[int, str | None], ...]
    #: The names ``__all__`` lists, when it is statically known; ``None`` when
    #: the module has no ``__all__`` at all.
    all_names: frozenset[str] | None
    #: True when ``__all__`` exists but cannot be read without running the
    #: module (see `_static_all`). `all_names` is then ``None`` and means
    #: nothing.
    all_dynamic: bool

    @property
    def has_getattr(self) -> bool:
        """True when the module binds a PEP 562 module-level ``__getattr__``."""
        name = "__getattr__"
        return name in self.defined or name in self.module_imports or name in self.from_imports


def _flatten(body: list[ast.stmt]) -> Iterator[ast.stmt]:
    """Every statement that runs at module level, descending as `_collect` does."""
    for stmt in body:
        yield stmt
        blocks: list[list[ast.stmt]] = []
        if isinstance(stmt, (ast.If, ast.For, ast.AsyncFor, ast.While)):
            blocks = [stmt.body, stmt.orelse]
        elif isinstance(stmt, (ast.Try, ast.TryStar)):
            blocks = [stmt.body, stmt.orelse, stmt.finalbody, *(h.body for h in stmt.handlers)]
        elif isinstance(stmt, (ast.With, ast.AsyncWith)):
            blocks = [stmt.body]
        elif isinstance(stmt, ast.Match):
            blocks = [case.body for case in stmt.cases]
        for block in blocks:
            yield from _flatten(block)


def _static_all(tree: ast.Module) -> tuple[frozenset[str] | None, bool]:
    """``(names, dynamic)`` for the module's ``__all__``.

    ``__all__`` is static only when the name ``__all__`` occurs exactly once in
    the whole file, as the target of an unconditional module-level assignment
    of a list or tuple of string literals. Any other occurrence -- ``+=``, an
    ``.extend(...)``, a second assignment, a conditional one, a read that
    could alias it for mutation -- makes it dynamic, because what a star
    import then exports is decided by running the module. A write through
    ``globals()`` is not seen; that is the one limit of reading it this way.
    """
    occurrences = sum(
        1 for node in ast.walk(tree) if isinstance(node, ast.Name) and node.id == "__all__"
    )
    if occurrences == 0:
        return None, False
    if occurrences == 1:
        for stmt in tree.body:
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
                target, value = stmt.targets[0], stmt.value
            elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
                target, value = stmt.target, stmt.value
            else:
                continue
            if not (isinstance(target, ast.Name) and target.id == "__all__"):
                continue
            if isinstance(value, (ast.List, ast.Tuple)) and all(
                isinstance(e, ast.Constant) and isinstance(e.value, str) for e in value.elts
            ):
                return frozenset(
                    str(e.value) for e in value.elts if isinstance(e, ast.Constant)
                ), False
    return None, True


@functools.cache
def namespace(path: str) -> Namespace | None:
    """The `Namespace` of the module at *path*, or ``None`` when it cannot be read.

    Unbounded, unlike the caches above: the module map already holds every
    path it will ask about, and star-import and re-export chains revisit the
    same files many times over. Same caveat otherwise -- keyed on the path, no
    mtime check.
    """
    tree = _parse(path)
    if tree is None:
        return None
    defined: set[str] = set()
    _collect(tree.body, defined, set(), set())
    module_imports: set[str] = set()
    from_imports: dict[str, list[Origin]] = {}
    stars: list[tuple[int, str | None]] = []
    for stmt in _flatten(tree.body):
        if isinstance(stmt, ast.Import):
            module_imports.update(a.asname or a.name.split(".")[0] for a in stmt.names)
        elif isinstance(stmt, ast.ImportFrom):
            for alias in stmt.names:
                if alias.name == "*":
                    stars.append((stmt.level, stmt.module))
                else:
                    origin = (stmt.level, stmt.module, alias.name)
                    from_imports.setdefault(alias.asname or alias.name, []).append(origin)
    all_names, all_dynamic = _static_all(tree)
    return Namespace(
        defined=frozenset(defined),
        module_imports=frozenset(module_imports),
        from_imports={name: tuple(origins) for name, origins in from_imports.items()},
        stars=tuple(stars),
        all_names=all_names,
        all_dynamic=all_dynamic,
    )

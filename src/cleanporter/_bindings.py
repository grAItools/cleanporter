"""Top-level name bindings of a module, read with ``ast``.

Two questions are answered here, both by parsing only -- nothing is imported:

* which names a package ``__init__`` binds at top level, so a binding that
  shadows a real submodule on disk can be reported ambiguous rather than
  guessed at (`top_level_bindings`), and
* which of a module's top-level names it merely *re-exports* -- binds by
  importing them from somewhere else rather than defining them
  (`import_bound_names`).
"""

from __future__ import annotations

import ast
import functools
import pathlib


def _collect(
    body: list[ast.stmt],
    defined: set[str],
    imported: set[str],
    submodule_imports: set[str],
) -> None:
    """Absorb *body*'s top-level bindings, split by *how* each one was bound.

    ``defined`` gets names a statement creates here (a ``def``, a ``class``,
    an assignment); ``imported`` gets names an ``import`` brings in. The two
    are collected separately rather than derived from one another because a
    name can be both -- ``try: from x import y`` with a fallback ``def y`` in
    the ``except`` is bound either way, so it survives a rewrite of the import
    and is not a re-export.
    """
    for stmt in body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(stmt.name)
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
                imported.add(bound)
                # ``from . import mod`` binds the submodule itself, so it is
                # not a shadowing binding -- but only at level 1 (the
                # package's own submodule) and only when unaliased: ``from .
                # import mod as m`` binds ``m``, which is *not* auto-populated
                # as an attribute, so it can still shadow a real ``pkg/m.py``.
                # ``from .. import Y`` (level 2+) names a submodule of an
                # *ancestor* package, not of this one, so it is a genuine
                # (possibly shadowing) binding here too.
                if stmt.level == 1 and stmt.module is None and alias.asname is None:
                    submodule_imports.add(bound)
        elif isinstance(stmt, ast.If):
            _collect(stmt.body, defined, imported, submodule_imports)
            _collect(stmt.orelse, defined, imported, submodule_imports)
        elif isinstance(stmt, (ast.For, ast.AsyncFor)):
            defined.update(n.id for n in ast.walk(stmt.target) if isinstance(n, ast.Name))
            _collect(stmt.body, defined, imported, submodule_imports)
            _collect(stmt.orelse, defined, imported, submodule_imports)
        elif isinstance(stmt, ast.While):
            _collect(stmt.body, defined, imported, submodule_imports)
            _collect(stmt.orelse, defined, imported, submodule_imports)
        elif isinstance(stmt, ast.Match):
            # Capture-pattern bindings (e.g. ``case Foo(x=y):``) are not
            # extracted -- a module-level ``match`` in an ``__init__.py``
            # binding a name that shadows a submodule is vanishingly rare,
            # and this limit is stated rather than silently assumed.
            for case in stmt.cases:
                _collect(case.body, defined, imported, submodule_imports)
        elif isinstance(stmt, (ast.Try, ast.TryStar)):
            _collect(stmt.body, defined, imported, submodule_imports)
            _collect(stmt.orelse, defined, imported, submodule_imports)
            _collect(stmt.finalbody, defined, imported, submodule_imports)
            for handler in stmt.handlers:
                _collect(handler.body, defined, imported, submodule_imports)
        elif isinstance(stmt, (ast.With, ast.AsyncWith)):
            _collect(stmt.body, defined, imported, submodule_imports)


@functools.lru_cache(maxsize=1024)
def top_level_bindings(path: str) -> frozenset[str]:
    """Names bound at the top level of *path*, excluding self-submodule imports.

    Cached on ``path`` alone, with no mtime check. That is only safe because
    nothing in a single run rewrites a scanned ``__init__.py``'s plain
    assignments before this cache is consulted again -- the fixer only
    rewrites ``ImportFrom`` nodes, and the one self-referential pattern it
    could otherwise introduce is already excluded. This is an assumption
    about the fixer's current narrow scope, not an enforced invariant.
    """
    try:
        tree = ast.parse(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError, ValueError):
        return frozenset()
    defined: set[str] = set()
    imported: set[str] = set()
    submodule_imports: set[str] = set()
    _collect(tree.body, defined, imported, submodule_imports)
    return frozenset((defined | imported) - submodule_imports)


@functools.lru_cache(maxsize=1024)
def import_bound_names(path: str) -> frozenset[str]:
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
    `top_level_bindings`.
    """
    try:
        tree = ast.parse(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError, ValueError):
        return frozenset()
    defined: set[str] = set()
    imported: set[str] = set()
    submodule_imports: set[str] = set()
    _collect(tree.body, defined, imported, submodule_imports)
    return frozenset(imported - defined - submodule_imports)

"""Top-level name bindings of a module, read with ``ast``.

Used to detect the case where a package ``__init__`` binds a name that also
exists as a submodule on disk. Parsing only -- nothing is imported.
"""

from __future__ import annotations

import ast
import functools
import pathlib

_TRY_TYPES: tuple[type[ast.AST], ...] = (ast.Try,) + (
    (ast.TryStar,) if hasattr(ast, "TryStar") else ()
)


def _collect(body: list[ast.stmt], names: set[str], submodule_imports: set[str]) -> None:
    for stmt in body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(stmt.name)
        elif isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                names.update(n.id for n in ast.walk(target) if isinstance(n, ast.Name))
        elif isinstance(stmt, ast.AnnAssign):
            # A bare ``mod: Type`` annotation (no ``=``) only populates
            # ``__annotations__``; it does not bind ``mod`` at runtime.
            if stmt.value is not None and isinstance(stmt.target, ast.Name):
                names.add(stmt.target.id)
        elif isinstance(stmt, ast.AugAssign):
            if isinstance(stmt.target, ast.Name):
                names.add(stmt.target.id)
        elif isinstance(stmt, ast.Import):
            for alias in stmt.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(stmt, ast.ImportFrom):
            for alias in stmt.names:
                if alias.name == "*":
                    continue
                bound = alias.asname or alias.name
                names.add(bound)
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
            _collect(stmt.body, names, submodule_imports)
            _collect(stmt.orelse, names, submodule_imports)
        elif isinstance(stmt, (ast.For, ast.AsyncFor)):
            names.update(n.id for n in ast.walk(stmt.target) if isinstance(n, ast.Name))
            _collect(stmt.body, names, submodule_imports)
            _collect(stmt.orelse, names, submodule_imports)
        elif isinstance(stmt, ast.While):
            _collect(stmt.body, names, submodule_imports)
            _collect(stmt.orelse, names, submodule_imports)
        elif isinstance(stmt, ast.Match):
            # Capture-pattern bindings (e.g. ``case Foo(x=y):``) are not
            # extracted -- a module-level ``match`` in an ``__init__.py``
            # binding a name that shadows a submodule is vanishingly rare,
            # and this limit is stated rather than silently assumed.
            for case in stmt.cases:
                _collect(case.body, names, submodule_imports)
        elif isinstance(stmt, _TRY_TYPES):
            _collect(stmt.body, names, submodule_imports)
            _collect(stmt.orelse, names, submodule_imports)
            _collect(stmt.finalbody, names, submodule_imports)
            for handler in stmt.handlers:
                _collect(handler.body, names, submodule_imports)
        elif isinstance(stmt, (ast.With, ast.AsyncWith)):
            _collect(stmt.body, names, submodule_imports)


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
    names: set[str] = set()
    submodule_imports: set[str] = set()
    _collect(tree.body, names, submodule_imports)
    return frozenset(names - submodule_imports)

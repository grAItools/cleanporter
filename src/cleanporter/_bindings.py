"""Top-level name bindings of a module, read with ``ast``.

Used to detect the case where a package ``__init__`` binds a name that also
exists as a submodule on disk. Parsing only -- nothing is imported.
"""

from __future__ import annotations

import ast
from functools import lru_cache
from pathlib import Path

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
        elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
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
                # not a shadowing binding.
                if stmt.level and stmt.module is None:
                    submodule_imports.add(bound)
        elif isinstance(stmt, ast.If):
            _collect(stmt.body, names, submodule_imports)
            _collect(stmt.orelse, names, submodule_imports)
        elif isinstance(stmt, _TRY_TYPES):
            _collect(stmt.body, names, submodule_imports)
            _collect(stmt.orelse, names, submodule_imports)
            _collect(stmt.finalbody, names, submodule_imports)
            for handler in stmt.handlers:
                _collect(handler.body, names, submodule_imports)
        elif isinstance(stmt, (ast.With, ast.AsyncWith)):
            _collect(stmt.body, names, submodule_imports)


@lru_cache(maxsize=1024)
def top_level_bindings(path: str) -> frozenset[str]:
    """Names bound at the top level of *path*, excluding self-submodule imports."""
    try:
        tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError, ValueError):
        return frozenset()
    names: set[str] = set()
    submodule_imports: set[str] = set()
    _collect(tree.body, names, submodule_imports)
    return frozenset(names - submodule_imports)

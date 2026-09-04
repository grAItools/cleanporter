"""Helpers for reading ``from ... import ...`` statements with libCST."""

from __future__ import annotations

import libcst as cst


def dotted(node: cst.BaseExpression | None) -> str:
    """Render a dotted ``Name``/``Attribute`` expression as ``a.b.c`` (``""`` if None)."""
    if node is None:
        return ""
    if isinstance(node, cst.Name):
        return node.value
    if isinstance(node, cst.Attribute):
        return f"{dotted(node.value)}.{node.attr.value}"
    raise TypeError(f"unexpected import module node: {type(node).__name__}")


def relative_level(node: cst.ImportFrom) -> int:
    """Number of leading dots (0 for an absolute import)."""
    return len(node.relative)


def resolve_parent(node: cst.ImportFrom, base_pkg: str) -> str | None:
    """Absolute dotted module that ``node`` imports *from*.

    ``base_pkg`` is the package containing the current file (``""`` for a
    top-level module). Returns ``None`` when a relative import cannot be
    anchored (e.g. it reaches above the top-level package).
    """
    module_str = dotted(node.module)
    level = relative_level(node)
    if level == 0:
        return module_str or None

    anchor = base_pkg.split(".") if base_pkg else []
    up = level - 1
    if up > len(anchor):
        return None
    anchor = anchor[: len(anchor) - up] if up else anchor
    parts = anchor + (module_str.split(".") if module_str else [])
    return ".".join(parts) or None


def imported_names(node: cst.ImportFrom) -> list[tuple[str, str | None, cst.ImportAlias]]:
    """List of ``(name, asname, alias_node)`` for a non-star import.

    ``name`` is the imported identifier, ``asname`` the bound alias (or None),
    and ``alias_node`` the original :class:`cst.ImportAlias` for surgery.
    """
    if isinstance(node.names, cst.ImportStar):
        return []
    out: list[tuple[str, str | None, cst.ImportAlias]] = []
    for alias in node.names:
        name = alias.name.value if isinstance(alias.name, cst.Name) else dotted(alias.name)
        asname = None
        if alias.asname is not None and isinstance(alias.asname.name, cst.Name):
            asname = alias.asname.name.value
        out.append((name, asname, alias))
    return out


def is_star(node: cst.ImportFrom) -> bool:
    return isinstance(node.names, cst.ImportStar)


def is_explicit_reexport(name: str, asname: str | None) -> bool:
    """True for ``from P import S as S`` -- PEP 484's *redundant alias*.

    Aliasing a name to itself is a no-op at runtime, so it is only ever
    written to say something to a reader or a type checker: this name is a
    deliberate part of the module's public surface. mypy's
    ``no_implicit_reexport`` and ruff's ``F401`` both read it that way, which
    makes it the one re-export marker that is machine-readable rather than
    inferred.

    That matters here because rewriting such an import *removes a public
    name*: ``from .exceptions import UsageError as UsageError`` in
    ``pkg/config/__init__.py`` is what makes ``from pkg.config import
    UsageError`` work everywhere else, and turning it into ``from pkg import
    exceptions`` breaks every one of those importers -- in files this tool
    may never even look at. It is the same failure the ``__all__``
    string-mention guard catches, stated in syntax instead of in a string, so
    it gets the same answer: reported, never rewritten.
    """
    return asname is not None and asname == name

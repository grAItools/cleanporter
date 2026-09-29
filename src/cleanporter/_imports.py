"""Helpers for reading ``from ... import ...`` statements with libCST."""

from __future__ import annotations

import sys

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


def module_import_spelling(
    node: cst.ImportFrom, parent: str, *, declared_root: bool = False
) -> tuple[str, str] | None:
    """How to spell an import of *parent*, the module *node* imports from.

    Returns ``(package, token)``: the statement is ``from <package> import
    <token>``, or ``import <token>`` when *package* is empty (see
    `render_import`). ``None`` means no relative spelling exists -- see below.

    An absolute import is spelled from *parent*, which is its own text.

    A relative import stays relative. Its absolute *parent* is only as good
    as the import root inferred for the file, and a PEP 420 namespace
    directory can make that root wrong: ``from .readers import read`` in
    ``analytics/io/__init__.py``, anchored against an inferred ``analytics/``
    root, is ``io.readers`` -- and ``from io import readers`` is the standard
    library. A relative spelling keeps the author's own dots and names:

    * ``from .readers import read`` -> ``from . import readers``;
    * ``from ..pkg.mod import X`` -> ``from ..pkg import mod``.

    Neither depends on the root at all: the dots climb from wherever the file
    really is, exactly as the original import did.

    ``from . import X`` / ``from .. import X`` has no module part to split:
    *parent* is the package the dots reach. The only relative spelling of a
    package is one more dot and its own name -- ``from .. import pkg`` --
    and that one *does* lean on the root, which must give ``pkg`` a parent
    package. The name itself is the leaf of *parent*, a directory name, so
    the risk is only in the extra dot, and it fails loudly: if ``pkg`` is in
    fact top-level, the import raises ``ImportError`` ("attempted relative
    import beyond top-level package") the first time the module is imported.
    It cannot silently bind a different module, the way ``import io`` did.

    When *parent* has no parent package under this run's roots, there is no
    relative spelling at all; an absolute ``import pkg`` would depend on the
    root in exactly the silent way the relative form does not, so this
    returns ``None`` and the import is kept (`unspellable_reason`).

    Unless the root is *declared*, and soundly so. *declared_root* says the
    file is anchored in a root the user named (``--root`` / ``source_roots``)
    that also passes the structural checks in
    `firstparty.ModuleMap.root_for_absolute_spelling`: it is not itself
    inside a package, it neither nests in nor contains another root, it is
    the only root holding a top-level name ``pkg``, and the file is not
    ``pkg``'s own ``__init__``. Then *parent* is not a reading of the
    directory tree but the name the user said the package has on
    ``sys.path``, and ``import pkg`` binds exactly the package the dots
    reach, on the same footing as any absolute import the fixer writes. Only
    this case consults it -- every relative spelling above stays relative,
    since it needs no root at all.

    A package named like a standard-library module is kept even then
    (`is_stdlib_name`): ``import io`` is the standard library's whatever the
    user's root holds, which is precisely the silent rebinding the relative
    spelling exists to avoid.
    """
    level = relative_level(node)
    if level == 0:
        package, _, token = parent.rpartition(".")
        return package, token
    module = dotted(node.module)
    if module:
        head, _, token = module.rpartition(".")
        return "." * level + head, token
    package, _, token = parent.rpartition(".")
    if not package:
        return ("", token) if declared_root and not is_stdlib_name(token) else None
    return "." * (level + 1), token


def render_import(spelling: tuple[str, str], bind: str | None = None) -> str:
    """The statement text for a `module_import_spelling` result, bound as *bind*."""
    package, token = spelling
    code = f"from {package} import {token}" if package else f"import {token}"
    if bind is not None and bind != token:
        code += f" as {bind}"
    return code


#: Top-level standard-library modules some supported Python has that the one
#: running cleanporter may not: removed in 3.12 (PEP 594's first wave and
#: ``distutils``/``imp``) or 3.13, or added in 3.14. The target interpreter
#: can be any of them, and its ``sys.stdlib_module_names`` is not asked --
#: that would cost a probe round-trip for a check that only ever declines.
_OTHER_VERSIONS_STDLIB = frozenset(
    {
        # removed in 3.12
        "asynchat",
        "asyncore",
        "distutils",
        "imp",
        "smtpd",
        # removed in 3.13 (PEP 594)
        "aifc",
        "audioop",
        "cgi",
        "cgitb",
        "chunk",
        "crypt",
        "imghdr",
        "lib2to3",
        "mailcap",
        "msilib",
        "nis",
        "nntplib",
        "ossaudiodev",
        "pipes",
        "sndhdr",
        "spwd",
        "sunau",
        "telnetlib",
        "uu",
        "xdrlib",
        # added in 3.14
        "annotationlib",
        "compression",
    }
)


def is_stdlib_name(name: str) -> bool:
    """Whether *name* is, or in some supported Python was, a top-level stdlib module.

    This interpreter's ``sys.stdlib_module_names``, widened by the names
    other supported versions add or removed (`_OTHER_VERSIONS_STDLIB`).
    Over-approximating is the safe direction: a name in here only ever keeps
    an import as written.
    """
    return name in sys.stdlib_module_names or name in _OTHER_VERSIONS_STDLIB


def unspellable_reason(node: cst.ImportFrom, parent: str, *, root_hint: str = "") -> str:
    """Why *node*, for which `module_import_spelling` is ``None``, is kept.

    *root_hint* is the inferred import root the file is anchored in, when
    declaring it would let the import be spelled ``import pkg`` (see
    `firstparty.ModuleMap.root_for_absolute_spelling`); the reason then says
    so, conditionally -- declaring a root is a statement about where Python
    imports the package from, not a switch to flip. It is left out for a
    standard-library name, which no declaration unlocks.
    """
    dots = "." * relative_level(node)
    if is_stdlib_name(parent):
        return (
            f"`from {dots} import` names the top-level package '{parent}' itself, whose "
            f"only spelling is the absolute 'import {parent}', and that names the standard "
            f"library's '{parent}', not this package"
        )
    hint = (
        f" (if '{root_hint}' is where Python imports it from, declaring it with --root or "
        f"source_roots lets --fix write 'import {parent}')"
        if root_hint
        else ""
    )
    return (
        f"`from {dots} import` names the package '{parent}' itself, and a relative import "
        f"can reach a package only from its parent, which '{parent}' does not have under "
        "this run's import roots; an absolute spelling would depend on the import root, "
        f"which the original relative import did not{hint}"
    )


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

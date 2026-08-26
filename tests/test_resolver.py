"""Resolver unit tests."""

from __future__ import annotations

from cleanporter.resolver import Origin, SymbolKind

from .conftest import make_resolver


def test_first_party_object_symbol(make_project):
    cfg = make_project()
    resolver = make_resolver(cfg)
    result = resolver.classify_absolute("mypkg.helpers", "Widget")
    assert result.kind is SymbolKind.OBJECT
    assert result.origin is Origin.FIRST_PARTY
    assert result.is_violation


def test_first_party_submodule_symbol(make_project):
    cfg = make_project()
    resolver = make_resolver(cfg)
    assert resolver.classify_absolute("mypkg", "helpers").kind is SymbolKind.MODULE
    assert resolver.classify_absolute("mypkg.sub", "data").kind is SymbolKind.MODULE


def test_unknown_prefix_is_unresolvable_but_reported(make_project):
    cfg = make_project()
    resolver = make_resolver(cfg, runtime_fallback=False)
    result = resolver.classify_absolute("no.such.module", "thing")
    assert result.kind is SymbolKind.UNRESOLVABLE


def test_stdlib_module_via_runtime_fallback():
    import sysconfig
    from pathlib import Path
    from cleanporter.config import Config
    from cleanporter.resolver import Resolver

    roots = [Path(sysconfig.get_path("purelib"))]
    resolver = Resolver(roots, runtime_fallback=True)
    assert resolver.classify_absolute("os", "path").kind is SymbolKind.MODULE


def test_stdlib_object_via_runtime_fallback():
    import sysconfig
    from pathlib import Path
    from cleanporter.config import Config
    from cleanporter.resolver import Resolver

    roots = [Path(sysconfig.get_path("purelib"))]
    resolver = Resolver(roots, runtime_fallback=True)
    result = resolver.classify_absolute("collections", "OrderedDict")
    assert result.kind is SymbolKind.OBJECT
    assert result.is_violation


def test_runtime_fallback_disabled_leaves_unresolvable(make_project):
    cfg = make_project()
    resolver = make_resolver(cfg, runtime_fallback=False)
    result = resolver.classify_absolute("collections", "OrderedDict")
    assert result.kind is SymbolKind.UNRESOLVABLE


def test_ambiguous_binding_and_submodule(tmp_path, make_project):
    base = make_project()
    pkg = base / "src" / "mypkg"
    # Both a submodule clash.py and a __init__-level binding 'clash' exist.
    (pkg / "__init__.py").write_text("clash = object()\n", encoding="utf-8")
    (pkg / "clash.py").write_text("x = 1\n", encoding="utf-8")

    from cleanporter.config import Config
    from cleanporter.resolver import Resolver, discover_source_roots

    cfg = Config(root=base)
    resolver = Resolver(discover_source_roots(base, ()), runtime_fallback=True)
    result = resolver.classify_absolute("mypkg", "clash")
    assert result.kind is SymbolKind.UNRESOLVABLE

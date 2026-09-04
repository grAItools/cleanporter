"""Analyzer + fixer behaviour over real files."""

from __future__ import annotations

import pathlib

import libcst as cst

from cleanporter import analyze, config, firstparty, model, rewrite
from cleanporter import resolver as resolver_lib

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def _record(
    source: str, path: pathlib.Path, module_map: firstparty.ModuleMap
) -> analyze.FileRecord:
    return analyze.FileRecord(
        path, source, cst.parse_module(source), analyze.package_of(path, module_map)
    )


def _fix(source: str, path: pathlib.Path) -> str:
    mm = firstparty.ModuleMap.from_paths([FIXTURES / "pkg", path])
    resolver = resolver_lib.Resolver(mm)
    rec = _record(source, path, mm)
    resolver.warm(_warm_pairs(rec))
    return rewrite.fix_record(rec, resolver, config.Config()).source


def _units(rec: analyze.FileRecord):
    from cleanporter import analyze as analyze_lib

    return [u for u in analyze_lib.iter_units(rec.tree, rec.base_pkg) if u.parent and not u.star]


def _warm_pairs(rec: analyze.FileRecord) -> list[tuple[str, str]]:
    return [(u.parent, u.name) for u in _units(rec) if u.parent]


def _analyze(source: str, path: pathlib.Path):
    mm = firstparty.ModuleMap.from_paths([FIXTURES / "pkg", path])
    resolver = resolver_lib.Resolver(mm)
    rec = _record(source, path, mm)
    resolver.warm(_warm_pairs(rec))
    return analyze.analyze_record(rec, resolver, config.Config())


# -- analysis -------------------------------------------------------------
def test_module_import_is_clean():
    src = "from pkg.sub import mod\nfrom os import path\nimport functools\n"
    assert _analyze(src, FIXTURES / "pkg" / "a.py") == []


def test_object_import_first_party_is_violation():
    src = "from pkg.sub.mod import Thing\n"
    findings = _analyze(src, FIXTURES / "pkg" / "a.py")
    assert [f.status for f in findings] == [model.Status.VIOLATION]
    assert findings[0].parent == "pkg.sub.mod"
    assert findings[0].name == "Thing"


def test_stdlib_object_is_violation():
    src = "from functools import partial\n"
    findings = _analyze(src, FIXTURES / "pkg" / "a.py")
    assert [f.status for f in findings] == [model.Status.VIOLATION]


def test_typing_is_exempt():
    src = "from typing import List, Optional\nfrom collections.abc import Mapping\n"
    assert _analyze(src, FIXTURES / "pkg" / "a.py") == []


def test_star_and_unknown():
    src = "from functools import *\nfrom nonexistent_pkg_xyz import Thing\n"
    findings = _analyze(src, FIXTURES / "pkg" / "a.py")
    statuses = sorted(f.status.value for f in findings)
    assert statuses == ["skipped", "unresolved"]


# -- fixing ---------------------------------------------------------------
def test_fix_first_party_object():
    src = "from pkg.sub.mod import Thing\n\nx = Thing()\n"
    out = _fix(src, FIXTURES / "pkg" / "a.py")
    assert "from pkg.sub import mod" in out
    assert "mod.Thing()" in out
    assert "import Thing" not in out


def test_fix_stdlib_object_toplevel_module():
    src = "from functools import partial\n\nf = partial(int)\n"
    out = _fix(src, FIXTURES / "pkg" / "a.py")
    assert "import functools" in out
    assert "functools.partial(int)" in out


def test_fix_mixed_keeps_module_name():
    src = "from pkg.sub import mod\nfrom pkg.sub.mod import Thing\n\ny = mod\nz = Thing\n"
    out = _fix(src, FIXTURES / "pkg" / "a.py")
    assert "from pkg.sub import mod" in out
    assert "mod.Thing" in out


def test_fix_respects_alias():
    src = "from pkg.sub.mod import Thing as T\n\nq = T()\n"
    out = _fix(src, FIXTURES / "pkg" / "a.py")
    assert "from pkg.sub import mod" in out
    assert "mod.Thing()" in out
    assert "T()" not in out


def test_fix_does_not_touch_shadowed_local():
    src = (
        "from functools import reduce\n\n"
        "def f():\n"
        "    reduce = 1\n"
        "    return reduce\n\n"
        "g = reduce\n"
    )
    out = _fix(src, FIXTURES / "pkg" / "a.py")
    # the local variable stays bare; only the module-level use is qualified
    assert "reduce = 1" in out
    assert "return reduce" in out
    assert "g = functools.reduce" in out


def test_fix_is_idempotent():
    src = "from pkg.sub.mod import Thing\n\nx = Thing()\n"
    once = _fix(src, FIXTURES / "pkg" / "a.py")
    twice = _fix(once, FIXTURES / "pkg" / "a.py")
    assert once == twice


def test_type_checking_import_not_fixed():
    src = (
        "from typing import TYPE_CHECKING\n\n"
        "if TYPE_CHECKING:\n"
        "    from functools import partial\n\n"
        "def f(x: 'partial') -> None: ...\n"
    )
    out = _fix(src, FIXTURES / "pkg" / "a.py")
    assert "from functools import partial" in out  # left untouched


# -- scope ------------------------------------------------------------------
def test_scope_first_party_ignores_stdlib():
    src = "from functools import partial\nfrom pkg.sub.mod import Thing\n"
    findings = _analyze_with(src, config.Config(scope="first-party"))
    assert [f.parent for f in findings] == ["pkg.sub.mod"]


def test_scope_all_reports_both():
    src = "from functools import partial\nfrom pkg.sub.mod import Thing\n"
    findings = _analyze_with(src, config.Config(scope="all"))
    assert sorted(f.parent for f in findings) == ["functools", "pkg.sub.mod"]


def test_scope_first_party_still_reports_unanchorable_relative_imports():
    findings = _analyze_with("from ..... import nothing\n", config.Config(scope="first-party"))
    assert [f.status for f in findings] == [model.Status.UNRESOLVED]


def _analyze_with(source: str, config: config.Config):
    path = FIXTURES / "pkg" / "a.py"
    mm = firstparty.ModuleMap.from_paths([FIXTURES / "pkg", path])
    resolver = resolver_lib.Resolver(mm)
    rec = _record(source, path, mm)
    resolver.warm([(u.parent, u.name) for u in rec.units if u.parent and not u.star])
    return analyze.analyze_record(rec, resolver, config)


def test_an_explicit_reexport_is_reported_as_skipped_not_a_violation():
    """`from P import S as S` is a declared public name; it cannot be rewritten."""
    findings = _analyze("from pkg.sub.mod import Thing as Thing\n", FIXTURES / "pkg" / "a.py")
    assert [f.code for f in findings] == ["CP003"]
    assert "public name" in findings[0].detail


def test_an_ordinary_alias_is_still_a_violation():
    findings = _analyze("from pkg.sub.mod import Thing as T\n", FIXTURES / "pkg" / "a.py")
    assert [f.code for f in findings] == ["CP001"]


def _reexport_tree(tmp_path: pathlib.Path) -> pathlib.Path:
    """`pkg.tool` re-exports `dump`; `pkg.user` imports it from there."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "display.py").write_text("def dump():\n    return 1\n")
    (pkg / "tool.py").write_text("from pkg.display import dump\n\ndef go():\n    return dump()\n")
    (pkg / "user.py").write_text("from pkg.tool import dump\nx = dump()\n")
    return pkg


def _findings_by_file(pkg: pathlib.Path):
    records, resolver, _errors, _warnings = analyze.build([pkg], config.Config(root=pkg.parent))
    return {
        rec.path.name: analyze.analyze_record(rec, resolver, config.Config(root=pkg.parent))
        for rec in records
    }


def test_a_load_bearing_reexport_is_reported_as_skipped(tmp_path: pathlib.Path) -> None:
    """`tool.py`'s own import is what makes `pkg.tool.dump` exist for `user.py`.

    Rewriting it is correct for `tool.py` alone and deletes the attribute
    `user.py` reads. Found by running libCST's own test suite against a
    rewritten copy: `libcst.tool.dump` stopped existing.
    """
    by_file = _findings_by_file(_reexport_tree(tmp_path))
    assert [f.code for f in by_file["tool.py"]] == ["CP003"]
    assert "another file imports 'dump' from 'pkg.tool'" in by_file["tool.py"][0].detail


def test_the_consumer_of_a_reexport_is_still_a_plain_violation(tmp_path: pathlib.Path) -> None:
    """Only the re-exporting side is protected; the consumer is fixable.

    Because `tool.py` keeps its import, `pkg.tool.dump` still exists, so
    qualifying `user.py`'s reference through it is safe.
    """
    by_file = _findings_by_file(_reexport_tree(tmp_path))
    assert [f.code for f in by_file["user.py"]] == ["CP001"]


def test_a_reexport_nobody_imports_is_still_fixable(tmp_path: pathlib.Path) -> None:
    """No consumer, no hazard: deleting an attribute nothing reads is free."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "display.py").write_text("def dump():\n    return 1\n")
    (pkg / "tool.py").write_text("from pkg.display import dump\n\ndef go():\n    return dump()\n")
    by_file = _findings_by_file(pkg)
    assert [f.code for f in by_file["tool.py"]] == ["CP001"]


def test_a_name_both_imported_and_defined_is_not_protected(tmp_path: pathlib.Path) -> None:
    """A try/except import with a fallback definition survives a rewrite."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "display.py").write_text("def dump():\n    return 1\n")
    (pkg / "tool.py").write_text(
        "try:\n    from pkg.display import dump\nexcept ImportError:\n"
        "    def dump():\n        return 0\n"
    )
    (pkg / "user.py").write_text("from pkg.tool import dump\nx = dump()\n")
    _records, resolver, _e, _w = analyze.build([pkg], config.Config(root=pkg.parent))
    assert resolver.is_load_bearing("pkg.tool", "dump") is False


def _shadowed_package_tree(tmp_path: pathlib.Path) -> pathlib.Path:
    """``pkg/`` re-exporting ``helper``, with a stale flat ``pkg.py`` beside it.

    The shape a package picks up when an older single-file release is left in
    place next to a newer packaged one -- the corpus has exactly this in
    ``click_plugins.py`` (2.0dev) beside ``click_plugins/`` (1.1.1.2).
    """
    (tmp_path / "pkg.py").write_text('def helper():\n    return "flat"\n')
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("from pkg.core import helper\n")
    (pkg / "core.py").write_text('def helper():\n    return "from package"\n')
    (tmp_path / "consumer.py").write_text("from pkg import helper\nx = helper()\n")
    return tmp_path


def test_a_reexport_is_protected_through_a_module_of_the_same_name(
    tmp_path: pathlib.Path,
) -> None:
    """A flat ``pkg.py`` must not make ``pkg/__init__.py`` look rewritable.

    Python resolves ``import pkg`` to the *package* and ignores the flat
    module, but the module map kept one source file per dotted name and the
    flat module was scanned last, so the re-export guard read ``pkg.py``,
    found no re-export and stood down. ``--fix`` then deleted ``pkg.helper``
    while rewriting ``consumer.py`` to read it. Found in the corpus:
    ``click_plugins.py`` beside ``click_plugins/`` broke
    ``celery.bin.celery``.
    """
    by_file = _findings_by_file(_shadowed_package_tree(tmp_path))
    assert [f.code for f in by_file["__init__.py"]] == ["CP003"]
    assert "another file imports 'helper' from 'pkg'" in by_file["__init__.py"][0].detail


def test_the_consumer_of_a_shadowed_reexport_is_still_a_violation(
    tmp_path: pathlib.Path,
) -> None:
    """Protecting the package's ``__init__`` is what keeps the consumer fixable."""
    by_file = _findings_by_file(_shadowed_package_tree(tmp_path))
    assert [f.code for f in by_file["consumer.py"]] == ["CP001"]


def _self_shadowing_tree(tmp_path: pathlib.Path, init: str) -> pathlib.Path:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "serialization.py").write_text('MARK = "pkg"\n')
    (pkg / "__init__.py").write_text(init)
    return pkg


def test_an_import_of_a_submodule_its_own_init_shadows_is_skipped(
    tmp_path: pathlib.Path,
) -> None:
    """The replacement would bind the shadowing name, not the submodule.

    ``from pkg import serialization`` inside ``pkg/__init__.py`` binds
    ``getattr(pkg, 'serialization')`` and only imports the submodule when
    that attribute is absent -- so the module-level ``serialization = 42``
    wins. Reported rather than silently left as a ``CP001`` nothing will
    ever clear.
    """
    pkg = _self_shadowing_tree(
        tmp_path, "serialization = 42\nfrom pkg.serialization import MARK\nx = MARK\n"
    )
    findings = _findings_by_file(pkg)["__init__.py"]
    assert [f.code for f in findings] == ["CP003"]
    assert "would bind the existing name instead of the submodule" in findings[0].detail


def test_the_same_import_is_an_ordinary_violation_without_the_shadow(
    tmp_path: pathlib.Path,
) -> None:
    """Self-reference alone is not the problem; only the competing binding is."""
    pkg = _self_shadowing_tree(tmp_path, "from pkg.serialization import MARK\nx = MARK\n")
    assert [f.code for f in _findings_by_file(pkg)["__init__.py"]] == ["CP001"]

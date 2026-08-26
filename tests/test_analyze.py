"""Analyzer + fixer behaviour over real files."""

from __future__ import annotations

from pathlib import Path

import libcst as cst

from cleanporter.analyze import FileRecord, analyze_record, package_of
from cleanporter.config import Config
from cleanporter.firstparty import ModuleMap
from cleanporter.model import Status
from cleanporter.resolver import Resolver
from cleanporter.rewrite import fix_record

FIXTURES = Path(__file__).parent / "fixtures"


def _record(source: str, path: Path, module_map: ModuleMap) -> FileRecord:
    return FileRecord(path, source, cst.parse_module(source), package_of(path, module_map))


def _fix(source: str, path: Path) -> str:
    mm = ModuleMap.from_paths([FIXTURES / "pkg", path])
    resolver = Resolver(mm)
    rec = _record(source, path, mm)
    resolver.warm([(u.parent, u.name) for u in _units(rec)])
    return fix_record(rec, resolver, Config()).source


def _units(rec: FileRecord):
    from cleanporter.analyze import iter_units

    return [u for u in iter_units(rec.tree, rec.base_pkg) if u.parent and not u.star]


def _analyze(source: str, path: Path):
    mm = ModuleMap.from_paths([FIXTURES / "pkg", path])
    resolver = Resolver(mm)
    rec = _record(source, path, mm)
    resolver.warm([(u.parent, u.name) for u in _units(rec)])
    return analyze_record(rec, resolver, Config())


# -- analysis -------------------------------------------------------------
def test_module_import_is_clean():
    src = "from pkg.sub import mod\nfrom os import path\nimport functools\n"
    assert _analyze(src, FIXTURES / "pkg" / "a.py") == []


def test_object_import_first_party_is_violation():
    src = "from pkg.sub.mod import Thing\n"
    findings = _analyze(src, FIXTURES / "pkg" / "a.py")
    assert [f.status for f in findings] == [Status.VIOLATION]
    assert findings[0].parent == "pkg.sub.mod"
    assert findings[0].name == "Thing"


def test_stdlib_object_is_violation():
    src = "from functools import partial\n"
    findings = _analyze(src, FIXTURES / "pkg" / "a.py")
    assert [f.status for f in findings] == [Status.VIOLATION]


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
    findings = _analyze_with(src, Config(scope="first-party"))
    assert [f.parent for f in findings] == ["pkg.sub.mod"]


def test_scope_all_reports_both():
    src = "from functools import partial\nfrom pkg.sub.mod import Thing\n"
    findings = _analyze_with(src, Config(scope="all"))
    assert sorted(f.parent for f in findings) == ["functools", "pkg.sub.mod"]


def test_scope_first_party_still_reports_unanchorable_relative_imports():
    findings = _analyze_with("from ..... import nothing\n", Config(scope="first-party"))
    assert [f.status for f in findings] == [Status.UNRESOLVED]


def _analyze_with(source: str, config: Config):
    path = FIXTURES / "pkg" / "a.py"
    mm = ModuleMap.from_paths([FIXTURES / "pkg", path])
    resolver = Resolver(mm)
    rec = _record(source, path, mm)
    resolver.warm([(u.parent, u.name) for u in rec.units if u.parent and not u.star])
    return analyze_record(rec, resolver, config)

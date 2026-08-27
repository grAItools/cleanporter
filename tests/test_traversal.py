"""Traversal shapes that the old hand-rolled statement walker got wrong."""

from __future__ import annotations

import pathlib

import libcst as cst

from cleanporter import analyze, config, firstparty, model, rewrite
from cleanporter import resolver as resolver_lib

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def _prepare(source: str):
    path = FIXTURES / "pkg" / "a.py"
    mm = firstparty.ModuleMap.from_paths([FIXTURES / "pkg", path])
    resolver = resolver_lib.Resolver(mm)
    rec = analyze.FileRecord(path, source, cst.parse_module(source), analyze.package_of(path, mm))
    from cleanporter import analyze as analyze_lib

    resolver.warm(
        [
            (u.parent, u.name)
            for u in analyze_lib.iter_units(rec.tree, rec.base_pkg)
            if u.parent and not u.star
        ]
    )
    return rec, resolver


def _analyze(source: str):
    rec, resolver = _prepare(source)
    return analyze.analyze_record(rec, resolver, config.Config())


def _fix(source: str) -> str:
    rec, resolver = _prepare(source)
    return rewrite.fix_record(rec, resolver, config.Config()).source


def test_elif_body_does_not_crash_and_is_reported():
    src = (
        "import sys\n"
        "if sys.argv:\n"
        "    pass\n"
        "elif len(sys.argv) > 1:\n"
        "    from pkg.sub.mod import Thing\n"
    )
    findings = _analyze(src)
    assert [f.status for f in findings] == [model.Status.VIOLATION]
    assert findings[0].line == 5


def test_one_liner_suite_is_reported_but_not_rewritten():
    src = "if True: from pkg.sub.mod import Thing\n"
    assert [f.status for f in _analyze(src)] == [model.Status.VIOLATION]
    assert _fix(src) == src


def test_semicolon_joined_line_is_reported_but_not_rewritten():
    src = 'from pkg.sub.mod import Thing; mod = "oops"\n'
    assert [f.status for f in _analyze(src)] == [model.Status.VIOLATION]
    assert _fix(src) == src


def test_async_and_nested_scopes_are_reported():
    src = (
        "async def a():\n"
        "    from pkg.sub.mod import Thing\n"
        "    return Thing\n"
        "\n"
        "class C:\n"
        "    from pkg.sub.mod import Thing as T2\n"
    )
    assert [f.status for f in _analyze(src)] == [model.Status.VIOLATION, model.Status.VIOLATION]

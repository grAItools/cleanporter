"""The analysis path must not re-walk a file's tree."""

from __future__ import annotations

import pathlib

import libcst as cst

from cleanporter import analyze, config, firstparty
from cleanporter import resolver as resolver_lib

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

SOURCE = "from pkg.sub.mod import Thing\nfrom pkg.sub import mod\nx = Thing()\n"


def _record() -> analyze.FileRecord:
    path = FIXTURES / "pkg" / "a.py"
    mm = firstparty.ModuleMap.from_paths([FIXTURES / "pkg", path])
    return analyze.FileRecord(path, SOURCE, cst.parse_module(SOURCE), analyze.package_of(path, mm))


def test_units_are_computed_once_and_cached():
    rec = _record()
    assert rec.units is rec.units
    assert [u.name for u in rec.units] == ["Thing", "mod"]


def test_positions_are_computed_once_and_cached():
    rec = _record()
    assert rec.positions is rec.positions


def test_repeated_analysis_does_not_rewalk_the_tree(monkeypatch):
    import cleanporter.analyze as analyze_mod

    rec = _record()
    mm = firstparty.ModuleMap.from_paths([FIXTURES / "pkg", rec.path])
    resolver = resolver_lib.Resolver(mm)
    resolver.warm(analyze.collect_pairs([rec]))

    calls = {"n": 0}
    real = analyze_mod.iter_units

    def counting(tree, base_pkg):
        calls["n"] += 1
        return real(tree, base_pkg)

    monkeypatch.setattr(analyze_mod, "iter_units", counting)

    analyze.analyze_record(rec, resolver, config.Config())
    analyze.analyze_record(rec, resolver, config.Config())
    assert calls["n"] == 0, "the cached rec.units must be reused, not recomputed"

"""The analysis path must not re-walk a file's tree."""

from __future__ import annotations

from pathlib import Path

import libcst as cst

from cleanporter.analyze import FileRecord, analyze_record, collect_pairs, package_of
from cleanporter.config import Config
from cleanporter.firstparty import ModuleMap
from cleanporter.resolver import Resolver

FIXTURES = Path(__file__).parent / "fixtures"

SOURCE = (
    "from pkg.sub.mod import Thing\n"
    "from pkg.sub import mod\n"
    "x = Thing()\n"
)


def _record() -> FileRecord:
    path = FIXTURES / "pkg" / "a.py"
    mm = ModuleMap.from_paths([FIXTURES / "pkg", path])
    return FileRecord(path, SOURCE, cst.parse_module(SOURCE), package_of(path, mm))


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
    mm = ModuleMap.from_paths([FIXTURES / "pkg", rec.path])
    resolver = Resolver(mm)
    resolver.warm(collect_pairs([rec]))

    calls = {"n": 0}
    real = analyze_mod.iter_units

    def counting(tree, base_pkg):
        calls["n"] += 1
        return real(tree, base_pkg)

    monkeypatch.setattr(analyze_mod, "iter_units", counting)

    analyze_record(rec, resolver, Config())
    analyze_record(rec, resolver, Config())
    assert calls["n"] == 0, "analyze_record must read the cached rec.units"

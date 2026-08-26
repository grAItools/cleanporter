"""Fixer behaviour: rewrites must be exact, or not happen at all."""

from __future__ import annotations

from pathlib import Path

import libcst as cst

from cleanporter.analyze import FileRecord, collect_pairs, package_of
from cleanporter.config import Config
from cleanporter.firstparty import ModuleMap
from cleanporter.model import Status
from cleanporter.resolver import Resolver
from cleanporter.rewrite import FixOutcome, fix_record

FIXTURES = Path(__file__).parent / "fixtures"


def outcome(source: str) -> FixOutcome:
    path = FIXTURES / "pkg" / "a.py"
    mm = ModuleMap.from_paths([FIXTURES / "pkg", path])
    resolver = Resolver(mm)
    rec = FileRecord(path, source, cst.parse_module(source), package_of(path, mm))
    resolver.warm(collect_pairs([rec]))
    return fix_record(rec, resolver, Config())


def test_basic_rewrite_reports_fixed():
    result = outcome("from pkg.sub.mod import Thing\nx = Thing()\n")
    assert result.status == "fixed"
    assert result.fixed == 1
    assert result.source == "from pkg.sub import mod\nx = mod.Thing()\n"
    assert result.blockers == []


def test_compliant_file_reports_clean():
    src = "from pkg.sub import mod\nx = mod.Thing()\n"
    result = outcome(src)
    assert result.status == "clean"
    assert result.source == src


def test_dunder_all_blocks_the_whole_file():
    src = 'from pkg.sub.mod import Thing\n__all__ = ["Thing"]\nx = Thing()\n'
    result = outcome(src)
    assert result.status == "skipped"
    assert result.source == src, "a blocked file must be byte-identical"
    assert [f.status for f in result.blockers] == [Status.SKIPPED]
    assert "string literal" in result.blockers[0].detail


def test_a_blocker_suppresses_otherwise_safe_rewrites_in_the_same_file():
    # 'go' is perfectly safe to rewrite, but the file is all-or-nothing.
    src = (
        "from pkg.sub.mod import Thing, go\n"
        '__all__ = ["Thing"]\n'
        "x = Thing()\n"
        "y = go()\n"
    )
    result = outcome(src)
    assert result.status == "skipped"
    assert result.source == src


def test_blocker_finding_formats_as_cp003():
    src = 'from pkg.sub.mod import Thing\n__all__ = ["Thing"]\n'
    (blocker,) = outcome(src).blockers
    assert blocker.code == "CP003"
    assert "file not rewritten" in blocker.format()

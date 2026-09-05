"""The analysis path must not re-walk a file's tree."""

from __future__ import annotations

import pathlib

import libcst as cst
from libcst import metadata

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


def test_no_skip_walk_without_rules():
    """The feature must be free for everyone not using it.

    `skipped` forces `positions`, which is a metadata resolution per file, so
    a record with no rules must not even ask.
    """
    rec = _record()
    assert rec.skipped is rec.skipped
    assert rec._positions is None, "an empty rule set must not resolve position metadata"


def test_skip_regions_are_computed_once():
    """The source must actually contain a match, or this proves nothing.

    With no matching definition `regions` returns the shared `skip.EMPTY`
    singleton, and `is` then holds however many times the property recomputes
    -- the assertion passes with the cache deleted.
    """
    from cleanporter import config as config_lib
    from cleanporter import skip

    path = FIXTURES / "pkg" / "a.py"
    mm = firstparty.ModuleMap.from_paths([FIXTURES / "pkg", path])
    source = "from pkg.sub.mod import Thing\n\n\ndef holder():\n    return Thing()\n"
    rec = analyze.FileRecord(
        path,
        source,
        cst.parse_module(source),
        analyze.package_of(path, mm),
        skip_rules=config_lib._parse_table({"skip": [{"function": "holder"}]}, FIXTURES).skip,
    )
    assert rec.skipped is not skip.EMPTY, "the fixture must match, or `is` proves nothing"
    assert rec.skipped is rec.skipped


def test_line_lookup_does_not_scan_the_span_list():
    """`covers` is asked once per import and once per name in the file.

    Scanning the spans for each was quadratic: 400 matched definitions in a
    13k-line file cost 1.7s on top of the metadata resolve that happens
    anyway. The index makes it a dict lookup.
    """
    from cleanporter import config as config_lib
    from cleanporter import skip

    source = "".join(f"@deco\ndef f{i}():\n    return {i}\n\n\n" for i in range(200))
    tree = cst.parse_module(source)
    positions = metadata.MetadataWrapper(tree, unsafe_skip_copy=True).resolve(
        metadata.PositionProvider
    )
    rules = config_lib._parse_table({"skip": [{"decorator": "deco"}]}, FIXTURES).skip
    result = skip.regions(tree, positions, rules, ("pkg/a.py",))
    assert len(result.spans) == 200
    assert len(result.lines) == 600, "every line of every span is indexed once"
    assert result.covers(2) is not None
    assert result.covers(len(source.splitlines())) is None

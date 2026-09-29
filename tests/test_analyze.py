"""Analyzer + fixer behaviour over real files."""

from __future__ import annotations

import dataclasses
import pathlib
import shutil
import subprocess
import sys

import libcst as cst

from cleanporter import analyze, config, engine, firstparty, model, project, rewrite
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
    resolver = resolver_lib.Resolver(mm, evidence=resolver_lib.NO_EVIDENCE)
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
    resolver = resolver_lib.Resolver(mm, evidence=resolver_lib.NO_EVIDENCE)
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


def test_scope_first_party_never_classifies_other_imports(tmp_path: pathlib.Path, monkeypatch):
    """Under first-party scope nothing outside the roots reaches the probe.

    Neither `build`'s warm-up nor a ``--fix`` of the file asks about
    ``functools`` or ``os``: out-of-scope imports are passed over without
    being classified, as the configuration docs say.
    """
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", newline="\n")
    (pkg / "mod.py").write_text("THING = 1\n", newline="\n")
    (pkg / "a.py").write_text(
        "import os\nfrom functools import partial\nfrom os import path\n"
        "from pkg.mod import THING\n\nx = partial(THING, path, os)\n",
        newline="\n",
    )
    asked: list[tuple[str, str]] = []
    real = resolver_lib.Resolver._probe

    def recording(self: resolver_lib.Resolver, pairs: list[tuple[str, str]]):
        asked.extend(pairs)
        return real(self, pairs)

    monkeypatch.setattr(resolver_lib.Resolver, "_probe", recording)
    cfg = config.Config(root=tmp_path, scope="first-party")
    built = project.build([pkg], cfg)
    records, resolver, errors = built.records, built.resolver, built.errors
    assert not errors
    for rec in records:
        rewrite.fix_record(rec, resolver, cfg)
        analyze.analyze_record(rec, resolver, cfg)
    assert asked == []

    cfg_all = config.Config(root=tmp_path, scope="all")
    project.build([pkg], cfg_all)
    assert ("functools", "partial") in asked, "the recorder must see an unscoped run's probe"


def test_scope_first_party_still_classifies_a_reexported_third_party_name(
    tmp_path: pathlib.Path, monkeypatch
):
    """``from pkg import path`` is first-party, but only ``os.path`` can say what it is."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("from os import path\n", newline="\n")
    (pkg / "a.py").write_text("from pkg import path\n\nx = path.sep\n", newline="\n")
    asked: list[tuple[str, str]] = []
    real = resolver_lib.Resolver._probe

    def recording(self: resolver_lib.Resolver, pairs: list[tuple[str, str]]):
        asked.extend(pairs)
        return real(self, pairs)

    monkeypatch.setattr(resolver_lib.Resolver, "_probe", recording)
    cfg = config.Config(root=tmp_path, scope="first-party")
    built = project.build([pkg], cfg)
    records, resolver, errors = built.records, built.resolver, built.errors
    assert not errors
    assert asked == [("os", "path")], "only the re-export's origin, batched by warm"
    assert resolver.is_module("pkg", "path") is True
    findings = [f for rec in records for f in analyze.analyze_record(rec, resolver, cfg)]
    assert findings == []


def _analyze_with(source: str, config: config.Config):
    path = FIXTURES / "pkg" / "a.py"
    mm = firstparty.ModuleMap.from_paths([FIXTURES / "pkg", path])
    resolver = resolver_lib.Resolver(mm, evidence=resolver_lib.NO_EVIDENCE)
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
    (pkg / "__init__.py").write_text("", newline="\n")
    (pkg / "display.py").write_text("def dump():\n    return 1\n", newline="\n")
    (pkg / "tool.py").write_text(
        "from pkg.display import dump\n\ndef go():\n    return dump()\n", newline="\n"
    )
    (pkg / "user.py").write_text("from pkg.tool import dump\nx = dump()\n", newline="\n")
    return pkg


def _findings_by_file(pkg: pathlib.Path):
    built = project.build([pkg], config.Config(root=pkg.parent))
    records, resolver = built.records, built.resolver
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
    (pkg / "__init__.py").write_text("", newline="\n")
    (pkg / "display.py").write_text("def dump():\n    return 1\n", newline="\n")
    (pkg / "tool.py").write_text(
        "from pkg.display import dump\n\ndef go():\n    return dump()\n", newline="\n"
    )
    by_file = _findings_by_file(pkg)
    assert [f.code for f in by_file["tool.py"]] == ["CP001"]


def test_a_name_both_imported_and_defined_is_not_protected(tmp_path: pathlib.Path) -> None:
    """A try/except import with a fallback definition survives a rewrite."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", newline="\n")
    (pkg / "display.py").write_text("def dump():\n    return 1\n", newline="\n")
    (pkg / "tool.py").write_text(
        "try:\n    from pkg.display import dump\nexcept ImportError:\n"
        "    def dump():\n        return 0\n",
        newline="\n",
    )
    (pkg / "user.py").write_text("from pkg.tool import dump\nx = dump()\n", newline="\n")
    built = project.build([pkg], config.Config(root=pkg.parent))
    resolver = built.resolver
    assert resolver.is_load_bearing("pkg.tool", "dump") is False


def _shadowed_package_tree(tmp_path: pathlib.Path) -> pathlib.Path:
    """``pkg/`` re-exporting ``helper``, with a stale flat ``pkg.py`` beside it.

    The shape a package picks up when an older single-file release is left in
    place next to a newer packaged one -- the corpus has exactly this in
    ``click_plugins.py`` (2.0dev) beside ``click_plugins/`` (1.1.1.2).
    """
    (tmp_path / "pkg.py").write_text('def helper():\n    return "flat"\n', newline="\n")
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("from pkg.core import helper\n", newline="\n")
    (pkg / "core.py").write_text('def helper():\n    return "from package"\n', newline="\n")
    (tmp_path / "consumer.py").write_text("from pkg import helper\nx = helper()\n", newline="\n")
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
    (pkg / "serialization.py").write_text('MARK = "pkg"\n', newline="\n")
    (pkg / "__init__.py").write_text(init, newline="\n")
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
    assert "both a submodule of 'pkg' and bound in its __init__" in findings[0].detail


def test_the_same_import_is_an_ordinary_violation_without_the_shadow(
    tmp_path: pathlib.Path,
) -> None:
    """Self-reference alone is not the problem; only the competing binding is."""
    pkg = _self_shadowing_tree(tmp_path, "from pkg.serialization import MARK\nx = MARK\n")
    assert [f.code for f in _findings_by_file(pkg)["__init__.py"]] == ["CP001"]


def _lazy_reexport_tree(tmp_path: pathlib.Path, init: str) -> pathlib.Path:
    """``pkg.sub`` is a package whose ``__init__`` decides what ``mod`` means."""
    pkg = tmp_path / "pkg"
    (pkg / "sub").mkdir(parents=True)
    (pkg / "__init__.py").write_text("", newline="\n")
    (pkg / "sub" / "__init__.py").write_text(init, newline="\n")
    (pkg / "sub" / "mod.py").write_text("def mod():\n    return 1\n\n\nOTHER = 2\n", newline="\n")
    (pkg / "consumer.py").write_text("from pkg.sub.mod import OTHER\nx = OTHER\n", newline="\n")
    return pkg


def test_an_import_whose_replacement_a_foreign_init_shadows_is_skipped(
    tmp_path: pathlib.Path,
) -> None:
    """The shadowing ``__init__`` need not be the file being rewritten.

    ``pkg/sub/__init__.py`` binds ``mod`` to a *function*, so the
    replacement ``from pkg.sub import mod`` hands ``consumer.py`` that
    function and ``mod.OTHER`` raises ``AttributeError`` -- code that
    imports, parses and does not run. gt4py's
    ``concat_where/transform_to_as_fieldop`` is this shape exactly.
    """
    pkg = _lazy_reexport_tree(tmp_path, "from pkg.sub.mod import mod\n")
    findings = _findings_by_file(pkg)["consumer.py"]
    assert [f.code for f in findings] == ["CP003"]
    assert "the replacement 'from pkg.sub import mod'" in findings[0].detail
    assert "both a submodule of 'pkg.sub' and bound in its __init__" in findings[0].detail


def test_an_init_that_imports_the_submodule_itself_leaves_the_import_fixable(
    tmp_path: pathlib.Path,
) -> None:
    """``from . import mod`` binds the module, so nothing shadows anything."""
    pkg = _lazy_reexport_tree(tmp_path, "from . import mod\n")
    assert [f.code for f in _findings_by_file(pkg)["consumer.py"]] == ["CP001"]


def test_an_import_from_a_module_this_run_cannot_see_is_unresolved(
    tmp_path: pathlib.Path,
) -> None:
    """Missing evidence is not evidence of an object, so nothing is rewritten.

    Nothing on disk says what ``pkg.missing.mod`` is -- the shape of a run
    pointed at one distribution of a namespace package, where the map answers
    for the whole first-party top-level name, scanned subtree or not. This
    used to be called an object by absence, reported ``CP001`` and then
    declined only by the replacement check; with no evidence either way it is
    ``CP002`` from the start.
    """
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", newline="\n")
    (pkg / "consumer.py").write_text("from pkg.missing.mod import OTHER\nx = OTHER\n", newline="\n")
    findings = _findings_by_file(pkg)["consumer.py"]
    assert [f.code for f in findings] == ["CP002"]
    assert "'pkg.missing.mod' is not a module or package on disk" in (findings[0].detail)


# -- [tool.cleanporter.skip] -------------------------------------------------


def _skip_config(*tables: dict[str, str]) -> config.Config:
    from cleanporter import config as config_lib

    return config_lib._parse_table({"skip": list(tables)}, FIXTURES)


def _analyze_rules(source: str, path: pathlib.Path, cfg: config.Config):
    mm = firstparty.ModuleMap.from_paths([FIXTURES / "pkg", path])
    resolver = resolver_lib.Resolver(mm, evidence=resolver_lib.NO_EVIDENCE)
    rec = analyze.FileRecord(
        path,
        source,
        cst.parse_module(source),
        analyze.package_of(path, mm),
        root=cfg.root,
        skip_rules=cfg.skip,
    )
    resolver.warm(_warm_pairs(rec))
    return analyze.analyze_record(rec, resolver, cfg)


def test_an_import_used_inside_a_skipped_region_is_cp004() -> None:
    source = "from pkg.sub.mod import Thing\n\n\n@field_operator\ndef op():\n    return Thing()\n"
    cfg = _skip_config({"decorator": "field_operator"})
    (finding,) = _analyze_rules(source, FIXTURES / "pkg" / "a.py", cfg)
    assert finding.code == "CP004"
    assert finding.status is model.Status.SKIPPED_BY_CONFIG
    assert "skip rule #1 (decorator='field_operator')" in finding.format()


def test_the_same_import_is_an_ordinary_violation_without_the_rule() -> None:
    source = "from pkg.sub.mod import Thing\n\n\n@field_operator\ndef op():\n    return Thing()\n"
    (finding,) = _analyze_rules(source, FIXTURES / "pkg" / "a.py", config.Config(root=FIXTURES))
    assert finding.code == "CP001"


def test_an_import_outside_every_region_is_still_a_violation() -> None:
    source = (
        "from pkg.sub.mod import Thing\n\n\n@field_operator\ndef op():\n    return 1\n"
        "\n\nx = Thing()\n"
    )
    cfg = _skip_config({"decorator": "field_operator"})
    (finding,) = _analyze_rules(source, FIXTURES / "pkg" / "a.py", cfg)
    assert finding.code == "CP001"


def test_a_file_rule_reports_every_import_as_cp004() -> None:
    source = "from pkg.sub.mod import Thing\nfrom pkg.sub.mod import go\nx = Thing(go)\n"
    cfg = _skip_config({"file": r".*a\.py"})
    findings = _analyze_rules(source, FIXTURES / "pkg" / "a.py", cfg)
    assert [f.code for f in findings] == ["CP004", "CP004"]


def test_a_star_import_inside_a_skipped_region_is_cp004_not_cp003() -> None:
    source = "def outer():\n    from pkg.sub.mod import *\n"
    cfg = _skip_config({"function": "outer"})
    (finding,) = _analyze_rules(source, FIXTURES / "pkg" / "a.py", cfg)
    assert finding.code == "CP004"


def test_a_skipped_file_still_contributes_reexport_evidence(tmp_path: pathlib.Path) -> None:
    """The difference between `skip` and `exclude`, and the reason for it.

    `conftest.py` is skipped, but it must still count as an importer of
    `helpers.THING` -- otherwise `helpers`'s own import of the name looks
    unused to the re-export guard and becomes rewritable, deleting the
    attribute `conftest.py` reads.
    """
    root = tmp_path / "src"
    (root / "demo").mkdir(parents=True)
    (root / "demo" / "__init__.py").write_text("", encoding="utf-8", newline="\n")
    (root / "demo" / "origin.py").write_text("THING = 1\n", encoding="utf-8", newline="\n")
    (root / "demo" / "helpers.py").write_text(
        "from demo.origin import THING\nx = THING\n", encoding="utf-8", newline="\n"
    )
    (root / "demo" / "conftest.py").write_text(
        "from demo.helpers import THING\ny = THING\n", encoding="utf-8", newline="\n"
    )
    from cleanporter import config as config_lib

    cfg = config_lib._parse_table({"skip": [{"file": r".*conftest\.py"}]}, tmp_path)
    built = project.build([root], cfg)
    records, resolver = built.records, built.resolver
    assert resolver.is_load_bearing("demo.helpers", "THING"), (
        "the skipped file's import must still count as a use"
    )
    helpers = next(r for r in records if r.path.name == "helpers.py")
    assert [f.code for f in analyze.analyze_record(helpers, resolver, cfg)] == ["CP003"]


def test_cp004_is_not_emitted_for_imports_that_were_never_violations() -> None:
    """`CP004` replaces a finding; it must not invent one.

    The count is what the docs offer as the way to see how much a rule
    swallowed, so padding it with a compliant module import and an exempt
    `typing` name degrades exactly that signal.
    """
    source = (
        "from typing import Any\nfrom pkg.sub import mod\nfrom pkg.sub.mod import Thing\n"
        "x: Any = mod\ny = Thing()\n"
    )
    cfg = _skip_config({"file": r".*a\.py"})
    findings = _analyze_rules(source, FIXTURES / "pkg" / "a.py", cfg)
    assert [(f.code, f.name) for f in findings] == [("CP004", "Thing")]


def test_a_skipped_unresolvable_import_is_cp004_not_cp002() -> None:
    """A skip replaces whichever finding the unit would have produced."""
    source = "from definitely_missing_pkg_xyz import thing\nx = thing\n"
    cfg = _skip_config({"file": r".*a\.py"})
    (finding,) = _analyze_rules(source, FIXTURES / "pkg" / "a.py", cfg)
    assert finding.code == "CP004"


def test_build_keeps_the_encoding_and_bytes_and_reports_an_undecodable_file(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_bytes(b"")
    crlf = b"from os.path import join\r\nprint(join('a'))\r\n"
    (pkg / "crlf.py").write_bytes(crlf)
    (pkg / "latin.py").write_bytes(b"# coding: latin-1\nx = '\xe9'\n")
    (pkg / "bad.py").write_bytes(b"x = '\xe9'\n")

    built = project.build([pkg], config.Config(root=tmp_path))

    records, errors = built.records, built.errors

    by_name = {rec.path.name: rec for rec in records}
    assert sorted(by_name) == ["__init__.py", "crlf.py", "latin.py"]
    assert by_name["crlf.py"].source == crlf.decode()  # no newline translation
    assert by_name["crlf.py"].raw == crlf
    assert by_name["latin.py"].encoding == "iso-8859-1"
    assert by_name["latin.py"].source == "# coding: latin-1\nx = '\xe9'\n"
    [error] = errors
    assert error.path == pkg / "bad.py"
    assert error.line == 1
    assert "cannot decode file" in error.detail


# -- check and fix agree -------------------------------------------------------
def test_scope_first_party_neither_reports_nor_rewrites_stdlib():
    """B1: check passed over `from os.path import join`, and `--fix` rewrote it."""
    src = 'from os.path import join\nprint(join("a", "b"))\n'
    cfg = config.Config(scope="first-party")
    path = FIXTURES / "pkg" / "a.py"
    mm = firstparty.ModuleMap.from_paths([FIXTURES / "pkg", path])
    resolver = resolver_lib.Resolver(mm, evidence=resolver_lib.NO_EVIDENCE)
    rec = _record(src, path, mm)
    resolver.warm(_warm_pairs(rec))
    assert analyze.analyze_record(rec, resolver, cfg) == []
    result = rewrite.fix_record(rec, resolver, cfg)
    assert (result.status, result.source) == ("clean", src)


class _RecordingFixer(rewrite._Fixer):
    """A `_Fixer` that remembers every import it chose to rewrite.

    Keyed ``(line, parent, name)`` -- the same key `_key` gives a finding --
    so two imports of one name (``Widget`` and ``Widget as Widget``) are
    compared separately rather than collapsing into one.
    """

    def __init__(self, rec, resolver, cfg):
        super().__init__(rec, resolver, cfg)
        self.rewritten: set[tuple[int, str, str]] = set()

    def _partition(self, imp, parent, scope):
        keep, fix = super()._partition(imp, parent, scope)
        line = self._line_of(imp)
        self.rewritten |= {(line, parent, name) for name, _asname in fix}
        return keep, fix


#: One package reaching every finding the decision can produce -- stdlib and
#: first-party objects, modules, exemptions, a wildcard, an unresolvable and
#: an unanchorable parent, relative, aliased, re-exported, load-bearing,
#: named-by-a-string,
#: never-read, unreachable-replacement, no-relative-spelling, function-local, TYPE_CHECKING-gated,
#: skip-pinned, skip-covered and guard-blocked imports.
_AGREEMENT_TREE = {
    "__init__.py": "from app.helpers import THING as THING\n",
    "helpers.py": "THING = 1\n\ndef go():\n    return 2\n\nclass Widget:\n    pass\n",
    "sub/__init__.py": "",
    "sub/tool.py": "def dump():\n    return 3\n",
    # `app.shadow` binds `mod` to an int while `app/shadow/mod.py` exists, so
    # the replacement `from app.shadow import mod` cannot be trusted.
    "shadow/__init__.py": "mod = 1\n",
    "shadow/mod.py": "def thing():\n    return 4\n",
    "far.py": "from app.shadow.mod import thing\n\nx = thing()\n",
    "loose.py": "from ..... import nothing\n\nx = nothing\n",
    # `app` is top-level, so no relative import can name it: the replacement
    # for `from . import THING` would have to be the absolute `import app`.
    "near.py": "from . import THING\n\nx = THING\n",
    "relay.py": "from app.helpers import go\n\ndef run():\n    return go()\n",
    "consumer.py": "from app.relay import go\n\nvalue = go()\n",
    # Nothing imports `go` from `app.named`, but a string names it by path.
    "named.py": "from app.helpers import go\n\ndef run():\n    return go()\n",
    "patcher.py": 'TARGET = "app.named.go"\n',
    "skipped.py": (
        "from app.helpers import Widget\n"
        "\n"
        "def kept():\n"
        "    from app.helpers import go\n"
        "    return Widget(), go()\n"
    ),
    "blocked.py": 'from app.helpers import go\n\n__all__ = ["go"]\nx = go()\n',
    # Files whose only `CP001` is not a module-level import, or is one the
    # fixer turns into a `CP003`: `rewrite._has_candidates` must still see it.
    "nested.py": "def later():\n    from app.helpers import go\n\n    return go()\n",
    "gated.py": (
        "from typing import TYPE_CHECKING\n"
        "\n"
        "if TYPE_CHECKING:\n"
        "    from app.helpers import Widget\n"
        "\n"
        "def make(w: 'Widget') -> None:\n"
        "    pass\n"
    ),
    "unread.py": "from app.helpers import go\n",
    "user.py": (
        "from __future__ import annotations\n"
        "from typing import TYPE_CHECKING, Any\n"
        "from os.path import join\n"
        "from collections import OrderedDict as OD\n"
        "from app.helpers import THING, go, Widget\n"
        "from app import helpers\n"
        "from app.sub.tool import dump\n"
        "from app.sub.tool import *\n"
        "from definitely_missing_pkg_xyz import thing\n"
        "from .helpers import go as g2\n"
        "from app.helpers import Widget as Widget\n"
        "from json import dumps\n"
        "if TYPE_CHECKING:\n"
        "    from app.sub.tool import dump as d2\n"
        "\n"
        "def f() -> tuple[object, ...]:\n"
        "    from os.path import basename\n"
        "    return (basename('x'), OD(), join('a'), THING, go(), Widget, dump(), thing,\n"
        "            g2(), helpers)\n"
        "\n"
        "x: Any = 1\n"
        "y: d2 = 2\n"
    ),
}

_AGREEMENT_CONFIGS: dict[str, dict[str, object]] = {
    "default": {},
    "first-party": {"scope": "first-party"},
    "exempt": {"exempt_names": ["go"], "exempt_modules": ["os"]},
    "skip": {"skip": [{"function": "kept"}]},
    "skip-file": {"skip": [{"file": r".*user\.py"}]},
    "first-party+skip": {"scope": "first-party", "skip": [{"function": "kept"}]},
    # `app`'s parent declared as a root: `near.py`'s `from . import THING`
    # may then be spelled `import app`, and is a `CP001` in both modes.
    "declared-root": {"source_roots": ["."]},
}

_Key = tuple[int, str, str]


def _key(finding: model.Finding) -> _Key:
    return (finding.line, finding.parent, finding.name)


def _violations(findings: list[model.Finding]) -> set[_Key]:
    return {_key(f) for f in findings if f.status is model.Status.VIOLATION}


@dataclasses.dataclass
class _Agreement:
    rewritten: set[_Key]
    #: Plain ``check``: nothing is known about never-read names.
    check: list[model.Finding]
    #: ``--fix``: the fixer's never-read names handed back.
    fix_mode: list[model.Finding]


def _agreement(tmp_path: pathlib.Path, table: dict[str, object]) -> dict[str, _Agreement]:
    """Per file (relative to the package), what each mode did with each import."""
    root = tmp_path / "app"
    for rel, text in _AGREEMENT_TREE.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8", newline="\n")
    cfg = config._parse_table(table, tmp_path)
    built = project.build([root], cfg)
    records, resolver, errors = built.records, built.resolver, built.errors
    assert errors == ()
    out: dict[str, _Agreement] = {}
    for rec in records:
        fixer = _RecordingFixer(rec, resolver, cfg)
        cst.MetadataWrapper(rec.tree, unsafe_skip_copy=True).visit(fixer)
        out[rec.path.relative_to(root).as_posix()] = _Agreement(
            fixer.rewritten,
            analyze.analyze_record(rec, resolver, cfg),
            analyze.analyze_record(rec, resolver, cfg, frozenset(fixer.unread)),
        )
    assert sorted(out) == sorted(_AGREEMENT_TREE)
    return out


def test_check_and_fix_agreement_the_fixer_rewrites_only_cp001s(tmp_path):
    """One decision drives both modes, so they cannot drift again.

    Across every config shape that changes the decision -- scope, exemptions,
    skip rules -- every import the fixer chose to rewrite is one plain
    ``check`` reports as `CP001`, and with the fixer's never-read names handed
    back (as ``--fix`` does) the two sets are equal. Nothing check calls
    compliant, unresolved or declined is ever rewritten.
    """
    for label, table in _AGREEMENT_CONFIGS.items():
        for name, a in _agreement(tmp_path / label, table).items():
            check, fix_mode = _violations(a.check), _violations(a.fix_mode)
            assert a.rewritten <= check, (label, name, a.rewritten - check)
            assert a.rewritten == fix_mode, (label, name, a.rewritten ^ fix_mode)


def _rung(finding: model.Finding) -> str:
    """Which outcome of `analyze.Decider.decide` produced *finding*."""
    if finding.status is not model.Status.SKIPPED:
        if finding.status is model.Status.UNRESOLVED and finding.detail == analyze._UNANCHORED:
            return "CP002 unanchored"
        return finding.code
    known = {
        analyze._WILDCARD: "CP003 wildcard",
        analyze._REEXPORT: "CP003 re-export",
        analyze._UNREAD: "CP003 never read",
    }
    if finding.detail in known:
        return known[finding.detail]
    if finding.detail.startswith("`from . import` names the package"):
        return "CP003 no relative spelling"
    if finding.detail.startswith("another file imports"):
        return "CP003 load-bearing"
    if "is named by the string" in finding.detail:
        return "CP003 named by a string"
    if finding.detail.startswith("the replacement"):
        return "CP003 unreachable"
    return "CP003 other"


def test_the_agreement_tree_reaches_every_outcome(tmp_path):
    """Guard for the test above: its tree must keep exercising every outcome.

    An edit that drops a case would otherwise leave the agreement test passing
    over less than it claims to.
    """
    runs = {label: _agreement(tmp_path / label, t) for label, t in _AGREEMENT_CONFIGS.items()}
    rungs = {
        _rung(f) for files in runs.values() for a in files.values() for f in a.check + a.fix_mode
    }
    assert rungs == {
        "CP001",
        "CP002",
        "CP002 unanchored",
        "CP003 wildcard",
        "CP003 re-export",
        "CP003 never read",
        "CP003 load-bearing",
        "CP003 named by a string",
        "CP003 unreachable",
        "CP003 no relative spelling",
        "CP004",
    }

    # CP004 both ways: pinned by name at module level, and covered by a rule
    # (the import inside the skipped function) -- plus a whole-file rule.
    skipped = runs["skip"]["skipped.py"].fix_mode
    assert [(f.line, f.code) for f in skipped] == [(1, "CP004"), (4, "CP004")]
    assert {f.code for f in runs["skip-file"]["user.py"].fix_mode} == {"CP004"}

    # The three no-finding outcomes: a module, an exemption, out of scope.
    def names(label: str) -> set[tuple[str, str]]:
        return {(f.parent, f.name) for a in runs[label].values() for f in a.check}

    assert ("app", "helpers") not in names("default")
    assert ("app.helpers", "go") in names("default")
    assert ("app.helpers", "go") not in names("exempt")
    assert ("os.path", "join") in names("default")
    assert ("os.path", "join") not in names("first-party")


def test_a_declared_root_turns_the_unspellable_import_into_a_cp001(tmp_path):
    """The one config that changes `near.py`'s answer, in both modes at once."""
    inferred = _agreement(tmp_path / "inferred", {})["near.py"]
    assert [_rung(f) for f in inferred.check] == ["CP003 no relative spelling"]
    assert "is where Python imports it from, declaring it with --root" in inferred.check[0].detail
    assert inferred.rewritten == set()
    declared = _agreement(tmp_path / "declared", {"source_roots": ["."]})["near.py"]
    assert [f.code for f in declared.check] == ["CP001"]
    assert [f.code for f in declared.fix_mode] == ["CP001"]
    assert declared.rewritten == {(1, "app", "THING")}


def test_the_agreement_check_is_not_vacuous(tmp_path):
    user = _agreement(tmp_path / "all", {})["user.py"]
    assert {(3, "os.path", "join"), (5, "app.helpers", "go")} <= user.rewritten
    user = _agreement(tmp_path / "fp", {"scope": "first-party"})["user.py"]
    assert not {k for k in user.rewritten | _violations(user.check) if k[1] == "os.path"}
    assert (5, "app.helpers", "go") in user.rewritten


# -- the fixer runs only where there is something to fix -----------------------
def _write_agreement_tree(root: pathlib.Path) -> None:
    for rel, text in _AGREEMENT_TREE.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8", newline="\n")


def _snapshot(root: pathlib.Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*.py"))}


def test_skipping_files_without_a_cp001_changes_no_output(tmp_path, monkeypatch):
    """The short-circuit in `rewrite.fix_record` is an optimisation, never a decision.

    Every config shape the agreement tests use, in both modes that run the
    fixer, over the tree that reaches every outcome: run once as shipped, and
    once with `rewrite._has_candidates` forced true -- scope analysis on every
    file, as before it existed. The results (findings, patches, warnings) and
    what is left on disk are identical. And it did fire, on some files and
    not on others, or this would prove nothing.
    """
    real = rewrite._has_candidates
    answers: list[bool] = []

    def recording(
        rec: analyze.FileRecord, resolver: resolver_lib.Resolver, cfg: config.Config
    ) -> bool:
        answer = real(rec, resolver, cfg)
        answers.append(answer)
        return answer

    def always(
        _rec: analyze.FileRecord, _resolver: resolver_lib.Resolver, _cfg: config.Config
    ) -> bool:
        return True

    monkeypatch.chdir(tmp_path)  # the patches' headers are relative to it
    for label, table in _AGREEMENT_CONFIGS.items():
        for mode in (engine.Mode.DIFF, engine.Mode.FIX):
            runs: list[tuple[engine.RunResult, dict[str, bytes]]] = []
            for index, gate in enumerate((recording, always)):
                # The same path both times, so the results compare as they are.
                work = tmp_path / label / mode.value
                if index:
                    shutil.rmtree(work)
                _write_agreement_tree(work / "app")
                monkeypatch.setattr(rewrite, "_has_candidates", gate)
                result = engine.run([work / "app"], config._parse_table(table, work), mode)
                runs.append((result, _snapshot(work / "app")))
            assert runs[0] == runs[1], (label, mode)
    assert set(answers) == {True, False}


def test_a_file_without_a_cp001_gets_no_scope_analysis(tmp_path, monkeypatch):
    """The metadata the fixer needs is built for exactly the files with a `CP001`.

    Those are the files ``check`` reports one in, wherever the import sits --
    ``nested.py``'s only one is function-local and ``gated.py``'s is under
    ``if TYPE_CHECKING:`` -- and including ``unread.py``, whose only `CP001`
    becomes a `CP003` under ``--fix``: only the fixer's scope analysis can
    find a name nothing reads.
    """
    root = tmp_path / "app"
    _write_agreement_tree(root)
    cfg = config.Config(root=tmp_path)
    checked = engine.run([root], cfg)
    with_cp001 = {f.path for f in checked.findings if f.code == "CP001"}
    assert {root / "nested.py", root / "gated.py", root / "unread.py"} <= with_cp001

    # `cst.MetadataWrapper` is handed only a tree, so the file it belongs to
    # is noted on the way into `fix_record`.
    real_fix, real_wrapper = rewrite.fix_record, cst.MetadataWrapper
    fixing: list[pathlib.Path] = []
    wrapped: list[pathlib.Path] = []

    def noting(
        rec: analyze.FileRecord, resolver: resolver_lib.Resolver, cfg: config.Config
    ) -> rewrite.FixOutcome:
        fixing.append(rec.path)
        return real_fix(rec, resolver, cfg)

    def spy(module: cst.Module, *, unsafe_skip_copy: bool = False) -> cst.MetadataWrapper:
        wrapped.append(fixing[-1])
        return real_wrapper(module, unsafe_skip_copy=unsafe_skip_copy)

    monkeypatch.setattr(rewrite, "fix_record", noting)
    monkeypatch.setattr(cst, "MetadataWrapper", spy)
    engine.run([root], cfg, engine.Mode.DIFF)
    assert len(fixing) == len(_AGREEMENT_TREE), "the fixer is still asked about every file"
    assert len(wrapped) == len(set(wrapped)), "one metadata build per file"
    assert set(wrapped) == with_cp001
    assert 0 < len(with_cp001) < len(_AGREEMENT_TREE)


# -- a declared root spells a top-level package's own import absolutely --------
def _top_level_tree(root: pathlib.Path, package: str = "toppkg") -> pathlib.Path:
    files = {
        "__init__.py": "VERSION = '1.0'\n\ndef helper():\n    return 'h'\n",
        "cli.py": "from . import VERSION, helper\n\ndef main():\n    return VERSION, helper()\n",
        "sub/__init__.py": "",
        "sub/deep.py": "from .. import helper\n\ndef run():\n    return helper()\n",
    }
    for rel, text in files.items():
        path = root / package / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    return root / package


def test_a_declared_root_lets_fix_rewrite_a_top_level_package_import(tmp_path):
    pkg = _top_level_tree(tmp_path)
    cfg = config._parse_table({"source_roots": ["."]}, tmp_path)
    checked = engine.run([pkg], cfg)
    assert {(f.path.name, f.name, f.code) for f in checked.findings} == {
        ("cli.py", "VERSION", "CP001"),
        ("cli.py", "helper", "CP001"),
        ("deep.py", "helper", "CP001"),
    }
    fixed = engine.run([pkg], cfg, engine.Mode.FIX)
    assert fixed.exit_code() == 0, fixed.findings
    assert (pkg / "cli.py").read_text(encoding="utf-8") == (
        "import toppkg\n\ndef main():\n    return toppkg.VERSION, toppkg.helper()\n"
    )
    assert (pkg / "sub" / "deep.py").read_text(encoding="utf-8") == (
        "import toppkg\n\ndef run():\n    return toppkg.helper()\n"
    )
    # And the rewritten package still runs.
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import toppkg.cli, toppkg.sub.deep; print(toppkg.cli.main(), toppkg.sub.deep.run())",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.stdout.strip() == "('1.0', 'h') h"


def test_an_inferred_root_keeps_the_top_level_package_import(tmp_path):
    pkg = _top_level_tree(tmp_path)
    cfg = config.Config(root=tmp_path)
    before = {p: p.read_bytes() for p in pkg.rglob("*.py")}
    for mode in (engine.Mode.CHECK, engine.Mode.FIX):
        result = engine.run([pkg], cfg, mode)
        assert {f.code for f in result.findings} == {"CP003"}, mode
    assert {p: p.read_bytes() for p in pkg.rglob("*.py")} == before


def test_a_declared_root_never_spells_a_stdlib_name(tmp_path):
    """`import io` is the standard library's, whatever the declared root holds."""
    pkg = _top_level_tree(tmp_path, package="io")
    cfg = config._parse_table({"source_roots": ["."]}, tmp_path)
    before = {p: p.read_bytes() for p in pkg.rglob("*.py")}
    for mode in (engine.Mode.CHECK, engine.Mode.FIX):
        result = engine.run([pkg], cfg, mode)
        found = [f for f in result.findings if f.path.name == "cli.py"]
        assert {f.code for f in found} == {"CP003"}, mode
        assert all("standard library's 'io'" in f.detail for f in found)
    assert {p: p.read_bytes() for p in pkg.rglob("*.py")} == before


# -- a CP001's advice spells the import as the fixer would ---------------------
def _messages(root: pathlib.Path, files: dict[str, str], **table: object) -> dict[str, str]:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8", newline="\n")
    result = engine.run([root], config._parse_table(dict(table), root))
    return {
        f"{f.path.relative_to(root).as_posix()}:{f.name}": f.message
        for f in result.findings
        if f.code == "CP001"
    }


def test_a_relative_cp001_advises_the_relative_replacement(tmp_path):
    messages = _messages(
        tmp_path,
        {
            "app/__init__.py": "",
            "app/helpers.py": "THING = 1\n",
            "app/user.py": "from .helpers import THING\n\nx = THING\n",
            "app/sub/__init__.py": "VALUE = 2\n",
            "app/sub/tool.py": "def dump():\n    return 3\n",
            "app/sub/deep.py": (
                "from . import VALUE\nfrom ..sub.tool import dump\n\nx = VALUE, dump()\n"
            ),
            "app/plain.py": "from app.helpers import THING\n\nx = THING\n",
        },
    )
    assert messages == {
        "app/user.py:THING": (
            "imports object 'THING' from module 'app.helpers'; "
            "import the module ('from . import helpers') and use 'helpers.THING'"
        ),
        "app/sub/deep.py:VALUE": (
            "imports object 'VALUE' from module 'app.sub'; "
            "import the module ('from .. import sub') and use 'sub.VALUE'"
        ),
        "app/sub/deep.py:dump": (
            "imports object 'dump' from module 'app.sub.tool'; "
            "import the module ('from ..sub import tool') and use 'tool.dump'"
        ),
        # An absolute import's text is what it always was.
        "app/plain.py:THING": (
            "imports object 'THING' from module 'app.helpers'; "
            "import the module and use 'helpers.THING'"
        ),
    }


def test_the_advice_never_names_the_module_the_inferred_root_implies(tmp_path):
    """`analytics/` is a namespace directory inferred as a root.

    The absolute name is then `io.readers`, and `from io import readers` is
    the standard library: the advice stays relative, as the fixer's would.
    """
    messages = _messages(
        tmp_path,
        {
            "analytics/io/__init__.py": "from .readers import read\n\nx = read\n",
            "analytics/io/readers.py": "def read():\n    return 1\n",
        },
    )
    assert messages == {
        "analytics/io/__init__.py:read": (
            "imports object 'read' from module 'io.readers'; "
            "import the module ('from . import readers') and use 'readers.read'"
        )
    }


def test_a_declared_root_advises_the_absolute_package_import(tmp_path):
    pkg = _top_level_tree(tmp_path)
    result = engine.run([pkg], config._parse_table({"source_roots": ["."]}, tmp_path))
    deep = [f.message for f in result.findings if f.path.name == "deep.py"]
    assert deep == [
        (
            "imports object 'helper' from module 'toppkg'; "
            "import the module ('import toppkg') and use 'toppkg.helper'"
        )
    ]


# -- statement order after a star import is not read ---------------------------
def test_a_circular_read_past_a_star_import_is_cp002_and_never_rewritten(tmp_path):
    """A definition after ``from os import *`` is not what a circular importer sees.

    ``app.c`` runs in the middle of ``app.m`` (``from app import c``), so its
    ``path`` is the star's ``os.path``; rewriting it to ``m.path`` reads the
    function instead, and ``path.join`` raises AttributeError.
    """
    files = {
        "app/__init__.py": "",
        "app/m.py": 'from os import *\nfrom app import c\ndef path():\n    return "func"\n',
        "app/c.py": 'from app.m import path\ndef f():\n    return path.join("a", "b")\n',
    }
    for rel, text in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text, encoding="utf-8", newline="\n")
    cfg = config.Config(root=tmp_path)
    for mode in (engine.Mode.CHECK, engine.Mode.FIX):
        result = engine.run([tmp_path / "app"], cfg, mode)
        found = {(f.path.name, f.code) for f in result.findings if f.name == "path"}
        assert found == {("c.py", "CP002")}, mode
    assert (tmp_path / "app" / "c.py").read_text(encoding="utf-8") == files["app/c.py"]


# -- a declared root is taken at its word only when it is structurally sound ----
def _write_tree(root: pathlib.Path, files: dict[str, str]) -> dict[pathlib.Path, bytes]:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8", newline="\n")
    return {root / rel: (root / rel).read_bytes() for rel in files}


def _stays_cp003(root: pathlib.Path, table: dict[str, object], file: str) -> list[str]:
    """Check and fix both keep *file*'s unspellable import; every file byte-identical."""
    before = {p: p.read_bytes() for p in root.rglob("*.py")}
    cfg = config._parse_table(table, root)
    details: list[str] = []
    for mode in (engine.Mode.CHECK, engine.Mode.FIX):
        result = engine.run([root / "src" if (root / "src").is_dir() else root], cfg, mode)
        found = [f for f in result.findings if f.path == root / file]
        assert [f.code for f in found] == ["CP003"], (mode, found)
        details.append(found[0].detail)
    assert {p: p.read_bytes() for p in root.rglob("*.py")} == before
    return details


def test_a_root_declared_inside_the_package_keeps_the_cp003(tmp_path):
    """``source_roots = ["src/toppkg"]``: `import utils` could be any top-level `utils`."""
    _write_tree(
        tmp_path,
        {
            "src/toppkg/__init__.py": "from .utils import cli\n",
            "src/toppkg/utils/__init__.py": 'def helper():\n    return "mine"\n',
            "src/toppkg/utils/cli.py": "from . import helper\ndef main():\n    return helper()\n",
            "elsewhere/utils/__init__.py": 'def helper():\n    return "IMPOSTOR"\n',
        },
    )
    (detail, _) = _stays_cp003(
        tmp_path, {"source_roots": ["src/toppkg"]}, "src/toppkg/utils/cli.py"
    )
    assert "declaring it" not in detail


def test_nested_roots_keep_the_cp003(tmp_path):
    """``src`` declared, and a ``tests`` package makes the repository root a root too."""
    _write_tree(
        tmp_path,
        {
            "src/toppkg/__init__.py": "def helper():\n    return 1\n",
            "src/toppkg/cli.py": "from . import helper\ndef main():\n    return helper()\n",
            "tests/__init__.py": "",
            "tests/test_cli.py": "def test_it():\n    pass\n",
        },
    )
    before = {p: p.read_bytes() for p in tmp_path.rglob("*.py")}
    cfg = config._parse_table({"source_roots": ["src"]}, tmp_path)
    for mode in (engine.Mode.CHECK, engine.Mode.FIX):
        result = engine.run([tmp_path / "src", tmp_path / "tests"], cfg, mode)
        found = [f.code for f in result.findings if f.path.name == "cli.py"]
        assert found == ["CP003"], mode
    assert {p: p.read_bytes() for p in tmp_path.rglob("*.py")} == before


def test_a_package_two_declared_roots_hold_keeps_the_cp003(tmp_path):
    files = {
        f"{side}/toppkg/{rel}": text
        for side in ("a", "b")
        for rel, text in {
            "__init__.py": f'def helper():\n    return "{side}"\n',
            "cli.py": "from . import helper\ndef main():\n    return helper()\n",
        }.items()
    }
    _write_tree(tmp_path, files)
    _stays_cp003(tmp_path, {"source_roots": ["a", "b"]}, "a/toppkg/cli.py")


def test_the_package_own_init_keeps_the_cp003_under_a_declared_root(tmp_path):
    _write_tree(
        tmp_path,
        {
            "src/toppkg/__init__.py": (
                "def helper():\n    return 1\n\nfrom . import helper as again\n\nx = again()\n"
            ),
        },
    )
    _stays_cp003(tmp_path, {"source_roots": ["src"]}, "src/toppkg/__init__.py")


def test_the_hint_names_the_inferred_root_and_not_for_a_stdlib_name(tmp_path):
    pkg = _top_level_tree(tmp_path / "plain")
    result = engine.run([pkg], config.Config(root=tmp_path / "plain"))
    detail = next(f.detail for f in result.findings if f.path.name == "deep.py")
    assert f"(if '{(tmp_path / 'plain').resolve()}' is where Python imports it from" in detail
    assert detail.endswith("lets --fix write 'import toppkg')")

    pkg = _top_level_tree(tmp_path / "std", package="cgi")  # stdlib only before 3.13
    result = engine.run([pkg], config.Config(root=tmp_path / "std"))
    detail = next(f.detail for f in result.findings if f.path.name == "deep.py")
    assert "standard library's 'cgi'" in detail
    assert "declaring" not in detail


_R10 = {
    "lib/__init__.py": "from .vendor.toppkg import cli\n",
    "lib/vendor/toppkg/__init__.py": 'def helper():\n    return "vendored"\n',
    "lib/vendor/toppkg/cli.py": "from . import helper\ndef main():\n    return helper()\n",
    "site/toppkg/__init__.py": 'def helper():\n    return "OTHER COPY"\n',
}


def test_a_declared_root_below_a_package_keeps_the_cp003_whatever_the_run_is_given(tmp_path):
    """``source_roots = ["lib/vendor"]`` under a ``lib/__init__.py``.

    ``toppkg`` is really ``lib.vendor.toppkg``; ``import toppkg`` would bind
    whichever ``toppkg`` is on ``sys.path``. Refused in a run over the
    subtree -- which never reads ``lib/__init__.py`` -- as in one over all.
    """
    for label, paths in (("subtree", ["lib/vendor"]), ("all", ["."])):
        work = tmp_path / label
        _write_tree(work, _R10)
        before = {p: p.read_bytes() for p in work.rglob("*.py")}
        cfg = config._parse_table({"source_roots": ["lib/vendor"]}, work)
        for mode in (engine.Mode.CHECK, engine.Mode.FIX):
            result = engine.run([work / p for p in paths], cfg, mode)
            found = [f.code for f in result.findings if f.path.name == "cli.py"]
            assert found == ["CP003"], (label, mode, result.findings)
        assert {p: p.read_bytes() for p in work.rglob("*.py")} == before, label


def test_a_package_own_init_has_its_own_reason(tmp_path):
    _write_tree(
        tmp_path,
        {
            "src/toppkg/__init__.py": (
                "def helper():\n    return 1\n\nfrom . import helper as again\n\nx = again()\n"
            ),
        },
    )
    tables: list[dict[str, object]] = [{}, {"source_roots": ["src"]}]
    for table in tables:
        detail = _stays_cp003(tmp_path, table, "src/toppkg/__init__.py")[0]
        assert "'toppkg''s own __init__ names the package itself" in detail, table
        assert "declaring" not in detail

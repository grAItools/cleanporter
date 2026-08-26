"""Layered resolution and the reasons attached to unresolved verdicts."""

from __future__ import annotations

from pathlib import Path

from cleanporter.firstparty import ModuleMap
from cleanporter.resolver import Resolver


def _pkg(tmp_path: Path) -> Path:
    root = tmp_path / "src"
    (root / "amb").mkdir(parents=True)
    (root / "amb" / "__init__.py").write_text("", encoding="utf-8")
    (root / "amb" / "mod.py").write_text("Q = 1\n", encoding="utf-8")
    return root


def test_first_party_module_and_object(tmp_path):
    r = Resolver(ModuleMap([_pkg(tmp_path)]))
    assert r.is_module("amb", "mod") is True
    assert r.is_module("amb", "Nope") is False


def test_stdlib_falls_through_to_the_probe(tmp_path):
    r = Resolver(ModuleMap([_pkg(tmp_path)]))
    assert r.is_module("os", "path") is True
    assert r.is_module("collections", "OrderedDict") is False


def test_ambiguous_is_unresolved_with_an_explanatory_reason(tmp_path):
    root = _pkg(tmp_path)
    (root / "amb" / "__init__.py").write_text('mod = "shadow"\n', encoding="utf-8")
    r = Resolver(ModuleMap([root]))
    assert r.is_module("amb", "mod") is None
    assert "both a submodule" in r.reason("amb", "mod")


def test_unimportable_parent_is_unresolved_with_its_own_reason(tmp_path):
    r = Resolver(ModuleMap([_pkg(tmp_path)]))
    assert r.is_module("definitely_missing_pkg_xyz", "thing") is None
    assert "not importable" in r.reason("definitely_missing_pkg_xyz", "thing")


def test_warm_batches_and_matches_individual_lookups(tmp_path):
    r = Resolver(ModuleMap([_pkg(tmp_path)]))
    pairs = [("amb", "mod"), ("collections", "OrderedDict"), ("os", "path")]
    r.warm(pairs)
    assert [r.is_module(p, n) for p, n in pairs] == [True, False, True]


def test_warm_then_reason_agree_for_an_ambiguous_pair(tmp_path):
    root = _pkg(tmp_path)
    (root / "amb" / "__init__.py").write_text('mod = "shadow"\n', encoding="utf-8")
    r = Resolver(ModuleMap([root]))
    r.warm([("amb", "mod")])
    assert r.is_module("amb", "mod") is None
    assert "both a submodule" in r.reason("amb", "mod")

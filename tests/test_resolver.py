"""Layered resolution and the reasons attached to unresolved verdicts."""

from __future__ import annotations

import pathlib

from cleanporter import firstparty, resolver


def _pkg(tmp_path: pathlib.Path) -> pathlib.Path:
    root = tmp_path / "src"
    (root / "amb").mkdir(parents=True)
    (root / "amb" / "__init__.py").write_text("", encoding="utf-8")
    (root / "amb" / "mod.py").write_text("Q = 1\n", encoding="utf-8")
    return root


def test_first_party_module_and_object(tmp_path):
    r = resolver.Resolver(firstparty.ModuleMap([_pkg(tmp_path)]))
    assert r.is_module("amb", "mod") is True
    assert r.is_module("amb", "Nope") is False


def test_stdlib_falls_through_to_the_probe(tmp_path):
    r = resolver.Resolver(firstparty.ModuleMap([_pkg(tmp_path)]))
    assert r.is_module("os", "path") is True
    assert r.is_module("collections", "OrderedDict") is False


def test_ambiguous_is_unresolved_with_an_explanatory_reason(tmp_path):
    root = _pkg(tmp_path)
    (root / "amb" / "__init__.py").write_text('mod = "shadow"\n', encoding="utf-8")
    r = resolver.Resolver(firstparty.ModuleMap([root]))
    assert r.is_module("amb", "mod") is None
    assert "both a submodule" in r.reason("amb", "mod")


def test_unimportable_parent_is_unresolved_with_its_own_reason(tmp_path):
    r = resolver.Resolver(firstparty.ModuleMap([_pkg(tmp_path)]))
    assert r.is_module("definitely_missing_pkg_xyz", "thing") is None
    assert "not importable" in r.reason("definitely_missing_pkg_xyz", "thing")


def test_warm_batches_and_matches_individual_lookups(tmp_path):
    r = resolver.Resolver(firstparty.ModuleMap([_pkg(tmp_path)]))
    pairs = [("amb", "mod"), ("collections", "OrderedDict"), ("os", "path")]
    r.warm(pairs)
    assert [r.is_module(p, n) for p, n in pairs] == [True, False, True]


def test_warm_then_reason_agree_for_an_ambiguous_pair(tmp_path):
    root = _pkg(tmp_path)
    (root / "amb" / "__init__.py").write_text('mod = "shadow"\n', encoding="utf-8")
    r = resolver.Resolver(firstparty.ModuleMap([root]))
    r.warm([("amb", "mod")])
    assert r.is_module("amb", "mod") is None
    assert "both a submodule" in r.reason("amb", "mod")


# -- the import the fixer would write --------------------------------------


def test_a_replacement_for_a_real_submodule_is_reachable(tmp_path):
    r = resolver.Resolver(firstparty.ModuleMap([_pkg(tmp_path)]))
    assert r.replacement_unreachable("amb.mod") is None


def test_a_top_level_parent_needs_no_replacement_check(tmp_path):
    """``from amb import X`` is replaced by ``import amb``: nothing to shadow."""
    r = resolver.Resolver(firstparty.ModuleMap([_pkg(tmp_path)]))
    assert r.replacement_unreachable("amb") is None


def test_a_replacement_the_parents_init_shadows_is_unreachable(tmp_path):
    root = _pkg(tmp_path)
    (root / "amb" / "__init__.py").write_text("from amb.mod import mod\n", encoding="utf-8")
    r = resolver.Resolver(firstparty.ModuleMap([root]))
    reason = r.replacement_unreachable("amb.mod")
    assert reason is not None
    assert "the replacement 'from amb import mod'" in reason
    assert "both a submodule of 'amb' and bound in its __init__" in reason


def test_a_replacement_the_run_cannot_see_is_unreachable(tmp_path):
    """The map says object, the import says module; neither can be trusted."""
    r = resolver.Resolver(firstparty.ModuleMap([_pkg(tmp_path)]))
    reason = r.replacement_unreachable("amb.nowhere")
    assert reason is not None
    assert "no submodule 'nowhere' under this run's import roots" in reason


def test_a_stdlib_replacement_is_reachable(tmp_path):
    """The probe answers for the emitted import exactly as for the read one."""
    r = resolver.Resolver(firstparty.ModuleMap([_pkg(tmp_path)]))
    assert r.replacement_unreachable("os.path") is None
    assert r.replacement_unreachable("collections.abc") is None

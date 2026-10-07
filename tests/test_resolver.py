# Copyright (c) 2026 grAItools
# SPDX-License-Identifier: BSD-3-Clause
# See LICENSE for the full license text.

"""Layered resolution and the reasons attached to unresolved verdicts."""

from __future__ import annotations

import importlib
import os
import pathlib
import subprocess
import sys
import venv

import pytest

from cleanporter import firstparty, resolver


def _pkg(tmp_path: pathlib.Path) -> pathlib.Path:
    root = tmp_path / "src"
    (root / "amb").mkdir(parents=True)
    (root / "amb" / "__init__.py").write_text("", encoding="utf-8", newline="\n")
    (root / "amb" / "mod.py").write_text("Q = 1\n", encoding="utf-8", newline="\n")
    return root


def test_first_party_module_and_object(tmp_path):
    r = resolver.Resolver(firstparty.ModuleMap([_pkg(tmp_path)]), evidence=resolver.NO_EVIDENCE)
    assert r.is_module("amb", "mod") is True
    assert r.is_module("amb.mod", "Q") is False


def test_first_party_name_that_is_nowhere_is_unresolved(tmp_path):
    """Neither on disk nor bound: missing is not an object, and the probe is not asked."""
    r = resolver.Resolver(firstparty.ModuleMap([_pkg(tmp_path)]), evidence=resolver.NO_EVIDENCE)
    assert r.is_module("amb", "Nope") is None
    assert r.reason("amb", "Nope") == (
        "'amb.Nope' is neither on disk under this run's import roots nor bound in 'amb'"
    )


def test_warm_carries_the_first_party_reason(tmp_path):
    root = _pkg(tmp_path)
    (root / "amb" / "__init__.py").write_text(
        "from . import _version\n", encoding="utf-8", newline="\n"
    )
    r = resolver.Resolver(firstparty.ModuleMap([root]), evidence=resolver.NO_EVIDENCE)
    r.warm([("amb", "_version")])
    assert r.is_module("amb", "_version") is None
    assert "by importing its own submodule of that name" in r.reason("amb", "_version")
    assert "not importable" not in r.reason("amb", "_version")


# -- a first-party re-export of a third-party name ---------------------------


def _reexporting(tmp_path: pathlib.Path, init: str) -> resolver.Resolver:
    root = _pkg(tmp_path)
    (root / "amb" / "__init__.py").write_text(init, encoding="utf-8", newline="\n")
    return resolver.Resolver(firstparty.ModuleMap([root]), evidence=resolver.NO_EVIDENCE)


def test_a_third_party_origin_is_answered_by_the_probe(tmp_path):
    """``from os import path`` in a first-party ``__init__``: ``amb.path`` is a module."""
    r = _reexporting(tmp_path, "from os import path, getcwd\n")
    assert r.is_module("amb", "path") is True
    assert r.is_module("amb", "getcwd") is False


def test_warm_settles_deferrals_in_the_same_probe_batch(tmp_path, monkeypatch):
    r = _reexporting(tmp_path, "from os import path, getcwd\n")
    batches: list[list[tuple[str, str]]] = []
    probe = r._probe

    def counting(pairs: list[tuple[str, str]]) -> dict[tuple[str, str], bool | None]:
        batches.append(list(pairs))
        return probe(pairs)

    monkeypatch.setattr(r, "_probe", counting)
    r.warm([("amb", "path"), ("amb", "getcwd"), ("collections", "OrderedDict")])
    assert len(batches) == 1
    assert set(batches[0]) == {("os", "path"), ("os", "getcwd"), ("collections", "OrderedDict")}
    assert [r.is_module("amb", n) for n in ("path", "getcwd")] == [True, False]
    assert len(batches) == 1


def test_an_unimportable_origin_leaves_the_reexport_unresolved(tmp_path):
    r = _reexporting(tmp_path, "from definitely_missing_pkg_xyz import thing\n")
    assert r.is_module("amb", "thing") is None
    reason = r.reason("amb", "thing")
    assert "'amb' binds 'thing' by importing 'thing' from 'definitely_missing_pkg_xyz'" in reason
    assert "not importable" in reason


def test_an_ambiguous_third_party_origin_leaves_the_reexport_unresolved(tmp_path, monkeypatch):
    """The probe's AMBIGUOUS for the origin is the re-export's answer too.

    ``resolver_shadow_pkg`` has a submodule ``leaf`` *and* rebinds ``leaf`` to
    a function in its ``__init__``, so what ``from resolver_shadow_pkg import
    leaf`` binds cannot be told without running it -- and a first-party
    package re-exporting that name inherits exactly that uncertainty, by
    either path through the resolver.
    """
    site = tmp_path / "site"
    shadow = site / "resolver_shadow_pkg"
    shadow.mkdir(parents=True)
    (shadow / "__init__.py").write_text("from .leaf import leaf\n", encoding="utf-8", newline="\n")
    (shadow / "leaf.py").write_text("def leaf(): ...\n", encoding="utf-8", newline="\n")
    monkeypatch.syspath_prepend(str(site))
    for dotted in ("resolver_shadow_pkg", "resolver_shadow_pkg.leaf"):
        monkeypatch.delitem(sys.modules, dotted, raising=False)
    importlib.invalidate_caches()

    for warm in (False, True):
        r = _reexporting(tmp_path / str(warm), "from resolver_shadow_pkg import leaf\n")
        if warm:
            r.warm([("amb", "leaf")])
        assert r.is_module("amb", "leaf") is None
        reason = r.reason("amb", "leaf")
        assert "'amb' binds 'leaf' by importing 'leaf' from 'resolver_shadow_pkg'" in reason
        assert "both a submodule of 'resolver_shadow_pkg' and bound in its __init__" in reason


def test_a_third_party_origin_must_agree_with_the_local_bindings(tmp_path):
    """``try: from os import path`` / ``except: path = None``: module or object."""
    init = "try:\n    from os import path\nexcept ImportError:\n    path = None\n"
    r = _reexporting(tmp_path, init)
    assert r.is_module("amb", "path") is None
    assert "more than once, to a module and to something that is not" in r.reason("amb", "path")
    r.warm([("amb", "path")])
    assert r.is_module("amb", "path") is None


def test_stdlib_falls_through_to_the_probe(tmp_path):
    r = resolver.Resolver(firstparty.ModuleMap([_pkg(tmp_path)]), evidence=resolver.NO_EVIDENCE)
    assert r.is_module("os", "path") is True
    assert r.is_module("collections", "OrderedDict") is False


def test_ambiguous_is_unresolved_with_an_explanatory_reason(tmp_path):
    root = _pkg(tmp_path)
    (root / "amb" / "__init__.py").write_text('mod = "shadow"\n', encoding="utf-8", newline="\n")
    r = resolver.Resolver(firstparty.ModuleMap([root]), evidence=resolver.NO_EVIDENCE)
    assert r.is_module("amb", "mod") is None
    assert "both a submodule" in r.reason("amb", "mod")


def test_unimportable_parent_is_unresolved_with_its_own_reason(tmp_path):
    r = resolver.Resolver(firstparty.ModuleMap([_pkg(tmp_path)]), evidence=resolver.NO_EVIDENCE)
    assert r.is_module("definitely_missing_pkg_xyz", "thing") is None
    assert "not importable" in r.reason("definitely_missing_pkg_xyz", "thing")


def test_warm_batches_and_matches_individual_lookups(tmp_path):
    r = resolver.Resolver(firstparty.ModuleMap([_pkg(tmp_path)]), evidence=resolver.NO_EVIDENCE)
    pairs = [("amb", "mod"), ("collections", "OrderedDict"), ("os", "path")]
    r.warm(pairs)
    assert [r.is_module(p, n) for p, n in pairs] == [True, False, True]


def test_warm_then_reason_agree_for_an_ambiguous_pair(tmp_path):
    root = _pkg(tmp_path)
    (root / "amb" / "__init__.py").write_text('mod = "shadow"\n', encoding="utf-8", newline="\n")
    r = resolver.Resolver(firstparty.ModuleMap([root]), evidence=resolver.NO_EVIDENCE)
    r.warm([("amb", "mod")])
    assert r.is_module("amb", "mod") is None
    assert "both a submodule" in r.reason("amb", "mod")


# -- the import the fixer would write --------------------------------------


def test_a_replacement_for_a_real_submodule_is_reachable(tmp_path):
    r = resolver.Resolver(firstparty.ModuleMap([_pkg(tmp_path)]), evidence=resolver.NO_EVIDENCE)
    assert r.replacement_unreachable("amb.mod") is None


def test_a_top_level_parent_needs_no_replacement_check(tmp_path):
    """``from amb import X`` is replaced by ``import amb``: nothing to shadow."""
    r = resolver.Resolver(firstparty.ModuleMap([_pkg(tmp_path)]), evidence=resolver.NO_EVIDENCE)
    assert r.replacement_unreachable("amb") is None


def test_a_replacement_the_parents_init_shadows_is_unreachable(tmp_path):
    root = _pkg(tmp_path)
    (root / "amb" / "__init__.py").write_text(
        "from amb.mod import mod\n", encoding="utf-8", newline="\n"
    )
    r = resolver.Resolver(firstparty.ModuleMap([root]), evidence=resolver.NO_EVIDENCE)
    reason = r.replacement_unreachable("amb.mod")
    assert reason is not None
    assert "the replacement 'from amb import mod'" in reason
    assert "both a submodule of 'amb' and bound in its __init__" in reason


def test_a_replacement_a_star_import_shadows_is_unreachable(tmp_path):
    """``from ._impl import *`` rebinds ``helpers`` just as ``helpers = ...`` would."""
    root = _pkg(tmp_path)
    (root / "amb" / "__init__.py").write_text(
        "from ._impl import *\n", encoding="utf-8", newline="\n"
    )
    (root / "amb" / "_impl.py").write_text("def mod(): ...\n", encoding="utf-8", newline="\n")
    r = resolver.Resolver(firstparty.ModuleMap([root]), evidence=resolver.NO_EVIDENCE)
    assert r.is_module("amb", "mod") is None
    reason = r.replacement_unreachable("amb.mod")
    assert reason is not None
    assert "both a submodule of 'amb' and bound in its __init__" in reason


def test_a_replacement_the_run_cannot_see_is_unreachable(tmp_path):
    """Nothing on disk and nothing bound: the replacement cannot be trusted."""
    r = resolver.Resolver(firstparty.ModuleMap([_pkg(tmp_path)]), evidence=resolver.NO_EVIDENCE)
    reason = r.replacement_unreachable("amb.nowhere")
    assert reason is not None
    assert "'amb.nowhere' is neither on disk under this run's import roots" in reason


def test_a_replacement_the_parent_binds_to_an_object_is_unreachable(tmp_path):
    root = _pkg(tmp_path)
    (root / "amb" / "__init__.py").write_text("def gone(): ...\n", encoding="utf-8", newline="\n")
    r = resolver.Resolver(firstparty.ModuleMap([root]), evidence=resolver.NO_EVIDENCE)
    reason = r.replacement_unreachable("amb.gone")
    assert reason is not None
    assert "'amb' binds 'gone' to something that is not a module" in reason


def test_a_stdlib_replacement_is_reachable(tmp_path):
    """The probe answers for the emitted import exactly as for the read one."""
    r = resolver.Resolver(firstparty.ModuleMap([_pkg(tmp_path)]), evidence=resolver.NO_EVIDENCE)
    assert r.replacement_unreachable("os.path") is None
    assert r.replacement_unreachable("collections.abc") is None


# -- probing another environment ---------------------------------------------


def _venv_with_onlyhere(tmp_path: pathlib.Path) -> pathlib.Path:
    """A real venv, built from this Python's base, holding a package cleanporter lacks."""
    target = tmp_path / "tv"
    try:
        venv.create(target, with_pip=False, symlinks=os.name != "nt")
    except (OSError, subprocess.CalledProcessError) as exc:  # pragma: no cover - platform
        pytest.skip(f"cannot create a virtual environment: {exc}")
    python = target / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    purelib = subprocess.run(
        [str(python), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    package = pathlib.Path(purelib) / "onlyhere"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        "def helper():\n    pass\n", encoding="utf-8", newline="\n"
    )
    return python


def test_another_venv_of_the_same_python_is_probed_in_its_own_environment(tmp_path):
    """Its interpreter resolves to the same base binary as ours; its packages do not.

    Comparing resolved paths probed it in process, against cleanporter's own
    ``sys.path``: ``onlyhere`` (only in the target) came back "not
    importable", and ``libcst`` (only in cleanporter's) came back classified.
    """
    python = _venv_with_onlyhere(tmp_path)
    r = resolver.Resolver(
        firstparty.ModuleMap([]), python=str(python), evidence=resolver.NO_EVIDENCE
    )
    r.warm([("onlyhere", "helper"), ("libcst", "parse_module")])
    assert r.is_module("onlyhere", "helper") is False
    assert r.is_module("libcst", "parse_module") is None
    assert r.reason("libcst", "parse_module") == (
        "'libcst' is not importable in the target interpreter"
    )

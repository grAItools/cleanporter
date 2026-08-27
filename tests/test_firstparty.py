"""Filesystem classification of first-party packages."""

from __future__ import annotations

from pathlib import Path

from cleanporter.firstparty import ModuleMap
from cleanporter.model import Kind


def _pkg(tmp_path: Path) -> Path:
    root = tmp_path / "src"
    (root / "amb").mkdir(parents=True)
    (root / "amb" / "__init__.py").write_text("", encoding="utf-8")
    (root / "amb" / "mod.py").write_text("Q = 1\n", encoding="utf-8")
    return root


def test_py_submodule_is_a_module(tmp_path):
    root = _pkg(tmp_path)
    mm = ModuleMap([root])
    assert mm.classify("amb", "mod") is Kind.MODULE


def test_plain_object_is_not_a_module(tmp_path):
    root = _pkg(tmp_path)
    (root / "amb" / "__init__.py").write_text("Thing = object()\n", encoding="utf-8")
    mm = ModuleMap([root])
    assert mm.classify("amb", "Thing") is Kind.OBJECT


def test_extension_submodule_is_a_module(tmp_path):
    root = _pkg(tmp_path)
    (root / "amb" / "accel.cpython-314-x86_64-linux-gnu.so").touch()
    mm = ModuleMap([root])
    assert mm.classify("amb", "accel") is Kind.MODULE


def test_windows_extension_submodule_is_a_module(tmp_path):
    root = _pkg(tmp_path)
    (root / "amb" / "fast.cp310-win_amd64.pyd").touch()
    mm = ModuleMap([root])
    assert mm.classify("amb", "fast") is Kind.MODULE


def test_directory_holding_only_an_extension_is_a_package(tmp_path):
    root = _pkg(tmp_path)
    (root / "amb" / "native").mkdir()
    (root / "amb" / "native" / "core.abi3.so").touch()
    mm = ModuleMap([root])
    assert mm.classify("amb", "native") is Kind.MODULE
    assert mm.classify("amb.native", "core") is Kind.MODULE


def test_non_first_party_defers_to_the_probe(tmp_path):
    mm = ModuleMap([_pkg(tmp_path)])
    assert mm.classify("collections", "OrderedDict") is None


def test_submodule_shadowed_by_an_init_binding_is_ambiguous(tmp_path):
    root = _pkg(tmp_path)
    (root / "amb" / "__init__.py").write_text(
        'mod = "shadowing string, not the submodule"\n', encoding="utf-8"
    )
    mm = ModuleMap([root])
    assert mm.classify("amb", "mod") is Kind.AMBIGUOUS


def test_init_importing_its_own_submodule_is_not_ambiguous(tmp_path):
    root = _pkg(tmp_path)
    (root / "amb" / "__init__.py").write_text("from . import mod\n", encoding="utf-8")
    mm = ModuleMap([root])
    assert mm.classify("amb", "mod") is Kind.MODULE


def test_for_loop_binding_that_shadows_a_submodule_is_ambiguous(tmp_path):
    root = _pkg(tmp_path)
    (root / "amb" / "__init__.py").write_text(
        'for mod in ["placeholder"]:\n    pass\n', encoding="utf-8"
    )
    mm = ModuleMap([root])
    assert mm.classify("amb", "mod") is Kind.AMBIGUOUS


def test_grandparent_relative_import_that_shadows_a_submodule_is_ambiguous(tmp_path):
    root = tmp_path / "src"
    (root / "pkg" / "sub").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "sub" / "__init__.py").write_text(
        "from .. import Y\n", encoding="utf-8"
    )
    (root / "pkg" / "sub" / "Y.py").write_text("Q = 1\n", encoding="utf-8")
    mm = ModuleMap([root])
    assert mm.classify("pkg.sub", "Y") is Kind.AMBIGUOUS


def test_aliased_self_import_that_shadows_a_real_submodule_is_ambiguous(tmp_path):
    root = _pkg(tmp_path)
    (root / "amb" / "__init__.py").write_text(
        "from . import mod as m\n", encoding="utf-8"
    )
    (root / "amb" / "m.py").write_text("Q = 1\n", encoding="utf-8")
    mm = ModuleMap([root])
    assert mm.classify("amb", "m") is Kind.AMBIGUOUS


def test_bare_annotation_does_not_bind_and_is_not_ambiguous(tmp_path):
    root = _pkg(tmp_path)
    (root / "amb" / "__init__.py").write_text(
        "from types import ModuleType\nmod: ModuleType\n", encoding="utf-8"
    )
    mm = ModuleMap([root])
    assert mm.classify("amb", "mod") is Kind.MODULE


# -- nested import roots (final review, Critical 1) --------------------------


def _src_layout(tmp_path: Path) -> Path:
    """A src-layout project whose ``tests/__init__.py`` drags the repo root in."""
    (tmp_path / "src" / "mypkg").mkdir(parents=True)
    (tmp_path / "src" / "mypkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "mypkg" / "helpers.py").write_text("Widget = object\n", encoding="utf-8")
    (tmp_path / "src" / "mypkg" / "consumer.py").write_text(
        "from .helpers import Widget\nw = Widget()\n", encoding="utf-8"
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "tests" / "test_it.py").write_text("", encoding="utf-8")
    return tmp_path


def test_the_most_specific_root_wins_for_qualname(tmp_path):
    root = _src_layout(tmp_path)
    mm = ModuleMap([root, root / "src"])
    assert mm.qualname_for(root / "src" / "mypkg" / "consumer.py") == "mypkg.consumer"


def test_from_paths_on_a_src_layout_still_qualifies_against_src(tmp_path):
    root = _src_layout(tmp_path)
    # What `cleanporter` with no path arguments does: every file under `.`.
    files = sorted(root.rglob("*.py"))
    mm = ModuleMap.from_paths(files)
    assert {r.name for r in mm.roots} == {root.name, "src"}, "both roots are inferred"
    assert mm.qualname_for(root / "src" / "mypkg" / "consumer.py") == "mypkg.consumer"
    assert mm.qualname_for(root / "src" / "mypkg" / "__init__.py") == "mypkg"


def test_nesting_roots_is_reported_as_a_warning(tmp_path):
    root = _src_layout(tmp_path)
    mm = ModuleMap([root, root / "src"])
    assert any("nest" in w for w in mm.warnings)
    assert ModuleMap([root / "src"]).warnings == []

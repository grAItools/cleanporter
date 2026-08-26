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

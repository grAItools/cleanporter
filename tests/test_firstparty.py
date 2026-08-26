"""Filesystem classification of first-party packages."""

from __future__ import annotations

from pathlib import Path

from cleanporter.firstparty import ModuleMap


def _pkg(tmp_path: Path) -> Path:
    root = tmp_path / "src"
    (root / "amb").mkdir(parents=True)
    (root / "amb" / "__init__.py").write_text("", encoding="utf-8")
    (root / "amb" / "mod.py").write_text("Q = 1\n", encoding="utf-8")
    return root


def test_py_submodule_is_a_module(tmp_path):
    root = _pkg(tmp_path)
    mm = ModuleMap([root])
    assert mm.classify("amb", "mod") is True


def test_plain_object_is_not_a_module(tmp_path):
    root = _pkg(tmp_path)
    (root / "amb" / "__init__.py").write_text("Thing = object()\n", encoding="utf-8")
    mm = ModuleMap([root])
    assert mm.classify("amb", "Thing") is False


def test_extension_submodule_is_a_module(tmp_path):
    root = _pkg(tmp_path)
    (root / "amb" / "accel.cpython-314-x86_64-linux-gnu.so").touch()
    mm = ModuleMap([root])
    assert mm.classify("amb", "accel") is True


def test_windows_extension_submodule_is_a_module(tmp_path):
    root = _pkg(tmp_path)
    (root / "amb" / "fast.cp310-win_amd64.pyd").touch()
    mm = ModuleMap([root])
    assert mm.classify("amb", "fast") is True


def test_directory_holding_only_an_extension_is_a_package(tmp_path):
    root = _pkg(tmp_path)
    (root / "amb" / "native").mkdir()
    (root / "amb" / "native" / "core.abi3.so").touch()
    mm = ModuleMap([root])
    assert mm.classify("amb", "native") is True
    assert mm.classify("amb.native", "core") is True


def test_non_first_party_defers_to_the_probe(tmp_path):
    mm = ModuleMap([_pkg(tmp_path)])
    assert mm.classify("collections", "OrderedDict") is None

"""Path expansion, exclusion, and skip directories."""

from __future__ import annotations

from pathlib import Path

from cleanporter.config import Config
from cleanporter.discover import iter_python_files


def _tree(tmp_path: Path) -> Path:
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "pkg" / "mod.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "pkg" / "skipme.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "pkg" / "__pycache__").mkdir()
    (tmp_path / "src" / "pkg" / "__pycache__" / "mod.py").write_text("", encoding="utf-8")
    (tmp_path / ".venv" / "lib").mkdir(parents=True)
    (tmp_path / ".venv" / "lib" / "vendored.py").write_text("", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("", encoding="utf-8")
    return tmp_path


def _names(files: list[Path]) -> list[str]:
    return sorted(f.name for f in files)


def test_walk_collects_python_files_only(tmp_path):
    root = _tree(tmp_path)
    files, warnings = iter_python_files([root], Config(root=root))
    assert _names(files) == ["__init__.py", "mod.py", "skipme.py"]
    assert warnings == []


def test_walk_skips_dot_directories_and_pycache(tmp_path):
    root = _tree(tmp_path)
    files, _ = iter_python_files([root], Config(root=root))
    assert not any(".venv" in f.parts for f in files)
    assert not any("__pycache__" in f.parts for f in files)


def test_explicitly_named_dot_directory_is_still_scanned(tmp_path):
    root = _tree(tmp_path)
    files, _ = iter_python_files([root / ".venv"], Config(root=root))
    assert _names(files) == ["vendored.py"]


def test_explicitly_named_file_is_scanned_even_if_excluded(tmp_path):
    root = _tree(tmp_path)
    cfg = Config(root=root, exclude=("**/skipme.py",))
    files, _ = iter_python_files([root / "src" / "pkg" / "skipme.py"], cfg)
    assert _names(files) == ["skipme.py"]


def test_exclude_glob_applies_while_walking(tmp_path):
    root = _tree(tmp_path)
    cfg = Config(root=root, exclude=("**/skipme.py",))
    files, _ = iter_python_files([root], cfg)
    assert _names(files) == ["__init__.py", "mod.py"]


def test_literal_exclude_matches_a_directory_and_its_contents(tmp_path):
    root = _tree(tmp_path)
    cfg = Config(root=root, exclude=("src/pkg",))
    files, _ = iter_python_files([root], cfg)
    assert files == []


def test_results_are_deduplicated_and_sorted(tmp_path):
    root = _tree(tmp_path)
    target = root / "src" / "pkg" / "mod.py"
    files, _ = iter_python_files([target, root / "src", target], Config(root=root))
    assert len(files) == len({f.resolve() for f in files})
    assert files == sorted(files)


def test_missing_path_is_a_warning_not_a_crash(tmp_path):
    root = _tree(tmp_path)
    files, warnings = iter_python_files([root / "nope"], Config(root=root))
    assert files == []
    assert len(warnings) == 1 and "does not exist" in warnings[0]


def test_absolute_exclude_pattern_matches(tmp_path):
    root = _tree(tmp_path)
    pattern = (root / "src" / "pkg" / "*.py").resolve().as_posix()
    cfg = Config(root=root, exclude=(pattern,))
    files, _ = iter_python_files([root], cfg)
    assert files == []


def test_walk_outside_config_root_still_honours_absolute_excludes(tmp_path, monkeypatch):
    # The path handed to iter_python_files must be genuinely unresolved and
    # contain a ".." segment -- this is what distinguishes the fixed
    # resolve()-based abs_posix arm from the pre-fix raw-path fallback,
    # which would see "../outside/src/pkg/mod.py" and never match an
    # absolute exclude pattern.
    outside = _tree(tmp_path / "outside")
    proj = tmp_path / "proj"
    proj.mkdir()
    excluded_file = (outside / "src" / "pkg" / "mod.py").resolve().as_posix()
    cfg = Config(root=proj, exclude=(excluded_file,))
    monkeypatch.chdir(proj)
    files, _ = iter_python_files([Path("../outside")], cfg)
    assert _names(files) == ["__init__.py", "skipme.py"]

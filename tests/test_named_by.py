"""The cross-file string guard: a dotted path in another file keeps a re-export.

``monkeypatch.setattr("pkg.mod.helper", ...)`` in a test, ``mock.patch`` of
the same, an entry point in ``pyproject.toml``: each names ``pkg.mod.helper``
by its dotted path, and each goes stale if ``--fix`` rewrites the ``from
pkg.core import helper`` that binds it. `resolver.Resolver.named_by` is the
guard; `analyze.Decider` turns it into a per-name `CP003`, in both modes.
"""

from __future__ import annotations

import pathlib

import pytest

from cleanporter import config, engine, guards

_CORE = "".join(f"def {name}():\n    return 1\n\n\n" for name in ("helper", "other", "main"))
_MOD = "from pkg.core import helper, other\n\n\ndef run():\n    return helper() + other()\n"
_MOD_FIXED = (
    "from pkg import core\nfrom pkg.core import helper\n\n\n"
    "def run():\n    return helper() + core.other()\n"
)


def _tree(root: pathlib.Path, files: dict[str, str], pyproject: str = "") -> pathlib.Path:
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "x"\n{pyproject}', encoding="utf-8", newline="\n"
    )
    base = {"pkg/__init__.py": "", "pkg/core.py": _CORE, "pkg/mod.py": _MOD, **files}
    for rel, text in base.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8", newline="\n")
    return root


def _run(
    root: pathlib.Path,
    mode: engine.Mode = engine.Mode.FIX,
    paths: list[pathlib.Path] | None = None,
    *,
    whole_project: bool = False,
) -> engine.RunResult:
    return engine.run(paths or [root], config.Config(root=root), mode, whole_project=whole_project)


def _in(result: engine.RunResult, name: str) -> list[tuple[str, str, str]]:
    return [(f.code, f.name, f.detail) for f in result.findings if f.path.name == name]


@pytest.mark.parametrize(
    "test_source",
    [
        'def test_it(monkeypatch):\n    monkeypatch.setattr("pkg.mod.helper", lambda: 5)\n',
        'from unittest import mock\n\n\n@mock.patch("pkg.mod.helper")\ndef test_it(m):\n    pass\n',
        'import importlib\n\nHELPER = "pkg.mod:helper"\n',
        'NAME = "pkg.mod.helper.__name__"\n',
    ],
    ids=["monkeypatch", "mock", "entry-point-spelling", "path-through-it"],
)
def test_a_string_in_another_file_keeps_that_one_import(
    tmp_path: pathlib.Path, test_source: str
) -> None:
    root = _tree(tmp_path, {"tests/test_x.py": test_source})
    result = _run(root)
    [(code, name, detail)] = _in(result, "mod.py")
    assert (code, name) == ("CP003", "helper")
    line = next(i for i, t in enumerate(test_source.splitlines(), 1) if "pkg.mod" in t)
    assert f"at {pathlib.Path('tests', 'test_x.py')}:{line}" in detail
    assert detail.startswith("'pkg.mod.helper' is named by the string 'pkg.mod")
    # Per name, like the load-bearing guard: the rest of the file is fixed.
    assert (root / "pkg" / "mod.py").read_text(encoding="utf-8") == _MOD_FIXED


def test_check_mode_reports_the_same_cp003(tmp_path: pathlib.Path) -> None:
    root = _tree(tmp_path, {"t.py": 'TARGET = "pkg.mod.helper"\n'})
    check = _in(_run(root, engine.Mode.CHECK), "mod.py")
    assert sorted((c, n) for c, n, _ in check) == [("CP001", "other"), ("CP003", "helper")]
    fix = _in(_run(root, engine.Mode.DIFF), "mod.py")
    assert [f for f in fix if f[0] == "CP003"] == [f for f in check if f[0] == "CP003"]


def test_an_entry_point_in_pyproject_keeps_its_target(tmp_path: pathlib.Path) -> None:
    root = _tree(
        tmp_path,
        {
            "pkg/cli.py": "from pkg.core import main\n\n\ndef go():\n    return main()\n",
            "pkg/gui.py": "from pkg.core import other\n\n\ndef go():\n    return other()\n",
        },
        '[project.scripts]\nx = "pkg.cli:main"\n'
        '[project.entry-points."x.plugins"]\np = "pkg.gui : other [extra]"\n',
    )
    result = _run(root)
    [(code, _, detail)] = _in(result, "cli.py")
    assert code == "CP003"
    assert "named by the string 'pkg.cli:main' at pyproject.toml:4" in detail
    [(code, _, detail)] = _in(result, "gui.py")
    assert code == "CP003"
    assert "named by the string 'pkg.gui:other' at pyproject.toml:6" in detail


def test_a_package_reexport_named_through_the_package(tmp_path: pathlib.Path) -> None:
    """``"pkg.helper"`` names what ``pkg/__init__`` imports from ``pkg.core``."""
    init = "from pkg.core import helper\n\n\ndef run():\n    return helper()\n"
    root = _tree(tmp_path, {"pkg/__init__.py": init, "t.py": 'X = "pkg.helper"\n'})
    [(code, name, detail)] = _in(_run(root), "__init__.py")
    assert (code, name) == ("CP003", "helper")
    assert "'pkg.helper' is named by the string 'pkg.helper' at t.py:1" in detail
    assert (root / "pkg" / "__init__.py").read_text(encoding="utf-8") == init


@pytest.mark.parametrize(
    "text",
    [
        "pkg.mod.run",  # a different attribute of the same module, defined there
        "pkg.mod.nothing",  # one it does not bind at all
        "other_pkg.mod.helper",  # not first-party
        "os.path.join",  # stdlib
        "see pkg.mod.helper",  # prose, not a path
        "pkg.mod.helper()",  # code, not a path: the cross-file reading is paths only
    ],
)
def test_strings_that_do_not_name_the_binding_do_not_block(
    tmp_path: pathlib.Path, text: str
) -> None:
    root = _tree(tmp_path, {"t.py": f"X = {text!r}\n"})
    result = _run(root)
    assert _in(result, "mod.py") == []
    assert (root / "pkg" / "mod.py").read_text(encoding="utf-8") != _MOD


def test_a_string_in_the_same_file_is_still_the_whole_file_guard(tmp_path: pathlib.Path) -> None:
    source = _MOD + 'TARGET = "pkg.mod.helper"\n'
    root = _tree(tmp_path, {"pkg/mod.py": source})
    result = _run(root)
    found = _in(result, "mod.py")
    # The whole-file blocker, not the per-name cross-file reason.
    assert ("CP003", "?", "name 'helper' appears in a string literal") in found
    assert not any("is named by the string" in d for _, _, d in found)
    assert (root / "pkg" / "mod.py").read_text(encoding="utf-8") == source


def test_whole_project_evidence_from_an_unlisted_file_blocks(tmp_path: pathlib.Path) -> None:
    root = _tree(tmp_path, {"tests/test_x.py": 'T = "pkg.mod.helper"\n'})
    mod = root / "pkg" / "mod.py"
    result = _run(root, paths=[mod], whole_project=True)
    assert result.files_checked == 1
    [(code, name, detail)] = _in(result, "mod.py")
    assert (code, name) == ("CP003", "helper")
    assert f"at {pathlib.Path('tests', 'test_x.py')}:1" in detail
    # Without the flag the test file is not read, and the import is rewritten.
    plain = _run(root, paths=[mod])
    assert _in(plain, "mod.py") == []
    assert "core.helper()" in mod.read_text(encoding="utf-8")


def test_an_unreadable_pyproject_is_a_warning(tmp_path: pathlib.Path) -> None:
    root = _tree(tmp_path, {})
    (root / "pyproject.toml").write_text("[project\n", encoding="utf-8", newline="\n")
    result = _run(root, engine.Mode.CHECK)
    assert any("cannot read its entry points" in w for w in result.warnings)


@pytest.mark.parametrize(
    ("content", "parts"),
    [
        ("pkg.mod", ("pkg", "mod")),
        ("pkg.mod.helper", ("pkg", "mod", "helper")),
        ("pkg.mod:helper", ("pkg", "mod", "helper")),
        ("pkg:helper.attr", ("pkg", "helper", "attr")),
        ("pkg", None),
        ("pkg.", None),
        ("pkg..mod", None),
        ("pkg.mod:a:b", None),
        ("pkg. mod", None),
        ("pkg.1mod", None),
        ("pkg/mod.py", None),
    ],
)
def test_dotted_reference(content: str, parts: tuple[str, ...] | None) -> None:
    assert guards.dotted_reference(content) == parts


def test_cross_file_pairs_offers_every_split() -> None:
    assert guards.cross_file_pairs(("a", "b", "c")) == [("a", "b"), ("a.b", "c")]

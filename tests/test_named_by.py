"""The cross-file string guard: a dotted path in another file keeps a re-export.

``monkeypatch.setattr("pkg.mod.helper", ...)`` in a test, ``mock.patch`` of
the same, an entry point in ``pyproject.toml``: each names ``pkg.mod.helper``
by its dotted path, and each goes stale if ``--fix`` rewrites the ``from
pkg.core import helper`` that binds it. `resolver.Resolver.named_by` is the
guard; `analyze.Decider` turns it into a per-name `CP003`, in both modes.
"""

from __future__ import annotations

import os
import pathlib

import pytest

from cleanporter import _pyproject, config, engine, guards

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


@pytest.fixture(autouse=True)
def _in_tmp(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run from the project, so paths are spelled ``pkg/mod.py`` as a user's would be."""
    monkeypatch.chdir(tmp_path)


def _run(
    root: pathlib.Path,
    mode: engine.Mode = engine.Mode.FIX,
    paths: list[pathlib.Path] | None = None,
    *,
    whole_project: bool = False,
) -> engine.RunResult:
    listed = paths or [pathlib.Path(os.path.relpath(root))]
    return engine.run(listed, config.Config(root=root), mode, whole_project=whole_project)


def _in(result: engine.RunResult, name: str) -> list[tuple[str, str, str]]:
    return [(f.code, f.name, f.detail) for f in result.findings if f.path.name == name]


@pytest.mark.parametrize(
    "test_source",
    [
        'def test_it(monkeypatch):\n    monkeypatch.setattr("pkg.mod.helper", lambda: 5)\n',
        'from unittest import mock\n\n\n@mock.patch("pkg.mod.helper")\ndef test_it(m):\n    pass\n',
        'import importlib\n\nHELPER = "pkg.mod:helper"\n',
        'NAME = "pkg.mod.helper.__name__"\n',
        'def test_it(monkeypatch):\n    monkeypatch.setattr("pkg.mod." "helper", lambda: 5)\n',
        'X = ("pkg."\n     f"mod.helper")\n',
        'X = f"pkg.mod.helper"\n',
        'X = rf"pkg.mod:helper"\n',
    ],
    ids=[
        "monkeypatch",
        "mock",
        "entry-point-spelling",
        "path-through-it",
        "implicit-concatenation",
        "concatenated-f-string",
        "f-string",
        "raw-f-string",
    ],
)
def test_a_string_in_another_file_keeps_that_one_import(
    tmp_path: pathlib.Path, test_source: str
) -> None:
    root = _tree(tmp_path, {"tests/test_x.py": test_source})
    result = _run(root)
    [(code, name, detail)] = _in(result, "mod.py")
    assert (code, name) == ("CP003", "helper")
    line = next(i for i, t in enumerate(test_source.splitlines(), 1) if "pkg." in t)
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


def test_any_table_of_pyproject_counts(tmp_path: pathlib.Path) -> None:
    """Poetry's scripts, a plugin list: not only the PEP 621 tables."""
    root = _tree(
        tmp_path,
        {
            "pkg/cli.py": "from pkg.core import main\n\n\ndef go():\n    return main()\n",
            "pkg/gui.py": "from pkg.core import other\n\n\ndef go():\n    return other()\n",
        },
        '[tool.poetry.scripts]\nx = "pkg.cli:main"\n[tool.thing]\nplugins = ["pkg.gui.other"]\n',
    )
    result = _run(root)
    [(_, _, detail)] = _in(result, "cli.py")
    assert "named by the string 'pkg.cli:main' at pyproject.toml:4" in detail
    # In an array: no line can be pinned down, so the file alone is named.
    [(_, _, detail)] = _in(result, "gui.py")
    assert "named by the string 'pkg.gui.other' at pyproject.toml;" in detail


def test_the_pyproject_line_is_the_assignment_in_its_table() -> None:
    """The value written earlier -- in a comment, another key, another table -- is not it."""
    toml = (
        "[project]\n"
        '# x = "pkg.cli:main"\n'
        'description = "pkg.cli:main"\n'
        "[tool.other]\n"
        'y = "pkg.cli:main"\n'
        "[project.scripts]\n"
        'x = "pkg.cli:main"\n'
    )
    refs = _pyproject.references(toml)
    assert sorted((r.line or 0) for r in refs) == [3, 5, 7]


@pytest.mark.parametrize(
    ("toml", "line"),
    [
        ('[a]\nx = "p.m:f"\n', 2),
        ("[a]\nx = 'p.m:f'  # comment\n", 2),
        ('[a."b.c"]\n"x" = "p.m:f"\n', 2),
        ('[a]\nb.x = "p.m:f"\n', None),  # a dotted key
        ('[a]\nx = { y = "p.m:f" }\n', None),  # an inline table
        ('[a]\nx = [\n  "p.m:f",\n]\n', None),  # an array
        ('[a]\nx = """p.m:f"""\n', None),  # a multi-line string
        ('[b]\ny = "p.m:f"\n[a]\nx = "p.m:f"\n', 2),  # its own table's line
        ('[a]\ny = "p.m:f"  # x = "p.m:f"\n', 2),
    ],
)
def test_pyproject_lines(toml: str, line: int | None) -> None:
    ref = _pyproject.references(toml)[0]
    assert (ref.text, ref.line) == ("p.m:f", line)


@pytest.mark.parametrize(
    ("toml", "lines"),
    [
        # A look-alike inside a multi-line string, either quote style.
        ('[tool.x]\ndoc = """\nk = "pkg.mod:h"\n"""\nk = "pkg.mod:h"\n', [5]),
        ("[tool.x]\ndoc = '''\nk = \"pkg.mod:h\"\n'''\nk = \"pkg.mod:h\"\n", [5]),
        # A fake header inside one.
        ('[tool.y]\ndoc = """\n[tool.x]\nk = "pkg.mod:h"\n"""\n[tool.x]\nk = "pkg.mod:h"\n', [7]),
        # ... and no real line at all: the value is in an inline table.
        ('[tool]\ndoc = """\n[tool.x]\nk = "pkg.mod:h"\n"""\nx = {k = "pkg.mod:h"}\n', [None]),
        # An array element that looks like a header.
        ('[y]\nm = [\n  ["x"]\n]\nk = "pkg.mod:h"\n[x]\nk = "pkg.mod:h"\n', [5, 7]),
    ],
    ids=["basic-multiline", "literal-multiline", "fake-header", "inline-only", "array-header"],
)
def test_a_pyproject_line_is_proved_not_guessed(toml: str, lines: list[int | None]) -> None:
    """A line that only looks like the assignment is never named: the parser must agree."""
    assert [r.line for r in _pyproject.references(toml)] == lines


_CLI = "from pkg.core import main\n\n\ndef go():\n    return main()\n"


def test_pyproject_is_named_absolutely_for_an_absolute_run(tmp_path: pathlib.Path) -> None:
    root = _tree(tmp_path, {"pkg/cli.py": _CLI}, '[project.scripts]\nx = "pkg.cli:main"\n')
    result = _run(root, paths=[root])
    [(_, _, detail)] = _in(result, "cli.py")
    assert f"at {(root / 'pyproject.toml').resolve()}:4;" in detail


def test_pyproject_outside_the_cwd_is_named_absolutely(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    root = _tree(proj, {"pkg/cli.py": _CLI}, '[project.scripts]\nx = "pkg.cli:main"\n')
    (tmp_path / "elsewhere").mkdir()
    monkeypatch.chdir(tmp_path / "elsewhere")
    result = _run(root, paths=[pathlib.Path("..", "proj")])
    [(_, _, detail)] = _in(result, "cli.py")
    assert f"at {(root / 'pyproject.toml').resolve()}:4;" in detail


@pytest.mark.parametrize(
    ("value", "text"),
    [
        ("pkg.cli:main", "pkg.cli:main"),
        ("pkg.cli : main", "pkg.cli:main"),
        ("pkg.cli:main [extra, other]", "pkg.cli:main"),
        ("pkg.cli:main[extra]", "pkg.cli:main"),
        ("pkg.cli.main", "pkg.cli.main"),
    ],
)
def test_pyproject_entry_point_spellings(value: str, text: str) -> None:
    assert [r.text for r in _pyproject.references(f'x = "{value}"\n')] == [text]


@pytest.mark.parametrize("value", ["pkg. cli:main", " pkg.cli:main", "0.4.0", "README.md x"])
def test_pyproject_values_that_are_not_references(value: str) -> None:
    assert _pyproject.references(f'x = "{value}"\n') == []


def test_the_reason_spells_the_path_as_the_run_does(tmp_path: pathlib.Path) -> None:
    """With no pyproject, the root is the listed package, not the working directory."""
    pkg = tmp_path / "pkg"
    files = {
        "__init__.py": "",
        "core.py": _CORE,
        "mod.py": _MOD,
        "sub/__init__.py": "",
        "sub/patcher.py": 'T = "pkg.mod.helper"\n',
    }
    for rel, text in files.items():
        (pkg / rel).parent.mkdir(parents=True, exist_ok=True)
        (pkg / rel).write_text(text, encoding="utf-8", newline="\n")
    result = engine.run([pathlib.Path("pkg")], config.Config(root=pkg), engine.Mode.CHECK)
    [finding] = [f for f in result.findings if f.code == "CP003"]
    assert finding.path == pathlib.Path("pkg", "mod.py")
    assert f"at {pathlib.Path('pkg', 'sub', 'patcher.py')}:1;" in finding.detail


def test_a_package_reexport_named_through_the_package(tmp_path: pathlib.Path) -> None:
    """``"pkg.helper"`` names what ``pkg/__init__`` imports from ``pkg.core``.

    At module level that import is the package's public surface
    (`analyze.Decider.public_surface`): kept without being reported, string or
    no string. In a function it binds a local, not the attribute the string
    names, so it is decided like any other import -- and fixed.
    """
    init = "from pkg.core import helper\n\n\ndef run():\n    return helper()\n"
    root = _tree(tmp_path, {"pkg/__init__.py": init, "t.py": 'X = "pkg.helper"\n'})
    assert _in(_run(root), "__init__.py") == []
    assert (root / "pkg" / "__init__.py").read_text(encoding="utf-8") == init

    local = "def run():\n    from pkg.core import helper\n\n    return helper()\n"
    (root / "pkg" / "__init__.py").write_text(local, encoding="utf-8", newline="\n")
    assert _in(_run(root), "__init__.py") == []
    assert (root / "pkg" / "__init__.py").read_text(encoding="utf-8") == (
        "def run():\n    from pkg import core\n\n    return core.helper()\n"
    )


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
    _assert_no_block(tmp_path, f"X = {text!r}\n")


@pytest.mark.parametrize(
    "source",
    [
        'X = b"pkg.mod.helper"\n',  # bytes
        'X = b"pkg.mod." b"helper"\n',
        'X = "pkg.mod." + "helper"\n',  # an expression: dynamic, not a literal
        'X = f"pkg.mod.{name}"\n',  # a placeholder
        'X = "pkg.mod." f"{name}"\n',
        'X = "pkg.mod.\\x68elper"\n',  # an escape
        'X = "pkg.mod.helper" "()"\n',  # concatenated into code
    ],
)
def test_strings_that_are_not_literal_paths_do_not_block(
    tmp_path: pathlib.Path, source: str
) -> None:
    _assert_no_block(tmp_path, "name = 'x'\n" + source)


def _assert_no_block(tmp_path: pathlib.Path, source: str) -> None:
    root = _tree(tmp_path, {"t.py": source})
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
    assert any("cannot read it for dotted references" in w for w in result.warnings)


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

"""Whole-project runs: read the whole tree for evidence, report only the listed files.

`engine.run(..., whole_project=True)` / ``--whole-project`` exist for
pre-commit, which hands a hook only the changed files. Each test here pins
one thing a run over those files alone would get wrong, or one part of the
report-only-what-was-listed contract, and `.pre-commit-hooks.yaml` is checked
against the command line it drives.
"""

from __future__ import annotations

import json
import pathlib
import shlex
import shutil
import subprocess
import sys
import tomllib

import pytest

from cleanporter import cli, config, engine

ROOT = pathlib.Path(__file__).resolve().parents[1]
MANIFEST = ROOT / ".pre-commit-hooks.yaml"

_REEXPORT = "from demo.core import helper\n\n\ndef run():\n    return helper()\n"


@pytest.fixture
def tree(tmp_path: pathlib.Path) -> pathlib.Path:
    """A project whose ``demo/__init__`` re-exports a name only ``consumer.py`` uses."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\n', encoding="utf-8")
    pkg = tmp_path / "demo"
    pkg.mkdir()
    (pkg / "core.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    (pkg / "__init__.py").write_text(_REEXPORT, encoding="utf-8")
    (tmp_path / "consumer.py").write_text("from demo import helper\n\nhelper()\n", "utf-8")
    return tmp_path


def _codes(result: engine.RunResult) -> list[tuple[str, str]]:
    return [(f.path.name, f.code) for f in result.findings]


def test_a_use_in_an_unlisted_file_still_blocks_the_rewrite(tree: pathlib.Path) -> None:
    init = tree / "demo" / "__init__.py"
    cfg = config.Config(root=tree)
    result = engine.run([init], cfg, engine.Mode.FIX, whole_project=True)
    # consumer.py was not listed, but it imports `helper` from `demo`: the
    # rewrite would delete that attribute, so it is declined.
    assert _codes(result) == [("__init__.py", "CP003")]
    assert "another file imports 'helper'" in result.findings[0].detail
    assert init.read_text(encoding="utf-8") == _REEXPORT
    assert result.files_checked == 1
    assert result.exit_code() == 1


def test_without_the_flag_the_same_file_alone_is_rewritten(tree: pathlib.Path) -> None:
    """The contrast: the partial tree is what makes the plain run unsafe."""
    init = tree / "demo" / "__init__.py"
    result = engine.run([init], config.Config(root=tree), engine.Mode.FIX)
    assert result.wrote
    assert init.read_text(encoding="utf-8") != _REEXPORT


def test_only_listed_files_are_reported_and_counted(tree: pathlib.Path) -> None:
    (tree / "demo" / "clean.py").write_text("from demo import core\n\ncore.helper()\n", "utf-8")
    result = engine.run([tree / "demo" / "clean.py"], config.Config(root=tree), whole_project=True)
    # consumer.py's CP001 is not this run's to report, nor to fail on.
    assert result.findings == ()
    assert result.files_checked == 1
    assert result.exit_code() == 0


def test_a_listed_directory_reports_every_file_under_it(tree: pathlib.Path) -> None:
    result = engine.run([tree / "demo"], config.Config(root=tree), whole_project=True)
    assert _codes(result) == [("__init__.py", "CP003")]
    assert result.files_checked == 2


def test_a_first_party_package_no_listed_file_lives_in_stays_first_party(
    tmp_path: pathlib.Path,
) -> None:
    """Import roots are inferred from the files a run reads, so read them all.

    Listed alone, ``tests/test_app.py`` infers only ``tests/`` as a root, and
    ``app`` (under ``src/``) is not first-party: the probe is asked instead,
    and answers from whatever is installed under that name -- here nothing,
    so ``CP002``; elsewhere, a stale copy.
    """
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "app"\n', encoding="utf-8")
    (tmp_path / "src" / "app").mkdir(parents=True)
    (tmp_path / "src" / "app" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "app" / "core.py").write_text("THING = 1\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    test = tmp_path / "tests" / "test_app.py"
    test.write_text("from app.core import THING\n\nTHING\n", encoding="utf-8")
    cfg = config.Config(root=tmp_path, python="self")

    partial = engine.run([test], cfg)
    whole = engine.run([test], cfg, whole_project=True)
    assert _codes(partial) == [("test_app.py", "CP002")]
    assert _codes(whole) == [("test_app.py", "CP001")]


def test_an_excluded_listed_file_is_not_reported(tree: pathlib.Path) -> None:
    (tree / "gen").mkdir()
    generated = tree / "gen" / "out.py"
    generated.write_text("from demo.core import helper\n\nhelper()\n", encoding="utf-8")
    cfg = config.Config(root=tree, exclude=("gen",))
    result = engine.run([generated], cfg, whole_project=True)
    assert result.findings == ()
    assert result.files_checked == 0
    # Named explicitly *without* the flag, an excluded file is still checked.
    assert engine.run([generated], cfg).files_checked == 1


def test_a_listed_file_outside_the_root_is_analysed_and_reported(
    tree: pathlib.Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    outside = tmp_path_factory.mktemp("elsewhere") / "script.py"
    outside.write_text("from os.path import join\n\njoin\n", encoding="utf-8")
    cfg = config.Config(root=tree, python="self")
    result = engine.run([outside], cfg, whole_project=True)
    assert _codes(result) == [("script.py", "CP001")]
    assert result.files_checked == 1


def test_a_missing_listed_path_is_a_warning(tree: pathlib.Path) -> None:
    result = engine.run([tree / "gone.py"], config.Config(root=tree), whole_project=True)
    assert result.findings == ()
    assert result.files_checked == 0
    assert any("path does not exist" in w for w in result.warnings)
    assert result.exit_code() == 0


def test_an_unparseable_unlisted_file_is_a_warning_not_an_error(tree: pathlib.Path) -> None:
    (tree / "broken.py").write_text("def (:\n", encoding="utf-8")
    cfg = config.Config(root=tree)
    result = engine.run([tree / "consumer.py"], cfg, whole_project=True)
    assert result.errors == ()
    assert any("broken.py" in w and "evidence" in w for w in result.warnings)
    listed = engine.run([tree / "broken.py"], cfg, whole_project=True)
    assert [e.path.name for e in listed.errors] == ["broken.py"]
    assert listed.exit_code() == 2


def test_cli_reports_listed_files_relative_to_the_cwd(tree: pathlib.Path, monkeypatch, capsys):
    monkeypatch.chdir(tree)
    rc = cli.main(["--whole-project", "demo/__init__.py"])
    out = capsys.readouterr().out
    assert out.startswith("demo/__init__.py:1:0: CP003 ")
    assert "checked 1 file(s)" in out
    assert rc == 1


def test_cli_fix_exits_0_once_everything_listed_is_fixed(tree: pathlib.Path, monkeypatch, capsys):
    """pre-commit fails a hook that changed files by itself; the exit code need not."""
    monkeypatch.chdir(tree)
    assert cli.main(["--whole-project", "--fix", "consumer.py"]) == 0
    fixed = (tree / "consumer.py").read_text(encoding="utf-8")
    assert fixed == "import demo\n\ndemo.helper()\n"
    assert "fixed: consumer.py" in capsys.readouterr().err


def test_cli_without_a_pyproject_is_an_error(tmp_path: pathlib.Path, capsys) -> None:
    script = tmp_path / "script.py"
    script.write_text("import os\n", encoding="utf-8")
    assert cli.main(["--whole-project", str(script)]) == 2
    assert "needs a pyproject.toml" in capsys.readouterr().err


@pytest.fixture
def stray(tmp_path_factory: pytest.TempPathFactory) -> pathlib.Path:
    """A clean file under no pyproject.toml, outside every project."""
    path = tmp_path_factory.mktemp("stray") / "stray.py"
    path.write_text("import os\n\nos.sep\n", encoding="utf-8")
    assert config.find_pyproject(path) is None
    return path


@pytest.mark.parametrize("stray_first", [True, False])
def test_the_project_does_not_depend_on_which_file_comes_first(
    tree: pathlib.Path, stray: pathlib.Path, capsys, *, stray_first: bool
) -> None:
    """pre-commit lists files in no promised order; the project is the first with one."""
    listed = [str(stray), str(tree / "demo" / "__init__.py")]
    rc = cli.main(["--whole-project", *(listed if stray_first else listed[::-1])])
    out = capsys.readouterr().out
    # consumer.py was read either way: its use of the re-export declines the rewrite.
    assert "__init__.py:1:0: CP003 " in out
    assert "checked 2 file(s)" in out
    assert rc == 1


def test_a_symlink_to_a_file_outside_the_project_belongs_to_the_project(
    tree: pathlib.Path, stray: pathlib.Path, capsys
) -> None:
    """Placed by where it is listed, not by its target, which has no pyproject.toml."""
    stray.write_text("from os.path import join\n\njoin\n", encoding="utf-8")
    link = tree / "linked.py"
    try:
        link.symlink_to(stray)
    except OSError:  # pragma: no cover - e.g. Windows without the privilege
        pytest.skip("cannot create a symlink here")
    rc = cli.main(["--whole-project", "--python", "self", str(link)])
    captured = capsys.readouterr()
    assert "needs a pyproject.toml" not in captured.err
    assert "CP001 imports object 'join'" in captured.out
    assert "checked 1 file(s)" in captured.out
    assert rc == 1


@pytest.mark.parametrize("nested_first", [True, False])
def test_files_from_two_projects_are_refused(
    tree: pathlib.Path, monkeypatch, capsys, *, nested_first: bool
) -> None:
    """A nested project's file must not decide the project -- nor be outvoted.

    Anchored on ``examples/ex``, ``demo/__init__.py`` became an outside file,
    ``consumer.py`` was never read, and ``--fix`` deleted the re-export it
    imports. Either order is refused, and nothing is written.
    """
    nested = tree / "examples" / "ex"
    nested.mkdir(parents=True)
    (nested / "pyproject.toml").write_text('[project]\nname = "ex"\n', encoding="utf-8")
    (nested / "a.py").write_text("import os\n", encoding="utf-8")
    monkeypatch.chdir(tree)
    listed = ["examples/ex/a.py", "demo/__init__.py"]
    rc = cli.main(["--whole-project", "--fix", *(listed if nested_first else listed[::-1])])
    err = capsys.readouterr().err
    assert rc == 2
    assert "judges one project per run" in err
    assert str(tree / "pyproject.toml") in err
    assert str(nested / "pyproject.toml") in err
    assert "files:" in err
    assert (tree / "demo" / "__init__.py").read_text(encoding="utf-8") == _REEXPORT


def test_relative_and_absolute_paths_of_one_project_are_one_project(
    tree: pathlib.Path, monkeypatch, capsys
) -> None:
    monkeypatch.chdir(tree)
    rc = cli.main(["--whole-project", "demo/__init__.py", str(tree / "consumer.py")])
    assert "checked 2 file(s)" in capsys.readouterr().out
    assert rc == 1


# -- .pre-commit-hooks.yaml -------------------------------------------------


def _manifest() -> list[dict[str, object]]:
    """The manifest, parsed by PyYAML in a child process.

    PyYAML is in the dev dependency group for this: libcst pulls it in on
    some Pythons only (on 3.13 it depends on ``pyyaml-ft`` instead). A child
    process, so this suite's type checkers never see the untyped ``yaml``
    module; JSON is the typed boundary.
    """
    code = "import json, sys, yaml; json.dump(yaml.safe_load(open(sys.argv[1])), sys.stdout)"
    out = subprocess.run(
        [sys.executable, "-c", code, str(MANIFEST)], capture_output=True, text=True, check=True
    ).stdout
    loaded: object = json.loads(out)
    assert isinstance(loaded, list)
    hooks: list[dict[str, object]] = []
    for item in loaded:
        assert isinstance(item, dict)
        hooks.append({str(k): v for k, v in item.items()})
    return hooks


def test_the_manifest_publishes_a_check_and_a_fix_hook() -> None:
    hooks = {str(h["id"]): h for h in _manifest()}
    assert sorted(hooks) == ["cleanporter", "cleanporter-fix"]
    for hook in hooks.values():
        assert hook["language"] == "python"
        assert hook["types"] == ["python"]
        # `types: [python]` also matches extensionless shebang scripts, which
        # the project walk never collects: they would be passed and unchecked.
        assert hook["files"] == r"\.py$"
        # Every run reads the whole tree: parallel batches would each re-read it.
        assert hook["require_serial"] is True
        assert hook.get("pass_filenames", True) is True
    assert "--fix" not in str(hooks["cleanporter"]["entry"])
    assert "--fix" in str(hooks["cleanporter-fix"]["entry"])


def test_the_manifest_entries_are_real_commands() -> None:
    scripts = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "scripts"
    ]
    flags = {
        option for action in cli.build_arg_parser()._actions for option in action.option_strings
    }
    for hook in _manifest():
        program, *args = shlex.split(str(hook["entry"]))
        assert program in scripts
        assert "--whole-project" in args, "a hook run on changed files needs the whole tree"
        assert set(args) <= flags
        # And they parse: the hook's own arguments plus a file, as pre-commit calls it.
        cli.build_arg_parser().parse_args([*args, "file.py"])


def test_prek_accepts_the_manifest() -> None:
    prek = shutil.which("prek", path=str(pathlib.Path(sys.executable).parent))
    if prek is None:
        pytest.skip("prek is not installed next to this interpreter")
    subprocess.run([prek, "validate-manifest", str(MANIFEST)], check=True, capture_output=True)

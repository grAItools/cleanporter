"""End-to-end CLI behaviour and exit codes."""

from __future__ import annotations

from pathlib import Path

import pytest

from cleanporter.cli import main


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    (tmp_path / "src" / "demo").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0"\n', encoding="utf-8"
    )
    (tmp_path / "src" / "demo" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "demo" / "helpers.py").write_text(
        "THING = 42\n", encoding="utf-8"
    )
    (tmp_path / "src" / "demo" / "consumer.py").write_text(
        "from demo.helpers import THING\ntotal = THING\n", encoding="utf-8"
    )
    return tmp_path


def test_check_reports_and_exits_1(project, capsys):
    rc = main([str(project / "src")])
    out = capsys.readouterr().out
    assert "consumer.py" in out and "CP001" in out
    assert rc == 1


def test_clean_tree_exits_0(project, capsys):
    (project / "src" / "demo" / "consumer.py").write_text(
        "from demo import helpers\ntotal = helpers.THING\n", encoding="utf-8"
    )
    assert main([str(project / "src")]) == 0


def test_fix_rewrites_and_exits_0(project, capsys):
    rc = main(["--fix", str(project / "src")])
    assert rc == 0
    assert (project / "src" / "demo" / "consumer.py").read_text(encoding="utf-8") == (
        "from demo import helpers\ntotal = helpers.THING\n"
    )
    assert "fixed" in capsys.readouterr().out


def test_diff_previews_without_writing(project, capsys):
    before = (project / "src" / "demo" / "consumer.py").read_text(encoding="utf-8")
    rc = main(["--diff", str(project / "src")])
    out = capsys.readouterr().out
    assert "-from demo.helpers import THING" in out
    assert (project / "src" / "demo" / "consumer.py").read_text(encoding="utf-8") == before
    assert rc == 1


def test_typing_imports_are_exempt(project, capsys):
    (project / "src" / "demo" / "consumer.py").write_text(
        "from typing import Any\nfrom collections.abc import Mapping\n"
        "x: Any = None\ny: Mapping = {}\n",
        encoding="utf-8",
    )
    assert main([str(project / "src")]) == 0


def test_exempt_flag_extends_the_allowlist(project, capsys):
    (project / "src" / "demo" / "consumer.py").write_text(
        "from demo.helpers import THING\n", encoding="utf-8"
    )
    assert main(["--exempt", "demo.helpers", str(project / "src")]) == 0


def test_exclude_config_is_respected(project, capsys):
    (project / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0"\n'
        '[tool.cleanporter]\nexclude = ["**/consumer.py"]\n',
        encoding="utf-8",
    )
    assert main([str(project / "src")]) == 0
    assert "consumer.py" not in capsys.readouterr().out


def test_syntax_error_exits_2(project, capsys):
    (project / "src" / "demo" / "broken.py").write_text("def (:\n", encoding="utf-8")
    assert main([str(project / "src")]) == 2


def test_bad_config_exits_2(project, capsys):
    (project / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0"\n'
        '[tool.cleanporter]\nscope = "nonsense"\n',
        encoding="utf-8",
    )
    assert main([str(project / "src")]) == 2
    assert "configuration error" in capsys.readouterr().err


def test_missing_path_warns_and_exits_0(project, capsys):
    rc = main([str(project / "nope")])
    captured = capsys.readouterr()
    assert "does not exist" in captured.out + captured.err
    assert rc == 0


def test_strict_promotes_unresolved_to_failure(project, capsys):
    (project / "src" / "demo" / "consumer.py").write_text(
        "from definitely_missing_pkg_xyz import thing\n", encoding="utf-8"
    )
    assert main([str(project / "src")]) == 0
    assert main(["--strict", str(project / "src")]) == 1

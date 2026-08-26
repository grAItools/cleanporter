"""End-to-end CLI tests."""

from __future__ import annotations

import pytest

from cleanporter.cli import main

from .conftest import make_project


@pytest.fixture()
def project(make_project):
    base = make_project()
    consumer = base / "src" / "mypkg" / "consumer.py"
    consumer.write_text(
        "from mypkg.helpers import THING\ntotal = THING\n",
        encoding="utf-8",
    )
    return base


def test_check_mode_reports_and_exits_1(project, capsys):
    rc = main([str(project / "src")])
    out = capsys.readouterr().out
    assert "consumer.py" in out
    assert "CP001" in out
    assert rc == 1


def test_fix_mode_rewrites_and_exits_0(project, capsys):
    target = project / "src" / "mypkg" / "consumer.py"
    rc = main(["--fix", str(project / "src")])
    assert rc == 0
    assert target.read_text(encoding="utf-8") == (
        "from mypkg import helpers\ntotal = helpers.THING\n"
    )
    out = capsys.readouterr().out
    assert "fixed" in out


def test_exclude_patterns_respected(make_project, capsys):
    base = make_project()
    (base / "src" / "mypkg" / "skipme.py").write_text(
        "from mypkg.helpers import THING\n", encoding="utf-8"
    )
    config_file = base / "pyproject.toml"
    config_file.write_text(
        '[project]\nname = "demo"\nversion = "0"\n'
        "[tool.cleanporter]\nexclude = [\"**/skipme.py\"]\n",
        encoding="utf-8",
    )
    rc = main([str(base / "src")])
    out = capsys.readouterr().out
    assert "skipme.py" not in out
    # helpers.py contains no violations; nothing else exists to flag.
    assert rc == 0


def test_scope_first_party_cli_config(tmp_path, make_project):
    base = make_project()
    (base / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0"\n'
        '[tool.cleanporter]\nscope = "first-party"\n',
        encoding="utf-8",
    )
    (base / "src" / "thirdparty.py").write_text(
        "from collections import OrderedDict\n",
        encoding="utf-8",
    )
    rc = main([str(base / "src")])
    assert rc == 0


def test_unknown_path_is_warning_not_crash(project, capsys):
    rc = main([str(project / "does-not-exist")])
    out = capsys.readouterr()
    assert "path does not exist" in out.err + out.out
    assert rc == 0


def test_syntax_error_yields_exit_2(make_project):
    base = make_project()
    (base / "src" / "mypkg" / "broken.py").write_text("def (:\n", encoding="utf-8")
    rc = main([str(base / "src")])
    assert rc == 2

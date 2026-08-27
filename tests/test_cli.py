"""End-to-end CLI behaviour and exit codes."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
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
    # progress and findings go to stderr while a patch is on stdout
    assert "fixed" in capsys.readouterr().err


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


def test_fix_still_reports_violations_it_declined(project, capsys):
    (project / "src" / "demo" / "consumer.py").write_text(
        "from demo.helpers import THING\n"
        '__all__ = ["THING"]\n',
        encoding="utf-8",
    )
    rc = main(["--fix", str(project / "src")])
    err = capsys.readouterr().err
    assert "CP003" in err, "the blocker must be explained"
    assert "CP001" in err, "the unfixed violation must still be reported"
    assert rc == 1


def test_fix_reports_nothing_for_a_fully_fixed_file(project, capsys):
    rc = main(["--fix", str(project / "src")])
    out = capsys.readouterr().out
    assert "CP001" not in out
    assert rc == 0


def test_summary_counts_match_the_printed_lines(project, capsys):
    (project / "src" / "demo" / "consumer.py").write_text(
        "from demo.helpers import THING\n"
        "from definitely_missing_pkg_xyz import other\n",
        encoding="utf-8",
    )
    main([str(project / "src")])
    out = capsys.readouterr().out
    assert out.count("CP001") == 1
    assert out.count("CP002") == 1
    assert "1 violation(s)" in out
    assert "1 unresolved" in out


def test_unanchorable_relative_import_is_counted(project, capsys):
    (project / "src" / "demo" / "consumer.py").write_text(
        "from ..... import nothing\n", encoding="utf-8"
    )
    main([str(project / "src")])
    out = capsys.readouterr().out
    assert "CP002" in out
    assert "0 unresolved" not in out


def test_strict_exits_1_for_unanchorable_relative_import(project, capsys):
    (project / "src" / "demo" / "consumer.py").write_text(
        "from ..... import nothing\n", encoding="utf-8"
    )
    assert main(["--strict", str(project / "src")]) == 1


def test_non_utf8_source_exits_2(project, capsys):
    (project / "src" / "demo" / "consumer.py").write_bytes(b"\xff\xfe# not utf-8\n")
    assert main([str(project / "src")]) == 2


def test_internal_rewrite_error_does_not_write_a_broken_file(project, monkeypatch):
    from cleanporter.model import Finding, Status
    from cleanporter.rewrite import FixOutcome

    target = project / "src" / "demo" / "consumer.py"
    before = target.read_text(encoding="utf-8")

    def fake(rec, resolver, config):
        return FixOutcome(
            "error", rec.source,
            [Finding(rec.path, 1, 0, "?", "?", Status.SKIPPED, "internal error")],
        )

    # cli imports fix_record by name, so patch it there.
    monkeypatch.setattr("cleanporter.cli.fix_record", fake)
    assert main(["--fix", str(project / "src")]) == 1
    assert target.read_text(encoding="utf-8") == before


# -- src layout, no path arguments (final review, Critical 1) ---------------


@pytest.fixture()
def src_layout(tmp_path: Path) -> Path:
    """A src-layout project with a `tests/` package, as most repos have."""
    (tmp_path / "src" / "mypkg").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "mypkg"\nversion = "0"\n', encoding="utf-8"
    )
    (tmp_path / "src" / "mypkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "mypkg" / "helpers.py").write_text(
        "class Widget:\n    pass\n", encoding="utf-8"
    )
    (tmp_path / "src" / "mypkg" / "consumer.py").write_text(
        "from .helpers import Widget\nw = Widget()\n", encoding="utf-8"
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "__init__.py").write_text("", encoding="utf-8")
    return tmp_path


def _imports_cleanly(project: Path) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, PYTHONPATH=str(project / "src"))
    return subprocess.run(
        [sys.executable, "-c", "import mypkg.consumer"],
        # cwd is *inside* src, so only the real import root is on sys.path --
        # running from the project root would make a bogus `src.` prefix
        # resolve as a namespace package and hide the bug.
        capture_output=True, text=True, env=env, cwd=project / "src",
    )


def test_fix_with_no_path_arguments_keeps_the_package_importable(src_layout, monkeypatch, capsys):
    monkeypatch.chdir(src_layout)
    assert _imports_cleanly(src_layout).returncode == 0, "fixture must import before --fix"

    main(["--fix"])
    capsys.readouterr()

    proc = _imports_cleanly(src_layout)
    assert proc.returncode == 0, proc.stderr
    after = (src_layout / "src" / "mypkg" / "consumer.py").read_text(encoding="utf-8")
    assert after == "from mypkg import helpers\nw = helpers.Widget()\n"


def test_check_on_a_src_layout_names_the_module_without_the_src_prefix(src_layout, monkeypatch, capsys):
    monkeypatch.chdir(src_layout)
    main([])
    out = capsys.readouterr().out + capsys.readouterr().err
    assert "src.mypkg" not in out

# -- output streams (final review, Important 5) -----------------------------


def test_diff_stdout_carries_only_the_patch(project, monkeypatch, capsys):
    monkeypatch.chdir(project)
    main(["--diff", "src"])
    captured = capsys.readouterr()
    assert captured.out.startswith("--- a/src/demo/consumer.py\n")
    assert "a//" not in captured.out
    assert "CP001" not in captured.out and "checked" not in captured.out
    for line in captured.out.splitlines():
        assert line[:1] in {"-", "+", "@", " "}, line
    assert "CP001" in captured.err
    assert "checked 3 file(s)" in captured.err


def test_diff_headers_are_relative_even_for_an_absolute_path_argument(project, monkeypatch, capsys):
    monkeypatch.chdir(project)
    main(["--diff", str(project / "src")])
    out = capsys.readouterr().out
    assert "--- a/src/demo/consumer.py" in out
    assert "a//" not in out


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_the_diff_can_be_applied_with_git_apply(project, monkeypatch, capsys):
    monkeypatch.chdir(project)
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    main(["--diff", "src"])
    patch = capsys.readouterr().out
    proc = subprocess.run(
        ["git", "apply", "--check", "-"], input=patch, text=True,
        capture_output=True, cwd=project,
    )
    assert proc.returncode == 0, proc.stderr


def test_warnings_go_to_stderr_when_a_patch_is_on_stdout(project, monkeypatch, capsys):
    monkeypatch.chdir(project)
    main(["--diff", "src", "nope"])
    captured = capsys.readouterr()
    assert "path does not exist" in captured.err
    assert "path does not exist" not in captured.out


def test_check_mode_still_reports_on_stdout(project, monkeypatch, capsys):
    monkeypatch.chdir(project)
    main(["src"])
    captured = capsys.readouterr()
    assert "CP001" in captured.out
    assert "checked 3 file(s)" in captured.out

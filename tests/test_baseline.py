"""``--select`` / ``--ignore`` and baselines (``--baseline``, ``--write-baseline``)."""

from __future__ import annotations

import json
import pathlib

import pytest

from cleanporter import _source, baseline, cli, config, engine

_CONSUMER = "from demo.helpers import THING\ntotal = THING\n"
#: A CP001 and a CP002 (a module no interpreter has).
_MIXED = "from demo.helpers import THING\nfrom no_such_pkg_cp import thing\ntotal = THING\n"


def _write(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture
def project(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """A project with one CP001 in ``src/demo/consumer.py``, and the cwd at its root."""
    _write(tmp_path / "pyproject.toml", '[project]\nname = "demo"\nversion = "0"\n')
    _write(tmp_path / "src" / "demo" / "__init__.py", "")
    _write(tmp_path / "src" / "demo" / "helpers.py", "THING = 42\nOTHER = 1\n")
    _write(tmp_path / "src" / "demo" / "consumer.py", _CONSUMER)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _consumer(project: pathlib.Path) -> pathlib.Path:
    return project / "src" / "demo" / "consumer.py"


# -- --select / --ignore -------------------------------------------------------


def test_select_reports_and_counts_only_the_selected_codes(
    project: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(_consumer(project), _MIXED)
    assert cli.main(["--select", "CP002", "src"]) == 0  # CP002 fails only under --strict
    out = capsys.readouterr().out
    assert "CP002" in out
    assert "CP001" not in out
    assert "0 violation(s)" in out
    assert cli.main(["--select", "CP002", "--strict", "src"]) == 1


def test_ignore_takes_a_code_out_of_the_report_and_the_exit_code(
    project: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["--ignore", "CP001", "src"]) == 0
    assert "CP001" not in capsys.readouterr().out


def test_codes_are_comma_separated_repeatable_and_case_insensitive(project: pathlib.Path) -> None:
    _write(_consumer(project), _MIXED)
    assert cli.main(["--ignore", "cp001, CP003", "--ignore", "CP002", "--strict", "src"]) == 0
    assert cli.main(["--select", "CP004,cp002", "--select", "CP001", "src"]) == 1


@pytest.mark.parametrize("flag", ["--select", "--ignore"])
@pytest.mark.parametrize("value", ["CP999", "CP001,E501", ""])
def test_an_unknown_code_is_a_usage_error(
    project: pathlib.Path, capsys: pytest.CaptureFixture[str], flag: str, value: str
) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main([flag, value, "src"])
    assert exc.value.code == 2
    assert flag in capsys.readouterr().err


def test_an_unknown_configured_code_exits_2(
    project: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with (project / "pyproject.toml").open("a", encoding="utf-8", newline="\n") as f:
        f.write('[tool.cleanporter]\nignore = ["CP042"]\n')
    assert cli.main(["src"]) == 2
    assert "unknown finding code(s) CP042" in capsys.readouterr().err


def test_the_command_line_replaces_the_configured_codes(
    project: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with (project / "pyproject.toml").open("a", encoding="utf-8", newline="\n") as f:
        f.write('[tool.cleanporter]\nselect = ["CP002"]\nignore = ["CP001"]\n')
    assert cli.main(["src"]) == 0
    assert "CP001" not in capsys.readouterr().out
    # --select replaces select; the configured ignore still applies ...
    assert cli.main(["--select", "CP001", "src"]) == 0
    # ... until --ignore replaces it too.
    assert cli.main(["--select", "CP001", "--ignore", "CP004", "src"]) == 1
    assert "CP001" in capsys.readouterr().out


def test_ignoring_cp001_does_not_change_what_fix_rewrites(
    project: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["--fix", "--ignore", "CP001", "src"]) == 0
    assert _consumer(project).read_text(encoding="utf-8") == (
        "from demo import helpers\ntotal = helpers.THING\n"
    )
    assert "fixed:" in capsys.readouterr().err


def test_the_engine_applies_select_and_ignore(project: pathlib.Path) -> None:
    _write(_consumer(project), _MIXED)
    cfg = config.Config(root=project, ignore=frozenset({"CP002"}))
    result = engine.run([pathlib.Path("src")], cfg)
    assert [f.code for f in result.findings] == ["CP001"]
    assert result.unresolved == 0


# -- baselines ---------------------------------------------------------------


def _write_baseline(*extra: str) -> int:
    return cli.main(["--write-baseline", "baseline.json", *extra, "src"])


def _baseline(project: pathlib.Path) -> dict[str, object]:
    document: dict[str, object] = json.loads((project / "baseline.json").read_text("utf-8"))
    return document


def test_write_baseline_records_the_findings_and_exits_0(
    project: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _write_baseline() == 0
    err = capsys.readouterr().err
    assert "wrote 1 finding(s) to baseline.json" in err
    assert "CP002" not in err
    assert _baseline(project) == {
        "version": 1,
        "findings": [
            {
                "path": "src/demo/consumer.py",
                "code": "CP001",
                "parent": "demo.helpers",
                "name": "THING",
            }
        ],
    }
    raw = (project / "baseline.json").read_bytes()
    assert raw.endswith(b"}\n")
    assert b"\r" not in raw


def test_the_baseline_file_is_deterministic(project: pathlib.Path) -> None:
    _write(project / "src" / "demo" / "b.py", _MIXED)
    _write(project / "src" / "demo" / "a.py", _MIXED)
    assert _write_baseline() == 0
    first = (project / "baseline.json").read_bytes()
    assert _write_baseline() == 0
    assert (project / "baseline.json").read_bytes() == first
    findings = _baseline(project)["findings"]
    assert isinstance(findings, list)
    assert findings == sorted(findings, key=lambda e: tuple(e.values()))


def test_a_baselined_finding_is_neither_reported_nor_counted(
    project: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _write_baseline() == 0
    capsys.readouterr()
    assert cli.main(["--baseline", "baseline.json", "src"]) == 0
    out = capsys.readouterr().out
    assert "CP001" not in out
    assert "0 violation(s)" in out
    assert "1 in the baseline" in out


def test_a_line_shift_keeps_the_finding_baselined(project: pathlib.Path) -> None:
    assert _write_baseline() == 0
    _write(_consumer(project), '"""Docs."""\n\nimport os\n\n' + _CONSUMER + "print(os)\n")
    assert cli.main(["--baseline", "baseline.json", "src"]) == 0


@pytest.mark.parametrize(
    "spelling",
    [
        "from  demo.helpers   import THING",
        "from demo.helpers import (THING)",
        "from demo.helpers import (\n    THING,\n)",
        "from demo.helpers \\\n    import THING",
        "from .helpers import THING",
    ],
    ids=["whitespace", "parentheses", "exploded", "continuation", "relative"],
)
def test_reformatting_the_statement_keeps_the_finding_baselined(
    project: pathlib.Path, spelling: str
) -> None:
    assert _write_baseline() == 0
    _write(_consumer(project), f"{spelling}\ntotal = THING\n")
    assert cli.main(["--baseline", "baseline.json", "src"]) == 0


def test_adding_a_name_reports_only_the_new_name(
    project: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _write_baseline() == 0
    _write(_consumer(project), "from demo.helpers import OTHER, THING\ntotal = THING + OTHER\n")
    capsys.readouterr()
    assert cli.main(["--baseline", "baseline.json", "src"]) == 1
    captured = capsys.readouterr()
    assert "'OTHER'" in captured.out
    assert "'THING'" not in captured.out
    assert "baseline entr" not in captured.err


def test_importing_another_name_brings_the_finding_back(
    project: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _write_baseline() == 0
    _write(_consumer(project), "from demo.helpers import OTHER\ntotal = OTHER\n")
    capsys.readouterr()
    assert cli.main(["--baseline", "baseline.json", "src"]) == 1
    captured = capsys.readouterr()
    assert "'OTHER'" in captured.out
    # The THING entry now matches nothing.
    assert "1 baseline entry matches no finding" in captured.err


def test_a_fixed_finding_leaves_a_stale_entry_noted_not_failed(
    project: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _write_baseline() == 0
    _write(_consumer(project), "from demo import helpers\ntotal = helpers.THING\n")
    capsys.readouterr()
    assert cli.main(["--baseline", "baseline.json", "src"]) == 0
    captured = capsys.readouterr()
    assert "cleanporter: note: 1 baseline entry matches no finding any more" in captured.err
    assert "--write-baseline" in captured.err
    assert "note" not in captured.out


def test_entries_for_files_or_codes_outside_the_run_are_not_stale(
    project: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _write_baseline() == 0
    capsys.readouterr()
    helpers = str(project / "src" / "demo" / "helpers.py")
    assert cli.main(["--baseline", "baseline.json", helpers]) == 0
    assert cli.main(["--baseline", "baseline.json", "--select", "CP002", "src"]) == 0
    assert "baseline entr" not in capsys.readouterr().err


def test_identical_findings_are_a_multiset(
    project: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    twice = "def f():\n    from demo.helpers import THING\n    return THING\n"
    _write(_consumer(project), twice + "\n\n" + twice.replace("f()", "g()"))
    assert _write_baseline() == 0
    findings = _baseline(project)["findings"]
    assert isinstance(findings, list)
    assert len(findings) == 2
    assert findings[0] == findings[1]
    assert cli.main(["--baseline", "baseline.json", "src"]) == 0
    # A third copy of the same statement is new.
    _write(_consumer(project), (twice + "\n\n") * 3)
    capsys.readouterr()
    assert cli.main(["--baseline", "baseline.json", "src"]) == 1
    assert capsys.readouterr().out.count("CP001") == 1


def test_json_counts_reflect_the_baseline(
    project: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(_consumer(project), _MIXED)
    assert _write_baseline("--select", "CP001") == 0
    capsys.readouterr()
    assert cli.main(["--format", "json", "--baseline", "baseline.json", "src"]) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["exit_code"] == 0
    assert document["counts"]["violations"] == 0
    assert document["counts"]["unresolved"] == 1
    assert document["counts"]["baselined"] == 1
    assert document["counts"]["stale_baseline"] == 0
    assert [f["code"] for f in document["findings"]] == ["CP002"]
    # Without a baseline the two counts are absent, not zero.
    assert cli.main(["--format", "json", "src"]) == 1
    counts = json.loads(capsys.readouterr().out)["counts"]
    assert "baselined" not in counts
    assert "stale_baseline" not in counts


def test_write_baseline_honours_select_and_ignore(project: pathlib.Path) -> None:
    _write(_consumer(project), _MIXED)
    assert _write_baseline("--ignore", "CP001") == 0
    findings = _baseline(project)["findings"]
    assert isinstance(findings, list)
    assert [e["code"] for e in findings] == ["CP002"]
    # So the CP001 it left out is still reported.
    assert cli.main(["--baseline", "baseline.json", "src"]) == 1


def test_write_baseline_ignores_the_existing_baseline(project: pathlib.Path) -> None:
    assert _write_baseline() == 0
    _write(project / "src" / "demo" / "more.py", "from demo.helpers import OTHER\n")
    assert _write_baseline("--baseline", "baseline.json") == 0
    findings = _baseline(project)["findings"]
    assert isinstance(findings, list)
    assert len(findings) == 2


@pytest.mark.parametrize("mode", ["--fix", "--diff"])
def test_write_baseline_is_refused_with_fix_or_diff(
    project: pathlib.Path, capsys: pytest.CaptureFixture[str], mode: str
) -> None:
    before = _consumer(project).read_bytes()
    with pytest.raises(SystemExit) as exc:
        _write_baseline(mode)
    assert exc.value.code == 2
    assert "--write-baseline" in capsys.readouterr().err
    assert not (project / "baseline.json").exists()
    assert _consumer(project).read_bytes() == before


def test_write_baseline_refuses_a_run_with_unreadable_files(
    project: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(project / "src" / "demo" / "broken.py", "def (:\n")
    assert _write_baseline() == 2
    assert "baseline not written" in capsys.readouterr().err
    assert not (project / "baseline.json").exists()


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (None, "cannot read baseline"),
        ("[1, 2", "not JSON"),
        ('{"version": 2, "findings": []}', "unsupported baseline version 2"),
        ('{"version": 1, "findings": [{"path": "x"}]}', r"findings\[1\]\.code must be a string"),
    ],
)
def test_a_bad_baseline_exits_2(
    project: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    content: str | None,
    message: str,
) -> None:
    if content is not None:
        _write(project / "baseline.json", content)
    assert cli.main(["--baseline", "baseline.json", "src"]) == 2
    err = capsys.readouterr().err
    assert "cleanporter: error: " in err
    with pytest.raises(baseline.BaselineError, match=message):
        baseline.load(project / "baseline.json")


def test_the_configured_baseline_is_relative_to_pyproject(
    project: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert cli.main(["--write-baseline", "ci/baseline.json", "src"]) == 2  # no such directory
    (project / "ci").mkdir()
    assert cli.main(["--write-baseline", "ci/baseline.json", "src"]) == 0
    with (project / "pyproject.toml").open("a", encoding="utf-8", newline="\n") as f:
        f.write('[tool.cleanporter]\nbaseline = "ci/baseline.json"\n')
    monkeypatch.chdir(project / "src")
    assert cli.main(["demo"]) == 0


def test_path_keys_are_relative_to_the_root_whatever_the_cwd(
    project: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(project / "src" / "demo")
    assert cli.main(["--write-baseline", str(project / "baseline.json"), "."]) == 0
    findings = _baseline(project)["findings"]
    assert isinstance(findings, list)
    # POSIX separators on every platform, so one baseline serves Windows and Linux.
    assert [e["path"] for e in findings] == ["src/demo/consumer.py"]
    monkeypatch.chdir(project)
    assert cli.main(["--baseline", "baseline.json", str(_consumer(project))]) == 0


def test_relative_path_uses_posix_separators(tmp_path: pathlib.Path) -> None:
    nested = tmp_path / "a" / "b" / "c.py"
    assert baseline.relative_path(nested, tmp_path) == "a/b/c.py"


def test_a_whole_project_run_uses_the_same_keys(
    project: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    listed = ["--whole-project", "src/demo/consumer.py"]
    assert cli.main(["--write-baseline", "baseline.json", *listed]) == 0
    assert cli.main(["--baseline", "baseline.json", *listed]) == 0
    # A baseline written by a full run serves a whole-project run too.
    assert _write_baseline() == 0
    capsys.readouterr()
    assert cli.main(["--baseline", "baseline.json", *listed]) == 0
    assert "baseline entr" not in capsys.readouterr().err


def test_apply_is_a_pure_function_of_the_result(project: pathlib.Path) -> None:
    cfg = config.Config(root=project)
    result = engine.run([pathlib.Path("src")], cfg)
    accepted = baseline.entries(result, project)
    applied = baseline.apply(result, accepted, cfg)
    assert applied.findings == ()
    assert (applied.baselined, applied.stale_baseline) == (1, 0)
    assert applied.exit_code() == 0
    assert result.findings  # untouched
    assert result.baselined is None
    stale = baseline.apply(result, [*accepted, *accepted], cfg)
    assert stale.stale_baseline == 1
    assert stale.notes[-1] == baseline.stale_note(1)


def test_cp004_is_never_recorded(project: pathlib.Path) -> None:
    with (project / "pyproject.toml").open("a", encoding="utf-8", newline="\n") as f:
        f.write("[tool.cleanporter]\nskip = [{ file = 'src/demo/consumer\\.py' }]\n")
    assert _write_baseline() == 0
    assert _baseline(project)["findings"] == []


# -- check runs only ------------------------------------------------------------


@pytest.mark.parametrize("mode", ["--fix", "--diff"])
def test_an_explicit_baseline_is_refused_with_fix_or_diff(
    project: pathlib.Path, capsys: pytest.CaptureFixture[str], mode: str
) -> None:
    assert _write_baseline() == 0
    before = _consumer(project).read_bytes()
    with pytest.raises(SystemExit) as exc:
        cli.main([mode, "--baseline", "baseline.json", "src"])
    assert exc.value.code == 2
    assert "--baseline applies to check runs only" in capsys.readouterr().err
    assert _consumer(project).read_bytes() == before


def test_a_configured_baseline_is_skipped_with_a_note_under_diff(
    project: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _write_baseline() == 0
    with (project / "pyproject.toml").open("a", encoding="utf-8", newline="\n") as f:
        f.write('[tool.cleanporter]\nbaseline = "baseline.json"\n')
    capsys.readouterr()
    assert cli.main(["--diff", "src"]) == 1
    err = capsys.readouterr().err
    assert "configured baseline is not applied under --fix or --diff" in err
    assert "CP001" in err
    assert "in the baseline" not in err
    # A check run still applies it.
    assert cli.main(["src"]) == 0


def test_the_library_refuses_a_baseline_for_other_modes(project: pathlib.Path) -> None:
    cfg = config.Config(root=project)
    result = engine.run([pathlib.Path("src")], cfg, engine.Mode.DIFF)
    with pytest.raises(ValueError, match="not a diff run"):
        baseline.apply(result, [], cfg)
    with pytest.raises(ValueError, match="not a diff run"):
        baseline.entries(result, project)


# -- identity across runs --------------------------------------------------------


def test_cp001_and_cp003_match_each_other(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A re-export is load-bearing only in a run that includes its importer.

    In ``demo/api.py``, not ``demo/__init__.py``: a module-level import there
    is the package's public surface, compliant in every run.
    """
    _write(tmp_path / "pyproject.toml", '[project]\nname = "demo"\n')
    _write(tmp_path / "demo" / "__init__.py", "")
    _write(tmp_path / "demo" / "core.py", "def helper():\n    return 1\n")
    _write(
        tmp_path / "demo" / "api.py",
        "from demo.core import helper\n\n\ndef run():\n    return helper()\n",
    )
    _write(tmp_path / "consumer.py", "import demo.api\n\ndemo.api.helper()\n")
    monkeypatch.chdir(tmp_path)
    cfg = config.Config(root=tmp_path)
    whole = engine.run([pathlib.Path()], cfg)
    part = engine.run([pathlib.Path("demo")], cfg)
    codes = [f.code for f in whole.findings], [f.code for f in part.findings]
    assert codes == (["CP003"], ["CP001"])
    assert cli.main(["--write-baseline", "baseline.json", "."]) == 0
    assert cli.main(["--baseline", "baseline.json", "demo"]) == 0
    assert cli.main(["--baseline", "baseline.json", "."]) == 0


def test_a_baseline_needs_a_pyproject(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(tmp_path / "pkg" / "helpers.py", "THING = 1\n")
    _write(tmp_path / "pkg" / "mod.py", "from pkg.helpers import THING\n")
    _write(tmp_path / "baseline.json", '{"version": 1, "findings": []}\n')
    monkeypatch.chdir(tmp_path)
    assert cli.main(["--write-baseline", "new.json", "pkg"]) == 2
    assert cli.main(["--baseline", "baseline.json", "pkg"]) == 2
    assert "need a pyproject.toml" in capsys.readouterr().err
    assert not (tmp_path / "new.json").exists()


def test_write_baseline_is_refused_with_a_structured_format(
    project: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        _write_baseline("--format", "json")
    assert exc.value.code == 2
    assert "--format" in capsys.readouterr().err
    assert not (project / "baseline.json").exists()


def test_write_baseline_notes_the_unresolved_it_records(
    project: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(_consumer(project), _MIXED)
    assert _write_baseline() == 0
    err = capsys.readouterr().err
    assert "wrote 2 finding(s)" in err
    assert "1 of them are CP002" in err


def test_write_baseline_replaces_an_existing_file(project: pathlib.Path) -> None:
    _write(project / "baseline.json", "stale contents\n")
    assert _write_baseline() == 0
    assert _baseline(project)["version"] == 1


def test_a_failed_write_leaves_no_new_file_behind(
    project: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(path: pathlib.Path, data: bytes) -> None:
        raise OSError(28, "No space left on device", str(path))

    monkeypatch.setattr(_source, "write_atomic", fail)
    assert _write_baseline() == 2
    assert not (project / "baseline.json").exists()


@pytest.mark.parametrize(
    "rewritten",
    [
        "from demo.helpers import THING as T\ntotal = T\n",
        "def f():\n    from demo.helpers import THING\n    return THING\n",
    ],
    ids=["alias", "scope"],
)
def test_an_alias_or_a_scope_change_keeps_the_finding_baselined(
    project: pathlib.Path, rewritten: str
) -> None:
    assert _write_baseline() == 0
    _write(_consumer(project), rewritten)
    assert cli.main(["--baseline", "baseline.json", "src"]) == 0


# -- CP005, unused inline suppressions -----------------------------------------

#: A CP001 on line 1 and an unused suppression (CP005) on line 2.
_UNUSED = (
    "from demo.helpers import THING\n"
    "from demo import helpers  # cleanporter: ignore[CP001]\n"
    "total = THING, helpers\n"
)


def test_select_and_ignore_filter_cp005(
    project: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(_consumer(project), _UNUSED)
    assert cli.main(["--ignore", "CP001", "src"]) == 1  # the CP005 still fails the run
    assert "CP005" in capsys.readouterr().out
    assert cli.main(["--ignore", "CP005", "src"]) == 1  # the CP001 still does
    out = capsys.readouterr().out
    assert "CP005" not in out
    assert "unused suppression" not in out
    assert cli.main(["--ignore", "CP001,CP005", "src"]) == 0
    capsys.readouterr()
    assert cli.main(["--select", "CP005", "src"]) == 1
    out = capsys.readouterr().out
    assert "CP005" in out
    assert ": CP001 " not in out


def test_a_cp005_is_never_baselined(
    project: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A stale suppression is always reported: a baseline would let it swallow a new finding."""
    _write(_consumer(project), _UNUSED)
    assert _write_baseline() == 0
    assert _baseline(project)["findings"] == [
        {
            "path": "src/demo/consumer.py",
            "code": "CP001",
            "parent": "demo.helpers",
            "name": "THING",
        },
    ]
    capsys.readouterr()
    assert cli.main(["--baseline", "baseline.json", "src"]) == 1
    out = capsys.readouterr().out
    assert "CP005" in out
    assert ": CP001 " not in out
    assert "1 in the baseline" in out
    # Deleting the stale comment is the fix.
    _write(_consumer(project), _UNUSED.replace("  # cleanporter: ignore[CP001]", ""))
    assert cli.main(["--baseline", "baseline.json", "src"]) == 0


def test_no_entry_accepts_a_cp005(project: pathlib.Path) -> None:
    """Not a hand-written ``CP005`` entry, nor a ``CP001`` or ``CP003`` one; each is stale."""
    _write(_consumer(project), _UNUSED)
    cfg = config.Config(root=project)
    result = engine.run([pathlib.Path("src")], cfg)
    assert sorted(f.code for f in result.findings) == ["CP001", "CP005"]
    path = "src/demo/consumer.py"
    entries = [baseline.Entry(path, code, "", "") for code in ("CP001", "CP003", "CP005")]
    kept = baseline.apply(result, entries, cfg)
    assert sorted(f.code for f in kept.findings) == ["CP001", "CP005"]
    assert (kept.baselined, kept.stale_baseline) == (0, 3)
    # The real CP001 entry is still accepted beside them.
    real = baseline.entries(result, project)
    assert [e.code for e in real] == ["CP001"]
    kept = baseline.apply(result, [*real, *entries], cfg)
    assert [f.code for f in kept.findings] == ["CP005"]

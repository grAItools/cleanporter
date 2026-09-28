"""Which interpreter the probe asks: named, cleanporter's own, or the project's, detected."""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import venv

import pytest

from cleanporter import _interpreter, cli, config, engine, model

posix_only = pytest.mark.skipif(os.name != "posix", reason="uses bin/python venv layouts")


def _fake_venv(venv_dir: pathlib.Path) -> pathlib.Path:
    """A venv layout whose interpreter is a symlink to this one, so it really runs."""
    python = venv_dir / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    return python


def _stub_venv(venv_dir: pathlib.Path) -> pathlib.Path:
    """A venv layout whose interpreter fails, so the probe warning names what ran."""
    python = venv_dir / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\necho 'stub interpreter ran' >&2\nexit 1\n", encoding="utf-8")
    python.chmod(0o755)
    return python


# -- which interpreter is "this one" -------------------------------------------


def test_this_interpreter_is_its_own_executable_path():
    assert _interpreter.is_this_interpreter(sys.executable) is True


def test_a_symlink_to_this_interpreter_is_not_this_interpreter(tmp_path):
    """A venv's ``bin/python`` is exactly such a symlink, to another environment."""
    link = tmp_path / "python"
    link.symlink_to(sys.executable)
    assert _interpreter.is_this_interpreter(str(link)) is False


def test_a_path_through_dotdot_is_not_this_interpreter():
    """``..`` after a symlinked directory runs something else; never collapse it."""
    here = pathlib.Path(sys.executable)
    spelled = str(here.parent / ".." / here.parent.name / here.name)
    assert _interpreter.is_this_interpreter(spelled) is False


def test_a_bare_command_name_is_not_this_interpreter(monkeypatch):
    """Even from the interpreter's own directory: a subprocess looks it up on PATH."""
    here = pathlib.Path(sys.executable)
    monkeypatch.chdir(here.parent)
    assert _interpreter.is_this_interpreter(here.name) is False


# -- choose: named and sentinel values -----------------------------------------


@posix_only
def test_a_named_interpreter_is_used_as_written_even_if_missing(tmp_path):
    """A request, not a search: its failure is the probe's to report."""
    _fake_venv(tmp_path / ".venv")
    missing = str(tmp_path / "nowhere" / "python")
    assert _interpreter.choose(missing, tmp_path) == _interpreter.Choice(missing)
    assert _interpreter.choose("python3", tmp_path) == _interpreter.Choice("python3")


@posix_only
def test_self_is_cleanporters_own_interpreter_even_with_a_venv_present(tmp_path, monkeypatch):
    _fake_venv(tmp_path / ".venv")
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / ".venv"))
    assert _interpreter.choose("self", tmp_path) == _interpreter.Choice(None)


@pytest.mark.parametrize("setting", [None, "auto"])
def test_detection_finding_nothing_keeps_cleanporters_own(tmp_path, setting):
    assert _interpreter.choose(setting, tmp_path) == _interpreter.Choice(None)


# -- choose: detection, in order ------------------------------------------------


@posix_only
@pytest.mark.parametrize("setting", [None, "auto"])
def test_the_projects_dot_venv_is_detected(tmp_path, setting):
    python = _fake_venv(tmp_path / ".venv")
    choice = _interpreter.choose(setting, tmp_path)
    assert choice.python == str(python)
    assert choice.note is not None
    assert str(python) in choice.note
    assert f"the .venv in the project root {tmp_path}" in choice.note
    assert "python = 'self'" in choice.note


@posix_only
def test_the_projects_dot_venv_wins_over_an_active_virtual_env(tmp_path, monkeypatch):
    """uv's order: a stale activated shell does not pick the project's packages."""
    project = _fake_venv(tmp_path / ".venv")
    _fake_venv(tmp_path / "elsewhere")
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "elsewhere"))
    choice = _interpreter.choose(None, tmp_path)
    assert choice.python == str(project)
    assert choice.note is not None
    assert f"not from the active $VIRTUAL_ENV {tmp_path / 'elsewhere'}" in choice.note


@posix_only
def test_virtual_env_is_used_when_the_project_has_no_environment(tmp_path, monkeypatch):
    active = _fake_venv(tmp_path / "elsewhere")
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "elsewhere"))
    choice = _interpreter.choose(None, tmp_path / "proj")
    assert choice.python == str(active)
    assert choice.note is not None
    assert "found from $VIRTUAL_ENV;" in choice.note
    assert "not from the active" not in choice.note


@posix_only
def test_an_active_virtual_env_that_is_the_projects_is_not_called_passed_over(
    tmp_path, monkeypatch
):
    _fake_venv(tmp_path / ".venv")
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / ".venv"))
    note = _interpreter.choose(None, tmp_path).note
    assert note is not None
    assert "not from the active" not in note


@posix_only
def test_uv_project_environment_is_read_against_the_root(tmp_path, monkeypatch):
    _fake_venv(tmp_path / ".venv")
    uv_env = _fake_venv(tmp_path / "envs" / "proj")
    monkeypatch.chdir(tmp_path / "envs")  # not against the cwd
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", "envs/proj")
    choice = _interpreter.choose(None, tmp_path)
    assert choice.python == str(uv_env)
    assert choice.note is not None
    assert "$UV_PROJECT_ENVIRONMENT" in choice.note


@posix_only
def test_uv_project_environment_wins_over_an_active_virtual_env(tmp_path, monkeypatch):
    _fake_venv(tmp_path / "active")
    uv_env = _fake_venv(tmp_path / "uv")
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "active"))
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", str(tmp_path / "uv"))
    assert _interpreter.choose(None, tmp_path).python == str(uv_env)


@posix_only
def test_candidates_that_cannot_run_are_passed_over_silently(tmp_path, monkeypatch):
    """A stale variable, a broken link and a non-executable file are all misses."""
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "gone"))
    broken = tmp_path / "broken" / "bin" / "python"
    broken.parent.mkdir(parents=True)
    broken.symlink_to(tmp_path / "no-such-python")
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", str(tmp_path / "broken"))
    plain = tmp_path / ".venv" / "bin" / "python"
    plain.parent.mkdir(parents=True)
    plain.write_text("", encoding="utf-8")
    plain.chmod(0o644)
    assert _interpreter.choose(None, tmp_path) == _interpreter.Choice(None)
    plain.chmod(0o755)
    assert _interpreter.choose(None, tmp_path).python == str(plain)


def test_empty_variables_are_no_candidates(tmp_path, monkeypatch):
    monkeypatch.setenv("VIRTUAL_ENV", "")
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", "")
    assert _interpreter.choose(None, tmp_path) == _interpreter.Choice(None)


@posix_only
def test_detecting_cleanporters_own_interpreter_stays_in_process(tmp_path, monkeypatch):
    """The ``uv run cleanporter`` case: the path is ``sys.executable``, nothing to say."""
    python = _fake_venv(tmp_path / ".venv")
    monkeypatch.setattr(sys, "executable", str(python))
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / ".venv"))
    assert _interpreter.choose(None, tmp_path) == _interpreter.Choice(None)


def _running_in(monkeypatch: pytest.MonkeyPatch, venv_dir: pathlib.Path) -> None:
    """Pretend cleanporter runs as *venv_dir*'s ``bin/python3`` (``uv run python``)."""
    monkeypatch.setattr(sys, "executable", str(venv_dir / "bin" / "python3"))
    monkeypatch.setattr(sys, "prefix", str(venv_dir))
    monkeypatch.setattr(sys, "base_prefix", "/usr")


@posix_only
def test_a_detected_venv_that_is_sys_prefix_stays_in_process(tmp_path, monkeypatch):
    """``bin/python3`` running, ``bin/python`` detected: one environment, no note."""
    _fake_venv(tmp_path / ".venv")
    _running_in(monkeypatch, tmp_path / ".venv")
    assert _interpreter.choose(None, tmp_path) == _interpreter.Choice(None)


@posix_only
def test_the_prefix_rule_is_not_applied_to_a_named_interpreter(tmp_path, monkeypatch):
    """A name keeps the strict path-identity rule: a miss costs a subprocess, nothing else."""
    python = _fake_venv(tmp_path / ".venv")
    _running_in(monkeypatch, tmp_path / ".venv")
    assert _interpreter.choose(str(python), tmp_path).python == str(python)


@posix_only
def test_the_prefix_rule_needs_a_virtual_environment(tmp_path, monkeypatch):
    """Outside a venv ``sys.prefix`` is a base install, which a detected venv never is."""
    python = _fake_venv(tmp_path / ".venv")
    _running_in(monkeypatch, tmp_path / ".venv")
    monkeypatch.setattr(sys, "base_prefix", str(tmp_path / ".venv"))
    assert _interpreter.choose(None, tmp_path).python == str(python)


# -- choose: uv workspaces ----------------------------------------------------------


def _workspace(tmp_path: pathlib.Path, table: str) -> pathlib.Path:
    """A workspace root at *tmp_path* declaring *table*, with a member ``packages/app``."""
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "ws"\n[tool.uv.workspace]\n{table}\n', encoding="utf-8"
    )
    member = tmp_path / "packages" / "app"
    member.mkdir(parents=True)
    (member / "pyproject.toml").write_text('[project]\nname = "app"\n', encoding="utf-8")
    return member


@posix_only
@pytest.mark.parametrize("members", ['["packages/*"]', '["packages/app"]', '["**"]'])
def test_a_workspace_members_environment_is_the_workspace_roots(tmp_path, members):
    member = _workspace(tmp_path, f"members = {members}")
    python = _fake_venv(tmp_path / ".venv")
    choice = _interpreter.choose(None, member)
    assert choice.python == str(python)
    assert choice.note is not None
    assert f"the .venv in the uv workspace root {tmp_path}" in choice.note


@posix_only
def test_uv_project_environment_is_read_against_the_workspace_root(tmp_path, monkeypatch):
    member = _workspace(tmp_path, 'members = ["packages/*"]')
    python = _fake_venv(tmp_path / "envs" / "ws")
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", "envs/ws")
    assert _interpreter.choose(None, member).python == str(python)


@posix_only
def test_a_members_own_dot_venv_is_tried_first(tmp_path):
    member = _workspace(tmp_path, 'members = ["packages/*"]')
    _fake_venv(tmp_path / ".venv")
    own = _fake_venv(member / ".venv")
    assert _interpreter.choose(None, member).python == str(own)


@posix_only
@pytest.mark.parametrize(
    "table",
    [
        'members = ["libs/*"]',  # not a member
        'members = ["*"]',  # `*` does not cross a `/`
        'members = ["packages/*"]\nexclude = ["packages/app"]',
        "",  # no members at all
    ],
)
def test_a_project_that_is_not_a_member_does_not_use_the_workspace(tmp_path, table):
    member = _workspace(tmp_path, table)
    _fake_venv(tmp_path / ".venv")
    assert _interpreter.choose(None, member) == _interpreter.Choice(None)


@posix_only
def test_the_workspace_walk_stops_at_the_nearest_declaring_ancestor(tmp_path):
    """A non-matching inner workspace hides an outer one that would match."""
    outer_member = _workspace(tmp_path, 'members = ["**"]')
    (outer_member / "pyproject.toml").write_text(
        '[project]\nname = "app"\n[tool.uv.workspace]\nmembers = ["other/*"]\n',
        encoding="utf-8",
    )
    project = outer_member / "sub"
    project.mkdir()
    _fake_venv(tmp_path / ".venv")
    assert _interpreter.choose(None, project) == _interpreter.Choice(None)


@posix_only
def test_an_unreadable_ancestor_pyproject_is_passed_over(tmp_path):
    (tmp_path / "pyproject.toml").write_text("this is [not toml", encoding="utf-8")
    project = tmp_path / "proj"
    project.mkdir()
    _fake_venv(tmp_path / ".venv")
    assert _interpreter.choose(None, project) == _interpreter.Choice(None)


# -- config and command line ------------------------------------------------------


@pytest.mark.parametrize("value", ["auto", "self"])
def test_the_sentinels_are_not_anchored_as_paths(tmp_path, value):
    (tmp_path / "pyproject.toml").write_text(
        f'[tool.cleanporter]\npython = "{value}"\n', encoding="utf-8"
    )
    assert config.load_config(tmp_path).python == value


@posix_only
def test_the_default_config_detects(tmp_path):
    """No key, no flag: a run under a plain `Config` probes the project's `.venv`."""
    assert config.Config().python is None
    python = _stub_venv(tmp_path / ".venv")
    (tmp_path / "app.py").write_text("from functools import partial\n", encoding="utf-8")
    result = engine.run([tmp_path / "app.py"], config.Config(root=tmp_path))
    assert len(result.notes) == 1
    assert str(python) in result.notes[0]
    assert any(f"interpreter probe '{python}'" in w for w in result.warnings)


_PROJECT = '[project]\nname = "demo"\nversion = "0"\n'


def _stub_project(tmp_path: pathlib.Path, table: str = "") -> pathlib.Path:
    """A project with a stdlib import to probe, and a failing stub as its ``.venv``."""
    (tmp_path / "pyproject.toml").write_text(_PROJECT + table, encoding="utf-8")
    (tmp_path / "app.py").write_text(
        "from functools import partial\nf = partial(print)\n", encoding="utf-8"
    )
    _stub_venv(tmp_path / ".venv")
    return tmp_path


@posix_only
def test_the_cli_detects_the_dot_venv_and_says_so(tmp_path, monkeypatch, capsys):
    project = _stub_project(tmp_path)
    monkeypatch.chdir(project)
    cli.main(["."])
    captured = capsys.readouterr()
    python = project / ".venv" / "bin" / "python"
    expected = f"cleanporter: note: classifying stdlib and third-party imports with {python}"
    assert expected in captured.err
    assert f"interpreter probe '{python}'" in captured.out  # it really was the one probed
    assert "stub interpreter ran" in captured.out


@posix_only
def test_python_self_in_config_turns_detection_off(tmp_path, monkeypatch, capsys):
    project = _stub_project(tmp_path, '[tool.cleanporter]\npython = "self"\n')
    monkeypatch.chdir(project)
    rc = cli.main(["."])
    captured = capsys.readouterr()
    assert "note:" not in captured.err
    assert "stub interpreter ran" not in captured.out
    assert "CP001" in captured.out
    assert rc == 1


@posix_only
def test_the_python_flag_beats_the_config_key(tmp_path, monkeypatch, capsys):
    """CLI over config over detection: ``--python self`` overrides a configured path."""
    project = _stub_project(tmp_path, '[tool.cleanporter]\npython = ".venv/bin/python"\n')
    monkeypatch.chdir(project)
    cli.main(["--python", "self", "."])
    assert "stub interpreter ran" not in capsys.readouterr().out


@posix_only
def test_python_auto_on_the_command_line_beats_a_configured_self(tmp_path, monkeypatch, capsys):
    project = _stub_project(tmp_path, '[tool.cleanporter]\npython = "self"\n')
    monkeypatch.chdir(project)
    cli.main(["--python", "auto", "."])
    captured = capsys.readouterr()
    assert "cleanporter: note:" in captured.err
    assert "stub interpreter ran" in captured.out


@posix_only
def test_a_configured_interpreter_beats_detection(tmp_path, monkeypatch, capsys):
    project = _stub_project(tmp_path, '[tool.cleanporter]\npython = "other/bin/python"\n')
    other = _stub_venv(project / "other")
    monkeypatch.chdir(project)
    cli.main(["."])
    captured = capsys.readouterr()
    assert "note:" not in captured.err  # named, not detected
    assert f"interpreter probe '{other}'" in captured.out


@posix_only
def test_the_note_stays_off_stdout_under_diff(tmp_path, monkeypatch, capsys):
    """stdout under --diff is the patch and nothing else."""
    project = _stub_project(tmp_path)
    monkeypatch.chdir(project)
    cli.main(["--diff", "."])
    captured = capsys.readouterr()
    assert "note:" not in captured.out
    assert "cleanporter: note:" in captured.err


# -- a real project environment, end to end --------------------------------------


def _real_venv_with_onlyhere(venv_dir: pathlib.Path) -> pathlib.Path:
    """A real venv, built from this Python's base, holding a package cleanporter lacks."""
    try:
        venv.create(venv_dir, with_pip=False, symlinks=os.name != "nt")
    except (OSError, subprocess.CalledProcessError) as exc:  # pragma: no cover - platform
        pytest.skip(f"cannot create a virtual environment: {exc}")
    python = venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    purelib = subprocess.run(
        [str(python), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    package = pathlib.Path(purelib) / "onlyhere"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("def helper():\n    pass\n", encoding="utf-8")
    return python


class _Notes(engine.Listener):
    def __init__(self) -> None:
        self.heard: list[str] = []

    def note(self, message: str) -> None:
        self.heard.append(message)


def test_a_package_only_the_project_has_is_classified_through_detection(tmp_path):
    """The pipx / ``uv tool`` case: cleanporter's environment lacks ``onlyhere``."""
    python = _real_venv_with_onlyhere(tmp_path / ".venv")
    (tmp_path / "pyproject.toml").write_text(_PROJECT, encoding="utf-8")
    (tmp_path / "app.py").write_text("from onlyhere import helper\nhelper()\n", encoding="utf-8")
    cfg = config.load_config(tmp_path)
    listener = _Notes()

    detected = engine.run([tmp_path / "app.py"], cfg, listener=listener)
    assert [f.status for f in detected.findings] == [model.Status.VIOLATION]
    assert len(detected.notes) == 1
    assert str(python) in detected.notes[0]
    assert listener.heard == list(detected.notes)

    own = engine.run([tmp_path / "app.py"], config.Config(root=tmp_path, python="self"))
    assert [f.status for f in own.findings] == [model.Status.UNRESOLVED]
    assert own.notes == ()


# -- the corpus harness ----------------------------------------------------------


def test_the_corpus_harness_keeps_the_probe_in_process():
    """Its corpus is importable only through the cwd, so detection must not move it."""
    harness = pathlib.Path(__file__).resolve().parents[1] / "corpus" / "run.py"
    text = harness.read_text(encoding="utf-8")
    assert '"-m", "cleanporter", "--fix", "--python", "self", "."' in text

# Copyright (c) 2026 grAItools
# SPDX-License-Identifier: BSD-3-Clause
# See LICENSE for the full license text.

"""Which interpreter the probe asks: named, cleanporter's own, or the project's, detected."""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import types
import venv

import pytest

from cleanporter import _interpreter, cli, config, engine, model

posix_only = pytest.mark.skipif(os.name != "posix", reason="uses a /bin/sh stub interpreter")


def _fake_venv(venv_dir: pathlib.Path) -> pathlib.Path:
    """A venv layout, in this platform's shape, that detection accepts.

    On POSIX its interpreter is a symlink to this one, so it really runs. On
    Windows, where any file is "executable" and a symlink needs a privilege,
    it is an empty file: nothing that uses this helper runs it.
    """
    python = _interpreter._venv_python(venv_dir)
    python.parent.mkdir(parents=True)
    if os.name == "posix":
        python.symlink_to(sys.executable)
    else:
        python.write_bytes(b"")
    return python


def test_the_venv_interpreter_is_where_each_platform_puts_it(monkeypatch):
    """``bin/python``, or ``Scripts\\python.exe`` on Windows, checked on either."""
    venv = pathlib.Path("env")
    for name, expected in (
        ("posix", venv / "bin" / "python"),
        ("nt", venv / "Scripts" / "python.exe"),
    ):
        # Only the module's view of `os` changes, never the real `os.name`.
        monkeypatch.setattr(_interpreter, "os", types.SimpleNamespace(name=name))
        assert _interpreter._venv_python(venv) == expected


def _stub_venv(venv_dir: pathlib.Path) -> pathlib.Path:
    """A venv layout whose interpreter fails, so the probe warning names what ran."""
    python = venv_dir / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text(
        "#!/bin/sh\necho 'stub interpreter ran' >&2\nexit 1\n", encoding="utf-8", newline="\n"
    )
    python.chmod(0o755)
    return python


# -- which interpreter is "this one" -------------------------------------------


def test_this_interpreter_is_its_own_executable_path():
    assert _interpreter.is_this_interpreter(sys.executable) is True


def test_a_symlink_to_this_interpreter_is_not_this_interpreter(tmp_path):
    """A venv's ``bin/python`` is exactly such a symlink, to another environment."""
    link = tmp_path / "python"
    try:
        link.symlink_to(sys.executable)
    except OSError:  # pragma: no cover - e.g. Windows without the privilege
        pytest.skip("cannot create a symlink here")
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


def test_a_named_interpreter_is_used_as_written_even_if_missing(tmp_path):
    """A request, not a search: its failure is the probe's to report."""
    _fake_venv(tmp_path / ".venv")
    missing = str(tmp_path / "nowhere" / "python")
    assert _interpreter.choose(missing, tmp_path) == _interpreter.Choice(missing)
    assert _interpreter.choose("python3", tmp_path) == _interpreter.Choice("python3")


def test_self_is_cleanporters_own_interpreter_even_with_a_venv_present(tmp_path, monkeypatch):
    _fake_venv(tmp_path / ".venv")
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / ".venv"))
    assert _interpreter.choose("self", tmp_path) == _interpreter.Choice(None)


@pytest.mark.parametrize("setting", [None, "auto"])
def test_detection_finding_nothing_keeps_cleanporters_own(tmp_path, setting):
    assert _interpreter.choose(setting, tmp_path) == _interpreter.Choice(None)


# -- choose: detection, in order ------------------------------------------------


@pytest.mark.parametrize("setting", [None, "auto"])
def test_the_projects_dot_venv_is_detected(tmp_path, setting):
    python = _fake_venv(tmp_path / ".venv")
    choice = _interpreter.choose(setting, tmp_path)
    assert choice.python == str(python)
    assert choice.note is not None
    assert str(python) in choice.note
    assert f"the .venv in the project root {tmp_path}" in choice.note
    assert "python = 'self'" in choice.note


def test_the_projects_dot_venv_wins_over_an_active_virtual_env(tmp_path, monkeypatch):
    """uv's order: a stale activated shell does not pick the project's packages."""
    project = _fake_venv(tmp_path / ".venv")
    _fake_venv(tmp_path / "elsewhere")
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "elsewhere"))
    choice = _interpreter.choose(None, tmp_path)
    assert choice.python == str(project)
    assert choice.note is not None
    assert f"not the active $VIRTUAL_ENV {tmp_path / 'elsewhere'}" in choice.note


def test_virtual_env_is_used_when_the_project_has_no_environment(tmp_path, monkeypatch):
    active = _fake_venv(tmp_path / "elsewhere")
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "elsewhere"))
    choice = _interpreter.choose(None, tmp_path / "proj")
    assert choice.python == str(active)
    assert choice.note is not None
    assert "found from $VIRTUAL_ENV;" in choice.note
    assert "not the active" not in choice.note


def test_an_active_virtual_env_that_is_the_projects_is_not_called_passed_over(
    tmp_path, monkeypatch
):
    _fake_venv(tmp_path / ".venv")
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / ".venv"))
    note = _interpreter.choose(None, tmp_path).note
    assert note is not None
    assert "not the active" not in note


def test_uv_project_environment_is_read_against_the_root(tmp_path, monkeypatch):
    _fake_venv(tmp_path / ".venv")
    uv_env = _fake_venv(tmp_path / "envs" / "proj")
    monkeypatch.chdir(tmp_path / "envs")  # not against the cwd
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", "envs/proj")
    choice = _interpreter.choose(None, tmp_path)
    assert choice.python == str(uv_env)
    assert choice.note is not None
    assert "$UV_PROJECT_ENVIRONMENT" in choice.note


def test_uv_project_environment_wins_over_an_active_virtual_env(tmp_path, monkeypatch):
    _fake_venv(tmp_path / "active")
    uv_env = _fake_venv(tmp_path / "uv")
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "active"))
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", str(tmp_path / "uv"))
    assert _interpreter.choose(None, tmp_path).python == str(uv_env)


@pytest.mark.skipif(
    os.name != "posix", reason="POSIX execute bits: on Windows every file is executable"
)
def test_candidates_that_cannot_run_are_passed_over_silently(tmp_path, monkeypatch):
    """A stale variable, a broken link and a non-executable file are all misses."""
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "gone"))
    broken = tmp_path / "broken" / "bin" / "python"
    broken.parent.mkdir(parents=True)
    broken.symlink_to(tmp_path / "no-such-python")
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", str(tmp_path / "broken"))
    plain = tmp_path / ".venv" / "bin" / "python"
    plain.parent.mkdir(parents=True)
    plain.write_text("", encoding="utf-8", newline="\n")
    plain.chmod(0o644)
    assert _interpreter.choose(None, tmp_path) == _interpreter.Choice(None)
    plain.chmod(0o755)
    assert _interpreter.choose(None, tmp_path).python == str(plain)


def test_empty_variables_are_no_candidates(tmp_path, monkeypatch):
    monkeypatch.setenv("VIRTUAL_ENV", "")
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", "")
    assert _interpreter.choose(None, tmp_path) == _interpreter.Choice(None)


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


def test_a_detected_venv_that_is_sys_prefix_stays_in_process(tmp_path, monkeypatch):
    """``bin/python3`` running, ``bin/python`` detected: one environment, no note."""
    _fake_venv(tmp_path / ".venv")
    _running_in(monkeypatch, tmp_path / ".venv")
    assert _interpreter.choose(None, tmp_path) == _interpreter.Choice(None)


def test_the_prefix_rule_is_not_applied_to_a_named_interpreter(tmp_path, monkeypatch):
    """A name keeps the strict path-identity rule: a miss costs a subprocess, nothing else."""
    python = _fake_venv(tmp_path / ".venv")
    _running_in(monkeypatch, tmp_path / ".venv")
    assert _interpreter.choose(str(python), tmp_path).python == str(python)


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
        f'[project]\nname = "ws"\n[tool.uv.workspace]\n{table}\n', encoding="utf-8", newline="\n"
    )
    member = tmp_path / "packages" / "app"
    member.mkdir(parents=True)
    (member / "pyproject.toml").write_text(
        '[project]\nname = "app"\n', encoding="utf-8", newline="\n"
    )
    return member


@pytest.mark.parametrize("members", ['["packages/*"]', '["packages/app"]', '["**"]'])
def test_a_workspace_members_environment_is_the_workspace_roots(tmp_path, members):
    member = _workspace(tmp_path, f"members = {members}")
    python = _fake_venv(tmp_path / ".venv")
    choice = _interpreter.choose(None, member)
    assert choice.python == str(python)
    assert choice.note is not None
    assert f"the .venv in the uv workspace root {tmp_path}" in choice.note


def test_uv_project_environment_is_read_against_the_workspace_root(tmp_path, monkeypatch):
    member = _workspace(tmp_path, 'members = ["packages/*"]')
    python = _fake_venv(tmp_path / "envs" / "ws")
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", "envs/ws")
    assert _interpreter.choose(None, member).python == str(python)


def test_a_members_own_dot_venv_is_not_tried(tmp_path):
    """uv keeps a member's environment at the workspace root, and so looks only there."""
    member = _workspace(tmp_path, 'members = ["packages/*"]')
    python = _fake_venv(tmp_path / ".venv")
    _fake_venv(member / ".venv")
    assert _interpreter.choose(None, member).python == str(python)


def test_a_member_without_a_workspace_environment_falls_back_to_virtual_env(tmp_path, monkeypatch):
    member = _workspace(tmp_path, 'members = ["packages/*"]')
    _fake_venv(member / ".venv")
    active = _fake_venv(tmp_path / "active")
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "active"))
    assert _interpreter.choose(None, member).python == str(active)


def test_a_project_inside_a_member_is_its_own_environment_root(tmp_path):
    """uv's counterexample: the first ancestor pyproject.toml (a plain project) decides.

    ``/ws`` (members ``packages/**``), ``/ws/packages/a`` (a plain project) and
    ``/ws/packages/a/examples/demo`` (its own project): uv uses demo's ``.venv``.
    """
    _workspace(tmp_path, 'members = ["packages/**"]')
    _fake_venv(tmp_path / ".venv")
    demo = tmp_path / "packages" / "app" / "examples" / "demo"
    demo.mkdir(parents=True)
    (demo / "pyproject.toml").write_text(
        '[project]\nname = "demo"\n', encoding="utf-8", newline="\n"
    )
    python = _fake_venv(demo / ".venv")
    choice = _interpreter.choose(None, demo)
    assert choice.python == str(python)
    assert choice.note is not None
    assert "the .venv in the project root" in choice.note


def test_a_project_that_declares_a_workspace_is_its_own_root(tmp_path):
    _workspace(tmp_path, 'members = ["packages/*"]')
    python = _fake_venv(tmp_path / ".venv")
    choice = _interpreter.choose(None, tmp_path)
    assert choice.python == str(python)
    assert choice.note is not None
    assert "the .venv in the project root" in choice.note


def test_a_nested_workspace_root_is_its_own_root(tmp_path):
    """uv: "Found workspace root: .../pkgs/inner", even though ``pkgs/*`` matches it."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "outer"\n[tool.uv.workspace]\nmembers = ["pkgs/*"]\n',
        encoding="utf-8",
        newline="\n",
    )
    inner = tmp_path / "pkgs" / "inner"
    inner.mkdir(parents=True)
    (inner / "pyproject.toml").write_text(
        '[project]\nname = "inner"\n[tool.uv.workspace]\nmembers = []\n',
        encoding="utf-8",
        newline="\n",
    )
    _fake_venv(tmp_path / ".venv")
    python = _fake_venv(inner / ".venv")
    choice = _interpreter.choose(None, inner)
    assert choice.python == str(python)
    assert choice.note is not None
    assert "the .venv in the project root" in choice.note


@pytest.mark.parametrize("members", ['["./packages/*"]', '["packages/*/"]', '["packages//app"]'])
def test_workspace_globs_are_matched_literally(tmp_path, members):
    """uv reports these "not included"; a lenient normalisation would pick the workspace."""
    member = _workspace(tmp_path, f"members = {members}")
    _fake_venv(tmp_path / ".venv")
    assert _interpreter.choose(None, member) == _interpreter.Choice(None)


def test_a_relative_root_is_made_absolute(tmp_path, monkeypatch):
    member = _workspace(tmp_path, 'members = ["packages/*"]')
    python = _fake_venv(tmp_path / ".venv")
    monkeypatch.chdir(member)
    assert _interpreter.choose(None, pathlib.Path()).python == str(python)


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


def test_the_workspace_walk_stops_at_the_nearest_declaring_ancestor(tmp_path):
    """A non-matching inner workspace hides an outer one that would match."""
    outer_member = _workspace(tmp_path, 'members = ["**"]')
    (outer_member / "pyproject.toml").write_text(
        '[project]\nname = "app"\n[tool.uv.workspace]\nmembers = ["other/*"]\n',
        encoding="utf-8",
        newline="\n",
    )
    project = outer_member / "sub"
    project.mkdir()
    _fake_venv(tmp_path / ".venv")
    assert _interpreter.choose(None, project) == _interpreter.Choice(None)


def test_an_unreadable_first_ancestor_pyproject_means_no_workspace(tmp_path):
    """It still ends the walk: a workspace further up is not consulted."""
    _workspace(tmp_path, 'members = ["**"]')
    _fake_venv(tmp_path / ".venv")
    (tmp_path / "packages" / "pyproject.toml").write_text(
        "this is [not", encoding="utf-8", newline="\n"
    )
    assert _interpreter.choose(None, tmp_path / "packages" / "app") == _interpreter.Choice(None)


def test_an_in_process_pick_still_names_a_different_active_virtual_env(tmp_path, monkeypatch):
    """The shell's environment is never ignored silently."""
    _fake_venv(tmp_path / ".venv")
    _fake_venv(tmp_path / "other")
    _running_in(monkeypatch, tmp_path / ".venv")
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "other"))
    choice = _interpreter.choose(None, tmp_path)
    assert choice.python is None
    assert choice.note is not None
    assert f"not the active $VIRTUAL_ENV {tmp_path / 'other'}" in choice.note
    assert "in process" in choice.note


def test_uv_run_is_silent(tmp_path, monkeypatch):
    """``uv run cleanporter``: $VIRTUAL_ENV is the project's own venv, spelled any way."""
    _fake_venv(tmp_path / ".venv")
    try:
        (tmp_path / "link").symlink_to(tmp_path / ".venv", target_is_directory=True)
    except OSError:  # pragma: no cover - e.g. Windows without the privilege
        pytest.skip("cannot create a symlink here")
    _running_in(monkeypatch, tmp_path / ".venv")
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "link"))
    assert _interpreter.choose(None, tmp_path) == _interpreter.Choice(None)


# -- config and command line ------------------------------------------------------


@pytest.mark.parametrize("value", ["auto", "self"])
def test_the_sentinels_are_not_anchored_as_paths(tmp_path, value):
    (tmp_path / "pyproject.toml").write_text(
        f'[tool.cleanporter]\npython = "{value}"\n', encoding="utf-8", newline="\n"
    )
    assert config.load_config(tmp_path).python == value


@posix_only
def test_the_default_config_detects(tmp_path):
    """No key, no flag: a run under a plain `Config` probes the project's `.venv`."""
    assert config.Config().python is None
    python = _stub_venv(tmp_path / ".venv")
    (tmp_path / "app.py").write_text(
        "from functools import partial\n", encoding="utf-8", newline="\n"
    )
    result = engine.run([tmp_path / "app.py"], config.Config(root=tmp_path))
    assert len(result.notes) == 1
    assert str(python) in result.notes[0]
    assert any(f"interpreter probe '{python}'" in w for w in result.warnings)


_PROJECT = '[project]\nname = "demo"\nversion = "0"\n'


def _stub_project(tmp_path: pathlib.Path, table: str = "") -> pathlib.Path:
    """A project with a stdlib import to probe, and a failing stub as its ``.venv``."""
    (tmp_path / "pyproject.toml").write_text(_PROJECT + table, encoding="utf-8", newline="\n")
    (tmp_path / "app.py").write_text(
        "from functools import partial\nf = partial(print)\n", encoding="utf-8", newline="\n"
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
    (package / "__init__.py").write_text(
        "def helper():\n    pass\n", encoding="utf-8", newline="\n"
    )
    return python


class _Notes(engine.Listener):
    def __init__(self) -> None:
        self.heard: list[str] = []

    def note(self, message: str) -> None:
        self.heard.append(message)


def test_a_package_only_the_project_has_is_classified_through_detection(tmp_path):
    """The pipx / ``uv tool`` case: cleanporter's environment lacks ``onlyhere``."""
    python = _real_venv_with_onlyhere(tmp_path / ".venv")
    (tmp_path / "pyproject.toml").write_text(_PROJECT, encoding="utf-8", newline="\n")
    (tmp_path / "app.py").write_text(
        "from onlyhere import helper\nhelper()\n", encoding="utf-8", newline="\n"
    )
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
    assert '"-m", "cleanporter", *extra, "--python", "self", "."' in text
    assert '_run_cleanporter(rewritten, "--fix"' in text

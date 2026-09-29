"""How the weekly gt4py check decides that the rewrite broke something.

`corpus/gt4py_check.py` runs against a moving upstream, so it reports once a
week to someone who was not watching. What it says has to be trustworthy in
both directions: a false alarm teaches everyone to skim past it, and a missed
regression is the class of bug the check exists for.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import subprocess

import pytest

from cleanporter import config

_SCRIPT = pathlib.Path(__file__).parent.parent / "corpus" / "gt4py_check.py"


def _load_harness():
    """Import ``corpus/gt4py_check.py``, which is a script rather than a package."""
    spec = importlib.util.spec_from_file_location("corpus_gt4py_check", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


harness = _load_harness()


def _junit(tmp_path: pathlib.Path, body: str) -> pathlib.Path:
    report = tmp_path / "report.xml"
    report.write_text(
        f'<?xml version="1.0"?><testsuites><testsuite>{body}</testsuite></testsuites>', newline="\n"
    )
    return report


_PASSED = '<testcase classname="t.test_a" name="test_ok"/>'
_FAILED = '<testcase classname="t.test_a" name="test_bad"><failure>boom</failure></testcase>'
_ERRORED = '<testcase classname="t.test_b" name="test_c"><error>collect</error></testcase>'
_SKIPPED = '<testcase classname="t.test_a" name="test_s"><skipped/></testcase>'

_JUNIT_ONE_PASSING = (
    '<testsuites><testsuite><testcase classname="t" name="a"/></testsuite></testsuites>'
)


def _stub_interpreter(tmp_path: pathlib.Path, imported: str) -> pathlib.Path:
    """An interpreter that answers the import probe and fakes a pytest run.

    `main` shells out for both, so a checkout that is only a few files still
    exercises the whole path -- including the guards that exist to stop this
    check from passing while proving nothing.
    """
    script = tmp_path / "fake-python"
    script.write_text(
        "#!/bin/sh\n"
        'out=""\n'
        'for arg in "$@"; do case "$arg" in --junit-xml=*) out="${arg#--junit-xml=}";; esac; done\n'
        f"if [ -n \"$out\" ]; then printf '%s' '{_JUNIT_ONE_PASSING}' > \"$out\"; exit 0; fi\n"
        f"case \"$*\" in *import*) printf '%s\\n' '{imported}'; exit 0;; esac\n"
        "printf '{}'\n",
        encoding="utf-8",
        newline="\n",
    )
    script.chmod(0o755)
    return script


def _checkout(tmp_path: pathlib.Path, user: str) -> pathlib.Path:
    """A miniature first-party tree, so no probe answer is needed to fix it."""
    checkout = tmp_path / "checkout"
    (checkout / "src" / "gt4py").mkdir(parents=True)
    (checkout / "pyproject.toml").write_text(
        "[project]\nname = 'gt4py'\n", encoding="utf-8", newline="\n"
    )
    (checkout / "src" / "gt4py" / "__init__.py").write_text("", encoding="utf-8", newline="\n")
    (checkout / "src" / "gt4py" / "sub.py").write_text(
        "THING = 1\n", encoding="utf-8", newline="\n"
    )
    (checkout / "src" / "gt4py" / "user.py").write_text(user, encoding="utf-8", newline="\n")
    return checkout


# -- reading the run --------------------------------------------------------


def test_failures_and_errors_are_both_regressions_and_skips_are_not(tmp_path):
    """A collection error arrives as an `error` case, and it is news."""
    outcomes, total = harness._parse_junit(
        _junit(tmp_path, _PASSED + _FAILED + _ERRORED + _SKIPPED)
    )
    assert total == 4
    assert outcomes == {"t.test_a::test_bad": "failure", "t.test_b::test_c": "error"}


def test_a_suite_with_nothing_wrong_reports_no_outcomes(tmp_path):
    outcomes, total = harness._parse_junit(_junit(tmp_path, _PASSED + _SKIPPED))
    assert (outcomes, total) == ({}, 2)


# -- reporting the difference -----------------------------------------------


def test_a_failure_that_was_already_there_is_not_a_regression(capsys):
    """Upstream `main` is allowed to be red; only a *change* is this check's news."""
    before = ({"t::a": "failure"}, 10)
    assert harness._report(before, before, "") is True
    assert "now fail" not in capsys.readouterr().out


def test_a_test_that_passed_and_now_fails_is_named(capsys):
    assert harness._report(({}, 10), ({"t::a": "failure"}, 10), "tail line") is False
    out = capsys.readouterr().out
    assert "1 test(s) that passed before now fail" in out
    assert "[failure] t::a" in out
    assert "tail line" in out, "the post-fix output is what a reader diagnoses from"


def test_a_failure_that_disappeared_is_not_a_regression(capsys):
    assert harness._report(({"t::a": "failure"}, 10), ({}, 10), "") is True
    assert "now fail" not in capsys.readouterr().out


def test_tests_that_stopped_being_collected_are_a_regression(capsys):
    """A test that vanished cannot show up as a new failure.

    Deleting an import the fixer got wrong can take a whole module out of
    collection, and comparing only failure sets would call that an
    improvement.
    """
    assert harness._report(({}, 10), ({}, 7), "") is False
    assert "3 test(s) are no longer collected" in capsys.readouterr().out


# -- the configuration the check writes -------------------------------------


def test_the_written_config_is_one_this_repository_still_accepts(tmp_path, capsys):
    """Anti-drift: the rules are parsed by the same code cleanporter would use.

    The check hands gt4py a `[tool.cleanporter.skip]` table. If the schema
    here changes under it, the weekly run fails against a moving upstream and
    looks like gt4py's fault.
    """
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'gt4py'\n", encoding="utf-8", newline="\n"
    )
    harness._configure(tmp_path)
    assert "wrote the expected" in capsys.readouterr().out

    loaded = config.load_config(tmp_path)
    assert [rule.decorator for rule in loaded.skip] == [
        "field_operator|scan_operator|program",
        None,
    ]
    assert [rule.file for rule in loaded.skip] == [None, r".*conftest\.py"]
    assert all(rule.reason for rule in loaded.skip), "a rule's reason is echoed in the report"


@pytest.mark.skipif(os.name != "posix", reason="uses a /bin/sh stub interpreter")
def test_the_written_rules_still_match_the_code_they_are_meant_to_cover(tmp_path, capsys):
    """Parsing is not matching, and matching is what the weekly run depends on.

    `skip.py` offers a decorator's dotted name *and* its last component as
    candidates, which is the only reason the bare `field_operator` pattern
    covers `@gtx.field_operator`. If that stopped being true the table would
    still load, the rules would silently cover nothing, and the DSL bodies
    they exist to protect would be rewritten -- so this asserts the effect on
    a real `--fix`, not the shape of the config.
    """
    checkout = _checkout(tmp_path, "from gt4py.sub import THING\n\nx = THING\n")
    (checkout / "src" / "gt4py" / "stencil.py").write_text(
        "import gt4py as gtx\n"
        "from gt4py.sub import THING\n"
        "\n"
        "\n"
        "@gtx.field_operator\n"
        "def op():\n"
        "    return THING\n",
        encoding="utf-8",
        newline="\n",
    )
    python = _stub_interpreter(tmp_path, str(checkout / "src" / "gt4py" / "__init__.py"))
    assert harness.main([str(checkout), "--python", str(python), "--tests", "src"]) == 0

    stencil = (checkout / "src" / "gt4py" / "stencil.py").read_text(encoding="utf-8")
    assert "from gt4py.sub import THING" in stencil, (
        f"the decorated body must be left alone:\n{stencil}"
    )
    assert "return THING" in stencil
    # ... while the file without the decorator is still fixed, so the rule is
    # doing the work rather than the fixer having declined everything.
    assert "from gt4py import sub" in (checkout / "src" / "gt4py" / "user.py").read_text()


def test_a_checkout_that_configures_cleanporter_itself_is_left_alone(tmp_path, capsys):
    """If gt4py ever adopts the tool, its rules are the ones worth testing."""
    original = "[project]\nname = 'gt4py'\n\n[tool.cleanporter]\nexempt_names = ['x']\n"
    (tmp_path / "pyproject.toml").write_text(original, encoding="utf-8", newline="\n")
    harness._configure(tmp_path)
    assert (tmp_path / "pyproject.toml").read_text(encoding="utf-8") == original
    assert "already configures cleanporter" in capsys.readouterr().out


# -- refusing to prove nothing ----------------------------------------------


@pytest.mark.skipif(os.name != "posix", reason="uses a /bin/sh stub interpreter")
def test_a_run_that_rewrote_nothing_is_an_error_not_a_pass(tmp_path, capsys):
    """Both suites then ran the same tree, so "nothing broke" is about nothing.

    It is also what an unhandled crash on the way into `--fix` looks like --
    cleanporter exits 1 for ordinary findings, so a traceback under that exit
    code would otherwise read as "no violations left".
    """
    checkout = _checkout(tmp_path, "from gt4py import sub\n\nx = sub.THING\n")
    python = _stub_interpreter(tmp_path, str(checkout / "src" / "gt4py" / "__init__.py"))

    assert harness.main([str(checkout), "--python", str(python), "--tests", "src"]) == 2
    assert "rewrote nothing" in capsys.readouterr().err


@pytest.mark.skipif(os.name != "posix", reason="uses a /bin/sh stub interpreter")
def test_a_run_that_rewrote_something_compares_the_two_suites(tmp_path, capsys):
    checkout = _checkout(tmp_path, "from gt4py.sub import THING\n\nx = THING\n")
    python = _stub_interpreter(tmp_path, str(checkout / "src" / "gt4py" / "__init__.py"))

    assert harness.main([str(checkout), "--python", str(python), "--tests", "src"]) == 0
    out = capsys.readouterr().out
    assert "rewrote 1 file(s)" in out
    assert "OK, the rewrite broke nothing" in out
    assert "from gt4py import sub" in (checkout / "src" / "gt4py" / "user.py").read_text()


@pytest.mark.skipif(os.name != "posix", reason="uses a /bin/sh stub interpreter")
def test_an_environment_that_imports_an_installed_copy_is_refused(tmp_path, capsys):
    """Under the checkout is not enough: a non-editable install is a copy.

    It sits in `<checkout>/.venv/.../site-packages`, which passes a plain
    "is it under the checkout" test while being precisely the code the
    rewrite does not touch.
    """
    checkout = _checkout(tmp_path, "from gt4py.sub import THING\n\nx = THING\n")
    installed = checkout / ".venv" / "lib" / "python3.12" / "site-packages" / "gt4py"
    python = _stub_interpreter(tmp_path, str(installed / "__init__.py"))

    assert harness.main([str(checkout), "--python", str(python), "--tests", "src"]) == 2
    assert "is not source under" in capsys.readouterr().err


def test_a_checkout_with_local_modifications_is_refused(tmp_path, capsys):
    """A second run over a rewritten tree would compare rewritten to rewritten."""
    checkout = _checkout(tmp_path, "x = 1\n")
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    assert harness._verify_pristine(checkout) is False
    assert "local modifications" in capsys.readouterr().err


def test_a_checkout_that_is_not_a_git_repository_is_not_refused(tmp_path, capsys):
    """Missing git is not evidence of anything; only a dirty tree is."""
    assert harness._verify_pristine(_checkout(tmp_path, "x = 1\n")) is True
    assert "cannot tell whether" in capsys.readouterr().out


def test_git_not_being_installed_is_not_refused_either(tmp_path, monkeypatch, capsys):
    """`subprocess.run` raises before there is a return code to fail open on.

    The exception would escape `main` and exit 1, which this script defines
    as "a test that passed now fails" -- red for a reason that has nothing to
    do with gt4py.
    """
    monkeypatch.setenv("PATH", str(tmp_path / "nothing-here"))
    assert harness._verify_pristine(_checkout(tmp_path, "x = 1\n")) is True
    assert "cannot tell whether" in capsys.readouterr().out


def test_a_dotted_skip_table_counts_as_already_configured(tmp_path, capsys):
    """Appending a second `[tool.cleanporter]` would be a duplicate-key TOML error.

    Which cleanporter reports by exiting 1 -- the same code as "findings
    remain" -- so the run would have gone on to rewrite nothing and call it a
    pass.
    """
    original = "[project]\nname = 'gt4py'\n\n[tool.cleanporter.skip]\n"
    (tmp_path / "pyproject.toml").write_text(original, encoding="utf-8", newline="\n")
    harness._configure(tmp_path)
    assert (tmp_path / "pyproject.toml").read_text(encoding="utf-8") == original
    assert "already configures cleanporter" in capsys.readouterr().out


@pytest.mark.skipif(os.name != "posix", reason="uses a /bin/sh stub interpreter")
def test_a_rewrite_that_misses_the_code_under_test_is_an_error_too(tmp_path, capsys):
    """Different trees is not the same as different *executed* code.

    A narrow `--tests` on a dispatch can leave the only rewritten file
    somewhere the suite never imports, and the comparison could then not have
    come out any other way.
    """
    checkout = _checkout(tmp_path, "x = 1\n")  # nothing under src/ to fix
    (checkout / "scripts").mkdir()
    (checkout / "scripts" / "tool.py").write_text(
        "from gt4py.sub import THING\n\nx = THING\n", encoding="utf-8", newline="\n"
    )
    python = _stub_interpreter(tmp_path, str(checkout / "src" / "gt4py" / "__init__.py"))

    assert harness.main([str(checkout), "--python", str(python), "--tests", "src"]) == 2
    assert "none of them under src/" in capsys.readouterr().err
    assert "from gt4py import sub" in (checkout / "scripts" / "tool.py").read_text()

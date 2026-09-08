"""How the corpus harness decides that a failure is *new*.

`corpus/run.py` is the only check that imports and runs the rewritten code, so
what it reports has to be trusted. A false positive is expensive in a
different way from a false negative: it makes the one check with teeth cry
wolf, and the next person learns to skim past it.
"""

from __future__ import annotations

import collections
import importlib.util
import pathlib
import shutil
import subprocess
import sys

import pytest

_RUN = pathlib.Path(__file__).parent.parent / "corpus" / "run.py"


def _load_harness():
    """Import ``corpus/run.py``, which is a script rather than a package."""
    spec = importlib.util.spec_from_file_location("corpus_run", _RUN)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


harness = _load_harness()

_FINDING = "m.py: F821 Undefined name `missing`"


def _tally(undefined: list[str]) -> dict[str, object]:
    """One probe result carrying only the F821 tally the tests care about."""
    return {"imports": {}, "undefined": collections.Counter(undefined), "suites": {}}


# -- keying the findings ----------------------------------------------------


@pytest.mark.skipif(shutil.which("ruff") is None, reason="needs the ruff binary")
def test_a_finding_that_only_moved_is_not_new(tmp_path):
    """Compacting an import block shifts every line under it; that is not a bug.

    ruff's concise output starts `path:line:col:`, so comparing its raw lines
    made any surviving finding below a rewritten import look brand new.
    `prompt_toolkit/application/application.py`'s `Undefined name 'result'`
    moved from 953 to 921 and was reported as a regression, under a tally
    reading `14 before, 14 after`.
    """
    body = "def f():\n    return missing\n"
    (tmp_path / "before").mkdir()
    (tmp_path / "after").mkdir()
    (tmp_path / "before" / "m.py").write_text(f"import os\nimport sys\n\n{body}", encoding="utf-8")
    (tmp_path / "after" / "m.py").write_text(body, encoding="utf-8")

    before = harness._undefined_names(tmp_path / "before")
    after = harness._undefined_names(tmp_path / "after")

    assert sum(before.values()) == 1, before
    assert set(before) == {_FINDING}
    assert after - before == collections.Counter(), "a finding that only moved is not new"


@pytest.mark.skipif(shutil.which("ruff") is None, reason="needs the ruff binary")
def test_an_extra_occurrence_of_a_known_name_is_still_new(tmp_path):
    """Dropping the position must not merge two findings into one.

    This is the false negative the obvious fix -- a set keyed on the file and
    the message -- would have introduced.
    """
    (tmp_path / "before").mkdir()
    (tmp_path / "after").mkdir()
    (tmp_path / "before" / "m.py").write_text("def f():\n    return missing\n", encoding="utf-8")
    (tmp_path / "after" / "m.py").write_text(
        "def f():\n    return missing\n\n\ndef g():\n    return missing\n", encoding="utf-8"
    )

    new = harness._undefined_names(tmp_path / "after") - harness._undefined_names(
        tmp_path / "before"
    )
    assert new == collections.Counter({_FINDING: 1})


# -- reporting the difference -----------------------------------------------


def test_report_passes_when_the_same_findings_come_back(capsys):
    assert harness._report(_tally([_FINDING]), _tally([_FINDING])) is True
    out = capsys.readouterr().out
    assert "NEW undefined name" not in out
    assert "undefined names (F821): 1 before, 1 after" in out


def test_report_fails_on_an_extra_occurrence_in_an_already_flagged_file(capsys):
    assert harness._report(_tally([_FINDING]), _tally([_FINDING, _FINDING])) is False
    out = capsys.readouterr().out
    assert "1 NEW undefined name(s)" in out
    assert _FINDING in out
    assert "undefined names (F821): 1 before, 2 after" in out


def test_report_fails_on_a_finding_in_a_file_that_had_none(capsys):
    other = "other.py: F821 Undefined name `absent`"
    assert harness._report(_tally([_FINDING]), _tally([_FINDING, other])) is False
    out = capsys.readouterr().out
    assert other in out


def test_report_does_not_flag_a_finding_that_disappeared(capsys):
    """The corpus is not required to be clean, only unchanged."""
    assert harness._report(_tally([_FINDING]), _tally([])) is True
    assert "NEW undefined name" not in capsys.readouterr().out


# -- repinning the manifest -------------------------------------------------


_MANIFEST = """\
# The corpus: real third-party code.
#
# Pinned on purpose.

libcst==1.9.0
Django==6.0.2
pluggy==1.6.0
"""


def test_update_preserves_comments_order_and_untouched_pins():
    """A bump should read as a column of versions changing and nothing else.

    The manifest's grouping and the reasoning written above it are the point
    of the file, and the resolver's own output is alphabetical -- writing that
    back would turn every bump into a whole-file diff.
    """
    updated, changes = harness._rewrite_manifest(
        _MANIFEST, {"libcst": "1.10.0", "django": "6.1.1", "pluggy": "1.6.0"}
    )
    assert updated == _MANIFEST.replace("1.9.0", "1.10.0").replace("6.0.2", "6.1.1")
    assert changes == [("libcst", "1.9.0", "1.10.0"), ("Django", "6.0.2", "6.1.1")]


def test_update_matches_names_the_way_pypi_does():
    """`Django` in the manifest and `django` from the resolver are one package."""
    _updated, changes = harness._rewrite_manifest(_MANIFEST, {"django": "6.1.1"})
    assert changes == [("Django", "6.0.2", "6.1.1")]


def test_update_leaves_a_package_the_resolver_could_not_answer_for():
    """No answer is not an answer: the pin stays, and the caller says so."""
    updated, changes = harness._rewrite_manifest(_MANIFEST, {})
    assert updated == _MANIFEST
    assert changes == []


def test_update_keeps_a_trailing_newline_off_a_file_that_had_none():
    text = _MANIFEST.rstrip("\n")
    updated, _changes = harness._rewrite_manifest(text, {"libcst": "1.10.0"})
    assert not updated.endswith("\n")
    assert updated.splitlines()[-1] == "pluggy==1.6.0"


def test_the_python_floor_comes_from_pyproject_not_from_this_interpreter():
    """Resolving on 3.14 could pin a wheel the 3.12 floor cannot install."""
    assert harness._python_floor() == "3.12"


def test_a_marker_or_a_trailing_comment_survives_the_bump():
    """Both are part of the line's meaning; only the version may move.

    A dropped marker changes what installs, and a dropped comment deletes the
    reason the package is on the list.
    """
    text = 'django==6.0.2 ; python_version < "3.13"\nlibcst==1.9.0  # ships its own tests\n'
    updated, changes = harness._rewrite_manifest(text, {"django": "6.1.1", "libcst": "1.10.0"})
    assert updated == (
        'django==6.1.1 ; python_version < "3.13"\nlibcst==1.10.0  # ships its own tests\n'
    )
    assert [name for name, _, _ in changes] == ["django", "libcst"]


def test_extras_are_matched_by_the_package_they_belong_to():
    """`celery[redis]` is celery; the extras ride along untouched."""
    updated, changes = harness._rewrite_manifest("celery[redis]==5.6.1\n", {"celery": "5.6.3"})
    assert updated == "celery[redis]==5.6.3\n"
    assert changes == [("celery[redis]", "5.6.1", "5.6.3")]


def test_an_unpinned_line_gets_the_pin_the_manifest_says_it_should_have():
    updated, changes = harness._rewrite_manifest("flask\n", {"flask": "3.1.0"})
    assert updated == "flask==3.1.0\n"
    assert changes == [("flask", "unpinned", "3.1.0")]


@pytest.mark.parametrize(
    "line",
    [
        "libcst>=1.9",
        # Whitespace around an operator is legal in a requirements file, and
        # it is what made the check above insufficient: only the spelling
        # without a space was caught, so `libcst == 1.9.0` became
        # `libcst==1.10.0 == 1.9.0` and was reported as a *new pin*.
        "libcst == 1.9.0",
        "libcst ==1.9.0",
        "pytest >= 9.1.1",
        "libcst\t>= 1.9",
        # Arbitrary equality is not `==`; rewriting it would quietly change
        # what the pin means.
        "libcst===1.9.0",
    ],
)
def test_a_line_that_is_not_a_simple_pin_is_left_alone_and_says_so(line, capsys):
    """Writing `libcst==1.10.0 >= 1.9` would be worse than doing nothing."""
    updated, changes = harness._rewrite_manifest(line + "\n", {"libcst": "1.10.0"})
    assert (updated, changes) == (line + "\n", [])
    assert "not a simple '==' pin" in capsys.readouterr().out


def test_indentation_is_left_where_it_was():
    updated, _changes = harness._rewrite_manifest("  libcst==1.9.0\n", {"libcst": "1.10.0"})
    assert updated == "  libcst==1.10.0\n"


# -- the contract both harnesses read ---------------------------------------


def test_fix_still_announces_each_rewritten_file_on_stderr(tmp_path):
    """`fixed: <path>` is an interface, not a log line.

    Both this harness and `corpus/gt4py_check.py` count those lines to know
    how much was rewritten, and `gt4py_check` refuses to compare two suites
    when the count is zero. Reword the line in `cli.py` and both would go
    quietly green having tested nothing.
    """
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "sub.py").write_text("THING = 1\n", encoding="utf-8")
    (pkg / "user.py").write_text("from pkg.sub import THING\n\nx = THING\n", encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, "-m", "cleanporter", "--fix", "."],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert proc.returncode in (0, 1), proc.stderr
    assert [ln for ln in proc.stderr.splitlines() if ln.startswith("fixed: ")], proc.stderr

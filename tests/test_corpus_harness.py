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

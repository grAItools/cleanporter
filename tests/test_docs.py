"""The documentation must not drift from the actual CLI surface.

Every flag, config key and finding code is asserted to appear in the page
that is supposed to document it, so adding one without documenting it fails
the suite. That is deliberate: this tool's whole value is that it does not
surprise you, and an undocumented flag is a surprise.

The claim tests near the bottom pin statements that a review found were
previously overstated in the docs. Each one is a promise the code actually
keeps -- do not relax them without changing the code first.
"""

from __future__ import annotations

import pathlib
import re

import pytest

import cleanporter
from cleanporter import cli, config, model

ROOT = pathlib.Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
README = ROOT / "README.md"
USAGE = DOCS / "usage.md"
CONFIGURATION = DOCS / "configuration.md"
SAFETY = DOCS / "safety.md"

#: Every Markdown file a reader is expected to see.
ALL_PAGES = [README, *sorted(DOCS.glob("*.md"))]


def _text(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def test_the_documented_pages_all_exist() -> None:
    missing = [p.name for p in (USAGE, CONFIGURATION, SAFETY) if not p.is_file()]
    assert missing == [], f"missing documentation pages: {missing}"


def test_every_cli_flag_is_documented() -> None:
    text = _text(USAGE)
    flags = {
        option
        for action in cli.build_arg_parser()._actions
        for option in action.option_strings
        if option.startswith("--") and option != "--help"
    }
    missing = sorted(f for f in flags if f not in text)
    assert missing == [], f"undocumented flags in {USAGE.name}: {missing}"


def test_every_config_key_is_documented() -> None:
    text = _text(CONFIGURATION)
    missing = sorted(k for k in config._KNOWN_KEYS if k not in text)
    assert missing == [], f"undocumented config keys in {CONFIGURATION.name}: {missing}"


def test_every_finding_code_is_documented() -> None:
    text = _text(USAGE)
    codes = {
        "CP001": model.Status.VIOLATION,
        "CP002": model.Status.UNRESOLVED,
        "CP003": model.Status.SKIPPED,
        "CP004": model.Status.SKIPPED_BY_CONFIG,
    }
    missing = sorted(c for c in codes if c not in text)
    assert missing == [], f"undocumented finding codes in {USAGE.name}: {missing}"


@pytest.mark.parametrize("page", ALL_PAGES, ids=lambda p: p.name)
def test_no_stale_references_to_the_old_tools(page: pathlib.Path) -> None:
    text = _text(page)
    assert "modimports" not in text
    assert "3rdparty" not in text


# -- metadata that must agree across files ----------------------------------


def test_the_version_is_single_sourced() -> None:
    """`__version__` is derived from package metadata, so it cannot drift.

    It silently did drift once, and `cleanporter --version` reported a
    release that had already been superseded.
    """
    import tomllib

    declared = tomllib.loads(_text(ROOT / "pyproject.toml"))["project"]["version"]
    assert cleanporter.__version__ == declared


def test_the_python_requirement_agrees_with_the_readme() -> None:
    import tomllib

    requires = tomllib.loads(_text(ROOT / "pyproject.toml"))["project"]["requires-python"]
    floor = requires.removeprefix(">=").strip()
    assert f"Python >= {floor}" in _text(README)


# -- claims a review found to be false --------------------------------------


def test_comment_preservation_is_not_overclaimed() -> None:
    """Comments are preserved *or the file is declined* -- not "exactly"."""
    text = _text(SAFETY)
    assert "comments, and blank lines are preserved exactly" not in text
    assert "never silently dropped" in text


def test_f_strings_are_not_listed_as_a_blocking_string_mention() -> None:
    """`find_string_mentions` visits `SimpleString` only; f-strings rewrite."""
    text = _text(SAFETY)
    assert "an f-string, ...)" not in text
    assert "An f-string is **not** a blocking" in text


def test_the_diff_is_documented_as_applyable() -> None:
    text = _text(USAGE)
    assert "| git apply" in text
    assert "is **not** currently" not in text


def test_cp003_is_not_described_as_informational() -> None:
    """`cli.run` folds SKIPPED into the failure count, so CP003 exits 1.

    The old README called CP003 "informational", which would have led a
    reader to expect a clean exit from a run that in fact fails.
    """
    for page in (README, USAGE):
        text = _text(page)
        assert "deliberately not rewritten — informational" not in text, (
            f"{page.name} still carries the old claim that CP003 is purely informational."
        )

    # And the consequence has to be stated positively somewhere near the code,
    # not merely left unclaimed.
    usage = _text(USAGE)
    index = usage.find("CP003")
    assert index != -1
    assert re.search(r"exit\w*\s*`?1`?", usage[index : index + 1200]), (
        "docs/usage.md must say that a CP003 finding makes the run exit 1; "
        "cli.run folds SKIPPED into the failure count."
    )


def test_the_formatter_is_not_documented_as_forbidden() -> None:
    """`ruff format` is a CI gate now; the old docs told readers to avoid it."""
    for page in ALL_PAGES:
        assert "Do not run `ruff format`" not in _text(page)

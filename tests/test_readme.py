"""The README must not drift from the actual CLI surface."""

from __future__ import annotations

from pathlib import Path

from cleanporter.cli import build_arg_parser
from cleanporter.config import _KNOWN_KEYS
from cleanporter.model import Status

README = Path(__file__).resolve().parents[1] / "README.md"


def test_every_cli_flag_is_documented():
    text = README.read_text(encoding="utf-8")
    flags = {
        option
        for action in build_arg_parser()._actions
        for option in action.option_strings
        if option.startswith("--") and option not in {"--help"}
    }
    missing = sorted(f for f in flags if f not in text)
    assert missing == [], f"undocumented flags: {missing}"


def test_every_config_key_is_documented():
    text = README.read_text(encoding="utf-8")
    missing = sorted(k for k in _KNOWN_KEYS if k not in text)
    assert missing == [], f"undocumented config keys: {missing}"


def test_every_finding_code_is_documented():
    text = README.read_text(encoding="utf-8")
    codes = {
        "CP001": Status.VIOLATION,
        "CP002": Status.UNRESOLVED,
        "CP003": Status.SKIPPED,
    }
    missing = sorted(c for c in codes if c not in text)
    assert missing == [], f"undocumented finding codes: {missing}"


def test_no_stale_references_to_the_old_tools():
    text = README.read_text(encoding="utf-8")
    assert "modimports" not in text
    assert "3rdparty" not in text

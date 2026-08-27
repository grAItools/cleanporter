"""The ``mypy --strict`` debt is a bounded budget, not an open account.

Thirteen errors are accepted on this tree (they are enumerated in the
README's Development section: libcst union shapes, a missing ``tomli``
stub, and three unannotated visitor hooks). Nothing prevented a
fourteenth from arriving unnoticed, so this test pins the count. When you
*fix* one, lower `BASELINE` in the same commit.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

#: Accepted `mypy --strict src/cleanporter` errors. Never raise this.
BASELINE = 13


def _error_count(output: str) -> int:
    match = re.search(r"^Found (\d+) errors?", output, re.MULTILINE)
    if match:
        return int(match.group(1))
    assert "Success" in output, f"unrecognized mypy output:\n{output}"
    return 0


@pytest.mark.skipif(
    importlib.util.find_spec("mypy") is None, reason="mypy is not installed"
)
def test_strict_error_count_stays_within_budget():
    proc = subprocess.run(
        [sys.executable, "-m", "mypy", "--strict", "src/cleanporter"],
        cwd=ROOT, capture_output=True, text=True,
    )
    count = _error_count(proc.stdout)
    assert count <= BASELINE, (
        f"mypy --strict reports {count} errors, budget is {BASELINE}. "
        f"New strict-mode debt is not accepted.\n{proc.stdout}"
    )

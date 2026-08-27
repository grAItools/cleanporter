"""Type-checker debt is a bounded budget, not an open account.

Ruff does no type inference, so `mypy --strict` and `pyright` are what
actually back this package's ``py.typed`` marker. Both report a handful of
errors that no amount of local care removes: they all originate in libcst's
partially-typed surface -- union shapes the strict modes cannot narrow, and
``CSTNode`` attributes that only exist on some members of a union.

Those are accepted. What is *not* accepted is a fourteenth one arriving
unnoticed, so this module pins the count per tool.

Every budget below is a ceiling that only ever moves down. Fixed one of the
accepted errors? Lower the number in the same commit. Never raise a budget to
make room for new code.

``zuban`` is deliberately absent: it is mypy-compatible and reports the same
diagnostics, so it earns its keep as an on-demand second opinion
(``uv sync --group zuban``) rather than as a third gate.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TARGET = "src/cleanporter"

#: Accepted `mypy --strict` errors. Never raise this.
MYPY_BASELINE = 9

#: Accepted `pyright` errors. Never raise this.
PYRIGHT_BASELINE = 9


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, check=False)


def _mypy_error_count(output: str) -> int:
    match = re.search(r"^Found (\d+) errors?", output, re.MULTILINE)
    if match:
        return int(match.group(1))
    assert "Success" in output, f"unrecognized mypy output:\n{output}"
    return 0


def _pyright_error_count(output: str) -> int:
    report = json.loads(output)
    return sum(
        1 for diagnostic in report["generalDiagnostics"] if diagnostic.get("severity") == "error"
    )


@pytest.mark.skipif(importlib.util.find_spec("mypy") is None, reason="mypy is not installed")
def test_mypy_stays_within_budget() -> None:
    proc = _run([sys.executable, "-m", "mypy", "--strict", TARGET])
    count = _mypy_error_count(proc.stdout)
    assert count <= MYPY_BASELINE, (
        f"mypy --strict reports {count} errors, budget is {MYPY_BASELINE}. "
        f"New strict-mode debt is not accepted.\n{proc.stdout}"
    )


@pytest.mark.skipif(importlib.util.find_spec("pyright") is None, reason="pyright is not installed")
def test_pyright_stays_within_budget() -> None:
    proc = _run([sys.executable, "-m", "pyright", "--outputjson"])
    if not proc.stdout.strip():
        pytest.skip(f"pyright produced no report:\n{proc.stderr}")
    count = _pyright_error_count(proc.stdout)
    assert count <= PYRIGHT_BASELINE, (
        f"pyright reports {count} errors, budget is {PYRIGHT_BASELINE}. "
        f"New type debt is not accepted.\n{proc.stdout}"
    )

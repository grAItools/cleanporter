"""Suite-wide fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_ambient_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the caller's environments out of interpreter detection.

    With no ``python`` configured, a run probes with an interpreter found from
    ``$UV_PROJECT_ENVIRONMENT`` or ``$VIRTUAL_ENV`` (`cleanporter._interpreter`).
    Whatever the suite happens to run under -- ``uv run`` sets one, an
    activated shell another -- must not decide which interpreter a test's
    project is probed with, so both are cleared for every test; a test that
    wants one sets it.
    """
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.delenv("UV_PROJECT_ENVIRONMENT", raising=False)

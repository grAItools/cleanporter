"""The two ruff pins must agree.

Ruff is installed twice, on purpose and unavoidably. `uv run ruff check` uses
the version resolved into `uv.lock`; the git hook -- and therefore CI, which
runs the hooks -- uses the version ruff-pre-commit builds from the `rev` in
`.pre-commit-config.yaml`, in an environment prek manages itself. There is no
way to make one of them read the other.

That is a drift vector with a nasty shape: the two only disagree after
somebody runs `uv lock --upgrade`, and when they do, the local command a
contributor is told to run and the check that gates their pull request are
running different linters. A formatting rule that changed between the two
versions then produces a CI failure that does not reproduce locally.

`.pre-commit-config.yaml` carries a "keep in step with uv.lock" comment. This
test is what makes that comment true.
"""

from __future__ import annotations

import pathlib
import re
import tomllib

ROOT = pathlib.Path(__file__).resolve().parents[1]

#: `rev: v0.16.4` under the ruff-pre-commit repo block. The quotes are
#: optional in YAML and several formatters add them, so they are stripped
#: rather than captured -- capturing them turns an agreeing pair into a
#: failure that tells you to bump the version you already have.
_REV = re.compile(
    r"-\s*repo:\s*https://github\.com/astral-sh/ruff-pre-commit\s*\n"
    r"(?:\s*#.*\n)*\s*rev:\s*[\"']?v?([^\"'\s]+)"
)


def _hook_ruff_version() -> str:
    text = (ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    match = _REV.search(text)
    assert match is not None, "could not find the ruff-pre-commit `rev:` in .pre-commit-config.yaml"
    return match.group(1)


def _locked_ruff_version() -> str:
    with (ROOT / "uv.lock").open("rb") as handle:
        lock = tomllib.load(handle)
    versions = [p["version"] for p in lock["package"] if p["name"] == "ruff"]
    assert len(versions) == 1, f"expected exactly one ruff in uv.lock, found {versions}"
    return str(versions[0])


def test_hook_ruff_matches_the_lockfile() -> None:
    hook, locked = _hook_ruff_version(), _locked_ruff_version()
    assert hook == locked, (
        f"ruff-pre-commit is pinned to v{hook} but uv.lock resolves ruff {locked}. "
        f"`uv run ruff check` and the git hook (and so CI) would run different "
        f"linters. Bump `rev:` in .pre-commit-config.yaml to v{locked}."
    )

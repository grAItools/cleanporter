"""The release workflow's checks, run against this repository's own files.

`.github/workflows/release.yml` publishes only when the pushed tag names
``project.version`` and CHANGELOG.md has a section for it; both checks live in
`.github/scripts/release.py`. These tests run that script the way the
workflow does -- by path, in a child process -- so a version bump without its
changelog section fails here, in the pull request, rather than at tag time.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tomllib

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github" / "scripts" / "release.py"
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"


def _release(*args: str) -> subprocess.CompletedProcess[str]:
    # The script writes UTF-8 itself; the variable covers anything Python
    # prints before `main` runs (a traceback) on a cp1252 Windows pipe.
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=dict(os.environ, PYTHONIOENCODING="utf-8"),
    )


def _version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        version = tomllib.load(handle)["project"]["version"]
    assert isinstance(version, str)
    return version


def _jobs() -> dict[str, dict[str, object]]:
    """The workflow's jobs, parsed by PyYAML in a child process (as `test_whole_project`)."""
    code = (
        "import json, sys, yaml\n"
        "with open(sys.argv[1], encoding='utf-8') as f:\n"
        "    json.dump(yaml.safe_load(f)['jobs'], sys.stdout)\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code, str(WORKFLOW)], capture_output=True, text=True, check=True
    ).stdout
    loaded: object = json.loads(out)
    assert isinstance(loaded, dict)
    jobs: dict[str, dict[str, object]] = {}
    for name, job in loaded.items():
        assert isinstance(job, dict)
        jobs[str(name)] = {str(k): v for k, v in job.items()}
    return jobs


def test_the_tag_for_pyproject_version_passes() -> None:
    done = _release("check-tag", f"v{_version()}")
    assert done.returncode == 0, done.stderr


def test_any_other_tag_fails_and_says_which_version_it_wanted() -> None:
    version = _version()
    for tag in (version, f"v{version}.1", "vfoo"):  # no `v`; a longer version; garbage
        done = _release("check-tag", tag)
        assert done.returncode == 1, tag
        assert f"v{version}" in done.stderr


def test_a_tag_that_is_not_a_normalised_public_version_fails_as_such() -> None:
    """``0.05``, ``-rc1``, ``+local``: PyPI would rewrite or refuse them."""
    for tag in ("v0.05", "v0.5.0-rc1", "v0.5.0+local", "V0.5.0", "v1!0.5.0", "v0.5.0RC1"):
        done = _release("check-tag", tag)
        assert done.returncode == 1, tag
        assert "normalised public version" in done.stderr, tag


def test_the_current_version_has_release_notes() -> None:
    """The bump PR carries the CHANGELOG section, so the tag cannot lack one."""
    done = _release("notes", _version())
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip()
    assert "## [" not in done.stdout  # one section: it stops at the next heading
    assert "]: https://" not in done.stdout  # ... or at the link definitions
    assert "policy above" not in done.stdout  # the notes are read without the preamble


def test_a_version_without_a_section_has_no_notes() -> None:
    done = _release("notes", "0.0.0")
    assert done.returncode == 1
    assert "## [0.0.0] - DATE" in done.stderr


def test_misuse_is_exit_2() -> None:
    assert _release().returncode == 2
    assert _release("publish", "v1").returncode == 2


def test_the_workflow_runs_every_check_before_building() -> None:
    steps = _jobs()["build"]["steps"]
    assert isinstance(steps, list)
    runs = [str(step.get("run", "")) for step in steps if isinstance(step, dict)]
    build = next(i for i, run in enumerate(runs) if run.strip() == "uv build --no-sources")
    for check in (
        "git merge-base --is-ancestor",
        "release.py check-tag",
        "release.py notes",
        "pytest",
    ):
        assert next(i for i, run in enumerate(runs) if check in run) < build, check
    checkout = steps[0]
    assert isinstance(checkout, dict)
    assert checkout["with"] == {"fetch-depth": 0}  # or origin/main is not there to compare


def test_pypi_is_opt_in_and_uses_trusted_publishing() -> None:
    """PyPI publishing waits for a repository variable; the GitHub release does not."""
    jobs = _jobs()
    pypi = jobs["pypi"]
    assert pypi["if"] == "vars.PUBLISH_TO_PYPI == 'true'"
    assert pypi["permissions"] == {"id-token": "write"}  # OIDC, and nothing else
    environment = pypi["environment"]
    assert isinstance(environment, dict)
    assert environment["name"] == "pypi"
    assert "if" not in jobs["github-release"]
    assert jobs["github-release"]["permissions"] == {"contents": "write"}

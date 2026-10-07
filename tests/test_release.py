# Copyright (c) 2026 grAItools
# SPDX-License-Identifier: BSD-3-Clause
# See LICENSE for the full license text.

"""The release workflow's checks, run against this repository's own files.

`.github/workflows/release.yml` publishes only when the tag names
``project.version`` and ``.github/release-notes/X.Y.Z.md`` holds curated notes
for it; both checks live in `.github/scripts/release.py`. These tests run that
script the way the workflow does -- by path, in a child process -- so a
version bump without its notes fails here, in the pull request, rather than
at release time.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tomllib

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github" / "scripts" / "release.py"
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"


def _release(*args: str, script: pathlib.Path = SCRIPT) -> subprocess.CompletedProcess[str]:
    # The script writes UTF-8 itself; the variable covers anything Python
    # prints before `main` runs (a traceback) on a cp1252 Windows pipe.
    return subprocess.run(
        [sys.executable, str(script), *args],
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


def _workflow() -> dict[str, object]:
    """The workflow, parsed by PyYAML in a child process (as `test_whole_project`).

    YAML 1.1 reads the key ``on`` as ``true``, which JSON writes as ``"true"``.
    """
    code = (
        "import json, sys, yaml\n"
        "with open(sys.argv[1], encoding='utf-8') as f:\n"
        "    json.dump(yaml.safe_load(f), sys.stdout)\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code, str(WORKFLOW)], capture_output=True, text=True, check=True
    ).stdout
    loaded: object = json.loads(out)
    assert isinstance(loaded, dict)
    return {str(k): v for k, v in loaded.items()}


def _jobs() -> dict[str, dict[str, object]]:
    loaded = _workflow()["jobs"]
    assert isinstance(loaded, dict)
    jobs: dict[str, dict[str, object]] = {}
    for name, job in loaded.items():
        assert isinstance(job, dict)
        jobs[str(name)] = {str(k): v for k, v in job.items()}
    return jobs


def _build_runs() -> list[str]:
    steps = _jobs()["build"]["steps"]
    assert isinstance(steps, list)
    return [str(step.get("run", "")) for step in steps if isinstance(step, dict)]


def _notes_in(tmp_path: pathlib.Path, version: str, text: str | None) -> pathlib.Path:
    """A copy of the script in a scratch repository layout, with *text* as *version*'s notes."""
    script = tmp_path / ".github" / "scripts" / "release.py"
    script.parent.mkdir(parents=True)
    shutil.copyfile(SCRIPT, script)
    notes = tmp_path / ".github" / "release-notes"
    notes.mkdir()
    if text is not None:
        (notes / f"{version}.md").write_text(text, encoding="utf-8", newline="\n")
    return script


def test_the_tag_for_pyproject_version_passes() -> None:
    done = _release("check-tag", f"v{_version()}")
    assert done.returncode == 0, done.stderr


def test_check_tag_reads_the_pyproject_it_is_given(tmp_path: pathlib.Path) -> None:
    """The workflow runs this revision's script against the tag's tree."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text('[project]\nversion = "9.8.7"\n', encoding="utf-8", newline="\n")
    assert _release("check-tag", "v9.8.7", str(pyproject)).returncode == 0
    done = _release("check-tag", f"v{_version()}", str(pyproject))
    assert done.returncode == 1
    assert "v9.8.7" in done.stderr


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
    """The release PR adds the notes, so the release cannot lack them."""
    version = _version()
    done = _release("notes", version)
    assert done.returncode == 0, done.stderr
    path = ROOT / ".github" / "release-notes" / f"{version}.md"
    assert done.stdout == path.read_text(encoding="utf-8").strip() + "\n"
    assert "CHANGELOG.md" in done.stdout  # the full record is a link away


def test_the_current_version_has_a_changelog_section_the_notes_link_to() -> None:
    """The notes summarise; CHANGELOG.md is the record, and the link must land on it."""
    version = _version()
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    heading = re.search(
        rf"^## (\[{re.escape(version)}\] - \d{{4}}-\d{{2}}-\d{{2}})$", changelog, re.MULTILINE
    )
    assert heading, f"CHANGELOG.md has no '## [{version}] - DATE' section"
    # GitHub's heading slug: lower-cased, punctuation but `-` and `_` dropped,
    # each space a hyphen. `[0.4.0] - 2026-09-28` -> `040---2026-09-28`.
    slug = re.sub(r"[^\w\- ]", "", heading.group(1).lower()).replace(" ", "-")
    notes = (ROOT / ".github" / "release-notes" / f"{version}.md").read_text(encoding="utf-8")
    anchors = re.findall(r"/blob/main/CHANGELOG\.md#([^)\s]+)", notes)
    assert anchors == [slug]


def test_release_notes_over_the_cap_fail(tmp_path: pathlib.Path) -> None:
    """A summary long enough to hit the cap has stopped being one."""
    at_cap = "x" * 4000
    assert _release("notes", "9.9.9", script=_notes_in(tmp_path, "9.9.9", at_cap)).returncode == 0
    done = _release("notes", "9.9.9", script=_notes_in(tmp_path / "over", "9.9.9", at_cap + "x"))
    assert done.returncode == 1
    assert "4001 characters" in done.stderr
    assert done.stdout == ""


def test_missing_or_empty_release_notes_fail(tmp_path: pathlib.Path) -> None:
    done = _release("notes", "9.9.9", script=_notes_in(tmp_path / "none", "9.9.9", None))
    assert done.returncode == 1
    assert ".github/release-notes/9.9.9.md does not exist" in done.stderr
    done = _release("notes", "9.9.9", script=_notes_in(tmp_path / "blank", "9.9.9", " \n\n"))
    assert done.returncode == 1
    assert "is empty" in done.stderr


def test_notes_take_a_version_not_a_path() -> None:
    for version in ("../../pyproject", "v0.4.0", ""):
        done = _release("notes", version)
        assert done.returncode == 1, version
        assert "normalised public version" in done.stderr, version


def test_misuse_is_exit_2() -> None:
    assert _release().returncode == 2
    assert _release("publish", "v1").returncode == 2


def test_the_workflow_runs_every_check_before_building() -> None:
    runs = _build_runs()
    build = next(i for i, run in enumerate(runs) if run.strip() == "uv build --no-sources")
    for check in (
        "git merge-base --is-ancestor",
        'release.py" check-tag',
        'release.py" notes',
        "releases/tags/$TAG",  # no GitHub release for the tag yet
        "pytest",
        "git status --porcelain",  # and nothing left in the tree to be packed
    ):
        assert next(i for i, run in enumerate(runs) if check in run) < build, check
    steps = _jobs()["build"]["steps"]
    assert isinstance(steps, list)
    [checkout] = [
        s
        for s in steps
        if isinstance(s, dict) and str(s.get("uses")).startswith("actions/checkout")
    ]
    assert checkout["with"] == {"fetch-depth": 0}  # or origin/main is not there to compare


def test_the_tag_shape_is_checked_before_the_tag_is_used() -> None:
    """On a dispatch the tag is free text: the job's first step pins its shape."""
    steps = _jobs()["build"]["steps"]
    assert isinstance(steps, list)
    first = steps[0]
    assert isinstance(first, dict)
    run = str(first["run"])
    assert "[[ $TAG =~ ^v[0-9]+" in run
    assert run.rstrip().endswith("fi")
    pattern = run[run.index("^v") : run.index("$ ]]") + 1]
    for good in ("v0.4.0", "v0.5.0rc1", "v1.0.0b2", "v0.5.0.post1", "v0.5.0.dev0", "v1"):
        assert re.fullmatch(pattern, good), good
    for bad in ("0.4.0", "v0.4.0-rc1", "v0.4.0+x", "v0.4.0/../x", "v0.4.0 -x", "--help", "v"):
        assert not re.fullmatch(pattern, bad), bad


def test_the_workflow_can_be_dispatched_with_a_tag() -> None:
    """Run from the Actions tab: the tag is an input, and required."""
    on = _workflow()["true"]  # YAML 1.1's `on`
    assert isinstance(on, dict)
    assert on["push"] == {"tags": ["v*"]}
    dispatch = on["workflow_dispatch"]
    assert isinstance(dispatch, dict)
    inputs = dispatch["inputs"]
    assert isinstance(inputs, dict)
    tag = inputs["tag"]
    assert isinstance(tag, dict)
    assert tag["required"] is True
    assert tag["type"] == "string"
    # Free text: only ever read through the environment.
    assert _workflow()["env"] == {"TAG": "${{ inputs.tag || github.ref_name }}"}
    assert "inputs.tag" not in "\n".join(_build_runs())


def test_a_dispatch_is_refused_unless_it_is_from_main() -> None:
    """The machinery comes from the dispatched revision, which has to be main."""
    runs = _build_runs()
    [machinery] = [run for run in runs if "git show" in run]
    assert '"$GITHUB_EVENT_NAME" == workflow_dispatch' in machinery
    assert '"$GITHUB_REF" != refs/heads/main' in machinery
    assert machinery.index("refs/heads/main") < machinery.index("git show")


def test_the_machinery_is_read_from_the_workflow_revision() -> None:
    """Not from the tag's tree: v0.4.0 predates release.py and its notes."""
    runs = _build_runs()
    [machinery] = [run for run in runs if "git show" in run]
    assert 'git show "$GITHUB_SHA:.github/scripts/release.py"' in machinery
    assert 'git show "$GITHUB_SHA:$notes"' in machinery
    assert 'notes=".github/release-notes/${TAG#v}.md"' in machinery
    assert '"$RUNNER_TEMP/machinery"' in machinery  # outside the checkout, so not packed
    for run in runs:
        if "release.py" in run and run is not machinery:
            assert '"$RUNNER_TEMP/machinery/.github/scripts/release.py"' in run, run
    [check_tag] = [run for run in runs if " check-tag " in run]
    assert check_tag.rstrip().endswith('"$TAG" pyproject.toml')  # the released tree's


def test_a_missing_tag_is_created_on_the_dispatched_commit() -> None:
    """Only where the build job said so; otherwise the tag must already exist."""
    jobs = _jobs()
    outputs = jobs["build"]["outputs"]
    assert isinstance(outputs, dict)
    assert "create-tag" in outputs
    all_steps = jobs["github-release"]["steps"]
    assert isinstance(all_steps, list)
    steps = [s for s in all_steps if isinstance(s, dict)]
    names = [s.get("name") for s in steps]
    tag, release = steps[names.index("Create the tag")], steps[names.index("Create the release")]
    assert names.index("Create the tag") < names.index("Create the release")
    assert tag["if"] == "needs.build.outputs.create-tag == 'true'"
    run = str(tag["run"])
    # An annotated tag object, then the ref: creating the ref is atomic and
    # fails if the tag exists, so there is no check-then-act.
    assert run.index('"repos/$GITHUB_REPOSITORY/git/tags"') < run.index(
        '"repos/$GITHUB_REPOSITORY/git/refs"'
    )
    assert '-f object="$GITHUB_SHA"' in run
    assert '-f ref="refs/tags/$TAG"' in run
    assert "git/ref/" not in run  # no lookup first
    assert "--verify-tag" in str(release["run"])
    assert "--target" not in str(release["run"])


def test_the_release_notes_are_written_outside_the_checkout() -> None:
    """Inside it, they would be packed into the sdist -- which PyPI never lets be replaced."""
    steps = _jobs()["build"]["steps"]
    assert isinstance(steps, list)
    [notes] = [s for s in steps if isinstance(s, dict) and 'release.py" notes' in str(s.get("run"))]
    run = str(notes["run"])
    assert '> "$RUNNER_TEMP/notes/notes.md"' in run
    assert run.count(">") == 1  # the one redirection is that one
    [upload] = [
        s for s in steps if isinstance(s, dict) and s.get("name") == "Upload the release notes"
    ]
    upload_with = upload["with"]
    assert isinstance(upload_with, dict)
    assert str(upload_with["path"]).startswith("${{ runner.temp }}/")


def test_pypi_is_opt_in_and_uses_trusted_publishing() -> None:
    """PyPI publishing waits for a repository variable; the GitHub release does not."""
    jobs = _jobs()
    pypi = jobs["pypi"]
    assert pypi["if"] == "vars.PUBLISH_TO_PYPI == 'true'"
    assert pypi["permissions"] == {"id-token": "write"}  # OIDC, and nothing else
    environment = pypi["environment"]
    assert isinstance(environment, dict)
    assert environment["name"] == "pypi"
    # Last: an upload can never be undone, so only once the tag and release exist.
    assert pypi["needs"] == ["build", "github-release"]
    assert "if" not in jobs["github-release"]
    assert jobs["github-release"]["permissions"] == {"contents": "write"}
    assert "permissions" not in jobs["build"]  # the workflow's `contents: read`
    assert _workflow()["permissions"] == {"contents": "read"}

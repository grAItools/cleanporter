#!/usr/bin/env python
"""The two checks `.github/workflows/release.yml` makes before anything is published.

``check-tag vX.Y.Z`` exits 1 unless the pushed tag names exactly
``project.version`` in pyproject.toml. The wheel and sdist take their version
from pyproject.toml, not from the tag, so a tag that disagrees would publish
one version under another's name -- and PyPI never lets a version be
re-uploaded, so the mistake could not be taken back.

``notes X.Y.Z`` prints the body of CHANGELOG.md's ``## [X.Y.Z] - DATE``
section, the release notes, and exits 1 when there is no such section or it
is empty: a release whose notes say nothing is a release whose changelog step
was skipped.

Standard library only, and run with ``uv run --no-project``: the release must
not depend on installing the project it is about to build.
"""

from __future__ import annotations

import pathlib
import re
import sys
import tomllib

ROOT = pathlib.Path(__file__).resolve().parents[2]

_USAGE = "usage: release.py check-tag vX.Y.Z | release.py notes X.Y.Z\n"


def project_version() -> str:
    """``project.version`` from pyproject.toml."""
    with (ROOT / "pyproject.toml").open("rb") as handle:
        version = tomllib.load(handle)["project"]["version"]
    if not isinstance(version, str):
        msg = "pyproject.toml: project.version is not a string"
        raise TypeError(msg)
    return version


def changelog_section(version: str, changelog: str) -> str | None:
    """The body of *changelog*'s ``## [version] - DATE`` section, stripped.

    It runs to the next ``## `` heading or to the link reference definitions
    (``[0.4.0]: https://...``) that close the file. ``None`` when there is
    no such heading.
    """
    heading = re.compile(rf"^## \[{re.escape(version)}\] - \S.*$", re.MULTILINE)
    match = heading.search(changelog)
    if match is None:
        return None
    end = re.compile(r"^(## |\[[^\]]+\]: )", re.MULTILINE).search(changelog, match.end())
    return changelog[match.end() : end.start() if end else len(changelog)].strip()


def _check_tag(tag: str) -> int:
    version = project_version()
    if tag != f"v{version}":
        sys.stderr.write(
            f"release: the tag {tag!r} does not name pyproject.toml's version {version!r}; "
            f"tag v{version}, or bump project.version first\n"
        )
        return 1
    sys.stdout.write(f"release: {tag} is project.version {version}\n")
    return 0


def _notes(version: str) -> int:
    body = changelog_section(version, (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))
    if not body:
        sys.stderr.write(
            f"release: CHANGELOG.md has no non-empty '## [{version}] - DATE' section\n"
        )
        return 1
    sys.stdout.write(f"{body}\n")
    return 0


def main(argv: list[str]) -> int:
    """Run the subcommand in *argv*; 0 when the check passes, 1 when not, 2 on misuse."""
    match argv:
        case ["check-tag", tag]:
            return _check_tag(tag)
        case ["notes", version]:
            return _notes(version)
        case _:
            sys.stderr.write(_USAGE)
            return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

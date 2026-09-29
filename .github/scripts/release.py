#!/usr/bin/env python
"""The two checks `.github/workflows/release.yml` makes before anything is published.

``check-tag vX.Y.Z [PYPROJECT]`` exits 1 unless the tag names exactly
``project.version`` in pyproject.toml (this checkout's, or the one at
*PYPROJECT*), and that version is a normalised public PEP 440 version
(``0.5.0``, ``0.5.0rc1``, ``0.5.0.post1``; not ``0.05``, ``0.5.0-rc1`` or
``0.5.0+local``, which PyPI would rewrite or refuse). The wheel and sdist take
their version from pyproject.toml, not from the tag, so a tag that disagrees
would publish one version under another's name -- and PyPI never lets a
version be re-uploaded, so the mistake could not be taken back. The workflow
passes *PYPROJECT* because it runs this script from its own revision against
the tag's tree, which may predate the script.

``notes X.Y.Z`` prints ``.github/release-notes/X.Y.Z.md``, the curated release
notes, and exits 1 when that file is missing, empty, or longer than
``NOTES_CAP`` characters. The notes are a summary written for the release, not
the CHANGELOG section: that is the full record, and 0.4.0's was too long for a
GitHub release to accept, let alone to read as one. The notes link to it.

Standard library only, and run with ``uv run --no-project``: the release must
not depend on installing the project it is about to build. Output is UTF-8
whatever the platform's pipe encoding, since the notes are not ASCII.
"""

from __future__ import annotations

import io
import pathlib
import re
import sys
import tomllib

ROOT = pathlib.Path(__file__).resolve().parents[2]

_USAGE = "usage: release.py check-tag vX.Y.Z [PYPROJECT] | release.py notes X.Y.Z\n"

#: The longest release notes accepted, in characters: a summary that has grown
#: past this has stopped being one.
NOTES_CAP = 4000

#: A normalised public PEP 440 version without an epoch: what `packaging`
#: would print back unchanged, and what PyPI stores as written.
_NORMALISED = re.compile(
    r"(0|[1-9]\d*)(\.(0|[1-9]\d*))*"
    r"((a|b|rc)(0|[1-9]\d*))?"
    r"(\.post(0|[1-9]\d*))?"
    r"(\.dev(0|[1-9]\d*))?"
)


def project_version(pyproject: pathlib.Path) -> str:
    """``project.version`` from the pyproject.toml at *pyproject*."""
    with pyproject.open("rb") as handle:
        version = tomllib.load(handle)["project"]["version"]
    if not isinstance(version, str):
        msg = "pyproject.toml: project.version is not a string"
        raise TypeError(msg)
    return version


def _check_tag(tag: str, pyproject: pathlib.Path) -> int:
    version = project_version(pyproject)
    if not tag.startswith("v") or not _NORMALISED.fullmatch(tag[1:]):
        sys.stderr.write(
            f"release: the tag {tag!r} is not 'v' + a normalised public version "
            f"(pyproject.toml has {version!r}, so the tag is v{version})\n"
        )
        return 1
    if tag != f"v{version}":
        sys.stderr.write(
            f"release: the tag {tag!r} does not name pyproject.toml's version {version!r}; "
            f"tag v{version}, or bump project.version first\n"
        )
        return 1
    sys.stdout.write(f"release: {tag} is project.version {version}\n")
    return 0


def _notes(version: str) -> int:
    if not _NORMALISED.fullmatch(version):  # it names a file: no `../`, no `v`
        sys.stderr.write(f"release: {version!r} is not a normalised public version\n")
        return 1
    path = ROOT / ".github" / "release-notes" / f"{version}.md"
    name = path.relative_to(ROOT).as_posix()
    if not path.is_file():
        sys.stderr.write(f"release: {name} does not exist; the release PR adds it\n")
        return 1
    body = path.read_text(encoding="utf-8").strip()
    if not body:
        sys.stderr.write(f"release: {name} is empty\n")
        return 1
    if len(body) > NOTES_CAP:
        sys.stderr.write(
            f"release: {name} is {len(body)} characters, over the cap of {NOTES_CAP}; "
            "summarise, and link to CHANGELOG.md for the rest\n"
        )
        return 1
    sys.stdout.write(f"{body}\n")
    return 0


def main(argv: list[str]) -> int:
    """Run the subcommand in *argv*; 0 when the check passes, 1 when not, 2 on misuse."""
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):  # a cp1252 pipe on Windows otherwise
            stream.reconfigure(encoding="utf-8")
    match argv:
        case ["check-tag", tag]:
            return _check_tag(tag, ROOT / "pyproject.toml")
        case ["check-tag", tag, pyproject]:
            return _check_tag(tag, pathlib.Path(pyproject))
        case ["notes", version]:
            return _notes(version)
        case _:
            sys.stderr.write(_USAGE)
            return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

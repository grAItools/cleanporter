#!/usr/bin/env python
# Copyright (c) 2026 grAItools
# SPDX-License-Identifier: BSD-3-Clause
# See LICENSE for the full license text.

"""Check or apply the repository's canonical Python license header.

Git defines the scope, including untracked, nonignored Python files. This
keeps fixtures and hidden operator scripts covered without touching downloaded
corpora or environments. Work with bytes so fixing a header preserves source
encoding and line endings. Only recognized notices owned by grAItools may be
normalized; conflicting notices require a human decision.
"""

from __future__ import annotations

import argparse
import io
import os
import pathlib
import re
import subprocess
import sys
import tokenize

ROOT = pathlib.Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / ".license-header.txt"
_BOM = b"\xef\xbb\xbf"
_COOKIE = re.compile(rb"^[ \t\f]*#.*?coding[:=][ \t]*[-\w.]+")
_NOTICE = re.compile(
    rb"^\s*#\s*(?:copyright\b|spdx-(?:license-identifier|filecopyrighttext)"
    rb"|.*\blicensed? under\b|licen[cs]e\s*:|(?:MIT|BSD.*|Apache.*) License\b)",
    re.IGNORECASE,
)
_OWNED = re.compile(
    rb"^#\s*(?:Copyright\s+\(c\)\s+|SPDX-FileCopyrightText:\s*)"
    rb"\d{4}(?:-\d{4})?\s+grAItools\.?$",
    re.IGNORECASE,
)
_LICENSE = re.compile(rb"^#\s*SPDX-License-Identifier:\s*(?:BSD-3-Clause|MIT)$", re.IGNORECASE)
_REFERENCE = re.compile(rb"^# See LICENSE\b.*$", re.IGNORECASE)


def _files(root: pathlib.Path) -> list[pathlib.Path]:
    """Return existing tracked and nonignored untracked Python paths."""
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root,
        capture_output=True,
        check=True,
    )
    return sorted(
        {
            root / os.fsdecode(name)
            for name in result.stdout.split(b"\0")
            if name.endswith(b".py")
            and ((root / os.fsdecode(name)).exists() or (root / os.fsdecode(name)).is_symlink())
        }
    )


def _preamble(lines: list[bytes]) -> int:
    """Keep shebangs and PEP 263 encoding cookies in their original positions."""
    if len(lines) > 1 and _COOKIE.match(lines[1]):
        return 2
    return int(bool(lines and (lines[0].startswith(b"#!") or _COOKIE.match(lines[0]))))


def _leading_comments(lines: list[bytes]) -> int:
    for index, line in enumerate(lines):
        if line.strip() and not line.lstrip().startswith(b"#"):
            return index
    return len(lines)


def _without_header(lines: list[bytes]) -> list[bytes]:
    """Remove only recognized owned notices from the leading comment region."""
    end = _leading_comments(lines)
    prefix = lines[:end]
    notices = [line.strip() for line in prefix if _NOTICE.search(line)]
    if notices and not any(_OWNED.fullmatch(line) for line in notices):
        msg = "license notice has no recognized grAItools copyright"
        raise ValueError(msg)
    if any(not (_OWNED.fullmatch(line) or _LICENSE.fullmatch(line)) for line in notices):
        msg = "conflicting or unrecognized license notice"
        raise ValueError(msg)
    remaining: list[bytes] = []
    after_header = False
    for line in prefix:
        if _NOTICE.search(line) or _REFERENCE.fullmatch(line.strip()):
            after_header = True
        elif line.strip() or not after_header:
            remaining.append(line)
            after_header = False
    # Normalize the separator without changing substantive source content.
    while remaining and not remaining[0].strip():
        remaining.pop(0)
    return remaining + lines[end:]


def _normalized(raw: bytes, template: bytes) -> bytes:
    """Produce the canonical header while retaining BOM, encoding and newlines."""
    tokenize.detect_encoding(io.BytesIO(raw).readline)
    bom = _BOM if raw.startswith(_BOM) else b""
    source = raw[len(bom) :]
    lines = source.splitlines(keepends=True)
    newline = b"\r\n" if b"\r\n" in source[: source.find(b"\n") + 1] else b"\n"
    start = _preamble(lines)
    if any(_NOTICE.search(line) for line in lines[:start]):
        msg = "license notice before or inside an encoding declaration"
        raise ValueError(msg)
    _check_placement(raw, start + _leading_comments(lines[start:]))
    preamble = b"".join(lines[:start])
    if preamble and not preamble.endswith(b"\n"):
        preamble += newline
    header = template.replace(b"\n", newline) + newline
    return bom + preamble + header + b"".join(_without_header(lines[start:]))


def _check_placement(raw: bytes, prefix_end: int) -> None:
    """Refuse misplaced notices without mistaking embedded source strings for headers."""
    for token in tokenize.tokenize(io.BytesIO(raw).readline):
        if (
            token.type == tokenize.COMMENT
            and token.start[0] > prefix_end
            and _NOTICE.search(token.string.encode("utf-8"))
        ):
            msg = "license notice outside the leading header block"
            raise ValueError(msg)


def _template(path: pathlib.Path) -> bytes:
    template = path.read_bytes()
    if (
        not template.endswith(b"\n")
        or b"\r" in template
        or not template.splitlines()
        or any(not line.startswith(b"# ") for line in template.splitlines())
    ):
        msg = "template must contain LF-terminated comment lines and no blank separator"
        raise ValueError(msg)
    return template


def _process(path: pathlib.Path, template: bytes, *, fix: bool) -> bool:
    if path.is_symlink():
        msg = "source symlinks are not supported"
        raise ValueError(msg)
    raw = path.read_bytes()
    expected = _normalized(raw, template)
    if raw == expected:
        return True
    if fix:
        path.write_bytes(expected)
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--check", action="store_true", help="report violations without writing")
    modes.add_argument("--fix", action="store_true", help="apply or normalize owned headers")
    args = parser.parse_args(argv)
    try:
        template = _template(TEMPLATE)
        files = _files(ROOT)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"license headers: {exc}", file=sys.stderr)
        return 2
    violations = False
    errors = False
    for path in files:
        try:
            valid = _process(path, template, fix=args.fix)
        except (OSError, SyntaxError, UnicodeError, tokenize.TokenError) as exc:
            print(f"{path.relative_to(ROOT)}: {exc}", file=sys.stderr)
            errors = True
        except ValueError as exc:
            print(f"{path.relative_to(ROOT)}: {exc}", file=sys.stderr)
            violations = True
        else:
            if not valid:
                action = "applied header" if args.fix else "missing or inconsistent header"
                print(f"{path.relative_to(ROOT)}: {action}")
                violations = violations or not args.fix
    return 2 if errors else int(violations)


if __name__ == "__main__":
    raise SystemExit(main())

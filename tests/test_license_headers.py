# Copyright (c) 2026 grAItools
# SPDX-License-Identifier: BSD-3-Clause
# See LICENSE for the full license text.

"""License enforcement covers repository-owned code and preserves source bytes."""

from __future__ import annotations

import ast
import pathlib
import shutil
import subprocess
import sys
import tomllib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github" / "scripts" / "license_headers.py"
HEADER = (ROOT / ".license-header.txt").read_bytes() + b"\n"


def _git(root: pathlib.Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, capture_output=True, check=True)


@pytest.fixture
def repository(tmp_path):
    script = tmp_path / ".github" / "scripts" / "license_headers.py"
    script.parent.mkdir(parents=True)
    shutil.copyfile(SCRIPT, script)
    shutil.copyfile(ROOT / ".license-header.txt", tmp_path / ".license-header.txt")
    _git(tmp_path, "init", "--quiet")
    # The copied utility is itself part of the checked repository.
    assert _run(tmp_path, "--fix").returncode == 0
    return tmp_path


def _run(root: pathlib.Path, *args: str):
    return subprocess.run(
        [sys.executable, str(root / ".github" / "scripts" / "license_headers.py"), *args],
        cwd=root / ".github",
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("preamble", "body"),
    [
        (b"", b""),
        (b"", b"x = 1"),
        (
            b"",
            b'"""Module documentation."""\nfrom __future__ import annotations\nx = "caf\xc3\xa9"\n',
        ),
        (b"#!/usr/bin/env python\n", b"print('hello')\n"),
        (b"# coding: latin-1\n", b"x = 'caf\xe9'\n"),
        (b"#!/usr/bin/python\n# coding: latin-1\n", b"x = 'caf\xe9'\n"),
        (b"# an unrelated comment\n# coding: latin-1\n", b"x = 'caf\xe9'\n"),
        (b"\xef\xbb\xbf", b"x = 1\n"),
        (b"#!/usr/bin/python\r\n", b"x = 1\r\n"),
    ],
)
def test_fix_preserves_source_and_is_idempotent(repository, preamble, body):
    path = repository / "a file.py"
    raw = preamble + body
    path.write_bytes(raw)
    before = ast.dump(ast.parse(raw))
    checked = _run(repository, "--check")
    assert checked.returncode == 1
    assert "a file.py" in checked.stdout
    assert path.read_bytes() == raw
    assert _run(repository, "--fix").returncode == 0
    header = HEADER.replace(b"\n", b"\r\n") if b"\r\n" in raw else HEADER
    expected = preamble + header + body
    assert path.read_bytes() == expected
    assert ast.dump(ast.parse(expected)) == before
    assert _run(repository, "--check").returncode == 0
    assert _run(repository, "--fix").returncode == 0
    assert path.read_bytes() == expected


@pytest.mark.parametrize(
    "old",
    [
        HEADER + HEADER,
        HEADER.rstrip(b"\n") + b"\n",
        HEADER + b"\n\n",
        HEADER.replace(b"2026", b"2025"),
        HEADER.replace(b"Copyright (c)", b"copyright   (c)").replace(b"# SPDX", b"#SPDX"),
        HEADER.replace(b"BSD-3-Clause", b"MIT"),
        HEADER.replace(b"See LICENSE for the full license text.", b"See LICENSE for license text."),
        b"# Copyright (c) 2026 grAItools\n",
        b"# See LICENSE for the full license text.\n",
    ],
)
def test_normalize_owned_headers(repository, old):
    path = repository / "example.py"
    body = b"# Preserve this comment.\nx = 1\n"
    path.write_bytes(old + body)
    assert _run(repository, "--check").returncode == 1
    assert path.read_bytes() == old + body
    assert _run(repository, "--fix").returncode == 0
    assert path.read_bytes() == HEADER + body
    assert _run(repository, "--check").returncode == 0


@pytest.mark.parametrize(
    "notice",
    [
        b"# Copyright (c) 2026 Someone Else\n",
        b"# SPDX-License-Identifier: Apache-2.0\n",
        b"# Copyright (c) 2026 grAItools\n# SPDX-License-Identifier: Apache-2.0\n",
        b"# Copyright (c) 2026 grAItools\n# Copyright (c) 2026 Someone Else\n",
        b"# Licensed under Apache-2.0\n",
        b"# This file is licensed under GPL-3.0-only\n",
        b"# License: Apache-2.0\n",
    ],
)
def test_conflicting_notices_are_never_overwritten(repository, notice):
    path = repository / "conflict.py"
    raw = notice + b"x = 1\n"
    path.write_bytes(raw)
    for mode in ("--check", "--fix"):
        result = _run(repository, mode)
        assert result.returncode == 1
        assert "conflict.py" in result.stderr
        assert path.read_bytes() == raw


def test_git_scope_includes_hidden_untracked_and_fixtures(repository):
    ignored = repository / "ignored.py"
    ignored.write_bytes(b"x = 1\n")
    (repository / ".gitignore").write_text("ignored.py\n", encoding="utf-8", newline="\n")
    fixtures = repository / "tests" / "fixtures"
    fixtures.mkdir(parents=True)
    fixture = fixtures / "empty.py"
    fixture.touch()
    _git(repository, "add", "tests/fixtures/empty.py")
    hidden = repository / ".github" / "new.py"
    hidden.touch()
    deleted = repository / "deleted.py"
    deleted.touch()
    _git(repository, "add", "deleted.py")
    deleted.unlink()
    assert _run(repository, "--fix").returncode == 0
    assert fixture.read_bytes() == HEADER
    assert hidden.read_bytes() == HEADER
    assert ignored.read_bytes() == b"x = 1\n"


def test_misplaced_notices_are_rejected_and_embedded_inputs_are_untouched(repository):
    path = repository / "misplaced.py"
    raw = b"x = 1\n" + HEADER
    path.write_bytes(raw)
    for mode in ("--check", "--fix"):
        assert _run(repository, mode).returncode == 1
        assert path.read_bytes() == raw
    embedded = b'input_code = """\n' + HEADER + b'x = 1\n"""\n'
    path.write_bytes(embedded)
    assert _run(repository, "--fix").returncode == 0
    assert path.read_bytes() == HEADER + embedded
    assert _run(repository, "--check").returncode == 0


def test_unrelated_copyright_comment_is_preserved(repository):
    path = repository / "comment.py"
    raw = b"# Verify copyright headers in our tests.\nx = 1\n"
    path.write_bytes(raw)
    assert _run(repository, "--fix").returncode == 0
    assert path.read_bytes() == HEADER + raw


def test_source_symlinks_are_rejected(repository, tmp_path):
    target = tmp_path / "outside.txt"
    target.write_bytes(b"x = 1\n")
    link = repository / "linked.py"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks are unavailable")
    for mode in ("--check", "--fix"):
        result = _run(repository, mode)
        assert result.returncode == 1
        assert "symlink" in result.stderr
        assert target.read_bytes() == b"x = 1\n"


def test_bad_template_and_git_errors_are_operational(repository):
    template = repository / ".license-header.txt"
    original = template.read_bytes()
    template.write_bytes(b"not a comment\n")
    assert _run(repository, "--check").returncode == 2
    template.write_bytes(original)
    shutil.rmtree(repository / ".git")
    assert _run(repository, "--check").returncode == 2


def test_unknown_source_encoding_is_operational(repository):
    path = repository / "bad.py"
    raw = b"# coding: nonexistent-encoding\nx = 1\n"
    path.write_bytes(raw)
    assert _run(repository, "--fix").returncode == 2
    assert path.read_bytes() == raw


def test_shebang_without_a_final_newline(repository):
    path = repository / "script.py"
    path.write_bytes(b"#!/usr/bin/env python")
    assert _run(repository, "--fix").returncode == 0
    expected = b"#!/usr/bin/env python\n" + HEADER
    assert path.read_bytes() == expected
    assert _run(repository, "--check").returncode == 0


def test_notice_before_an_encoding_cookie_is_rejected(repository):
    path = repository / "misplaced.py"
    raw = b"# Copyright (c) 2026 grAItools\n# coding: utf-8\nx = 1\n"
    path.write_bytes(raw)
    for mode in ("--check", "--fix"):
        assert _run(repository, mode).returncode == 1
        assert path.read_bytes() == raw


def test_cli_requires_exactly_one_mode(repository):
    assert _run(repository).returncode == 2
    assert _run(repository, "--check", "--fix").returncode == 2


def test_repository_headers_are_canonical():
    result = _run(ROOT, "--check")
    assert result.returncode == 0, result.stdout + result.stderr


def test_license_metadata_and_readme_agree():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["license"] == "BSD-3-Clause"
    assert project["license-files"] == ["LICENSE"]
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "Copyright (c) 2026 grAItools" in license_text
    assert "Neither the name of the copyright holder" in license_text
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "license-BSD--3--Clause" in readme
    assert "earlier releases remain under MIT" in readme
    contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert HEADER.splitlines()[0].decode() in contributing
    assert "contributions are licensed under the BSD 3-Clause" in contributing
    assert b"# SPDX-License-Identifier: " + project["license"].encode() in HEADER.splitlines()


def test_all_type_checkers_cover_the_header_utility():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]
    for checker, key in (
        ("mypy", "files"),
        ("pyright", "include"),
        ("zuban", "files"),
        ("pyrefly", "project-includes"),
    ):
        assert ".github/scripts/license_headers.py" in config[checker][key]
    assert config["pyrefly"]["disable-project-excludes-heuristics"] is True


def test_header_hook_checks_the_whole_tree_even_on_template_only_changes():
    # PyYAML is a dev dependency, independent of libcst's optional YAML backend.
    code = (
        "import json, sys, yaml\n"
        "with open(sys.argv[1], encoding='utf-8') as f:\n"
        "    json.dump(yaml.safe_load(f), sys.stdout)\n"
    )
    import json

    result = subprocess.run(
        [sys.executable, "-c", code, str(ROOT / ".pre-commit-config.yaml")],
        capture_output=True,
        text=True,
        check=True,
    )
    config = json.loads(result.stdout)
    [hook] = [
        hook
        for repo in config["repos"]
        for hook in repo["hooks"]
        if hook["id"] == "license-headers"
    ]
    assert hook["entry"] == "uv run python .github/scripts/license_headers.py --check"
    assert hook["language"] == "system"
    assert hook["always_run"] is True
    assert hook["pass_filenames"] is False

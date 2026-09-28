"""End-to-end CLI behaviour and exit codes."""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys

import pytest

from cleanporter import cli


@pytest.fixture
def project(tmp_path: pathlib.Path) -> pathlib.Path:
    (tmp_path / "src" / "demo").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0"\n', encoding="utf-8"
    )
    (tmp_path / "src" / "demo" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "demo" / "helpers.py").write_text("THING = 42\n", encoding="utf-8")
    (tmp_path / "src" / "demo" / "consumer.py").write_text(
        "from demo.helpers import THING\ntotal = THING\n", encoding="utf-8"
    )
    return tmp_path


def test_check_reports_and_exits_1(project, capsys):
    rc = cli.main([str(project / "src")])
    out = capsys.readouterr().out
    assert "consumer.py" in out and "CP001" in out
    assert rc == 1


def test_clean_tree_exits_0(project, capsys):
    (project / "src" / "demo" / "consumer.py").write_text(
        "from demo import helpers\ntotal = helpers.THING\n", encoding="utf-8"
    )
    assert cli.main([str(project / "src")]) == 0


def test_fix_rewrites_and_exits_0(project, capsys):
    rc = cli.main(["--fix", str(project / "src")])
    assert rc == 0
    assert (project / "src" / "demo" / "consumer.py").read_text(encoding="utf-8") == (
        "from demo import helpers\ntotal = helpers.THING\n"
    )
    # progress and findings go to stderr while a patch is on stdout
    assert "fixed" in capsys.readouterr().err


def test_diff_previews_without_writing(project, capsys):
    before = (project / "src" / "demo" / "consumer.py").read_text(encoding="utf-8")
    rc = cli.main(["--diff", str(project / "src")])
    out = capsys.readouterr().out
    assert "-from demo.helpers import THING" in out
    assert (project / "src" / "demo" / "consumer.py").read_text(encoding="utf-8") == before
    assert rc == 1


def test_typing_imports_are_exempt(project, capsys):
    (project / "src" / "demo" / "consumer.py").write_text(
        "from typing import Any\nfrom collections.abc import Mapping\n"
        "x: Any = None\ny: Mapping = {}\n",
        encoding="utf-8",
    )
    assert cli.main([str(project / "src")]) == 0


def test_exempt_flag_extends_the_allowlist(project, capsys):
    (project / "src" / "demo" / "consumer.py").write_text(
        "from demo.helpers import THING\n", encoding="utf-8"
    )
    assert cli.main(["--exempt", "demo.helpers", str(project / "src")]) == 0


def test_exclude_config_is_respected(project, capsys):
    (project / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0"\n'
        '[tool.cleanporter]\nexclude = ["**/consumer.py"]\n',
        encoding="utf-8",
    )
    assert cli.main([str(project / "src")]) == 0
    assert "consumer.py" not in capsys.readouterr().out


def test_syntax_error_exits_2(project, capsys):
    (project / "src" / "demo" / "broken.py").write_text("def (:\n", encoding="utf-8")
    assert cli.main([str(project / "src")]) == 2


def test_bad_config_exits_2(project, capsys):
    (project / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0"\n[tool.cleanporter]\nscope = "nonsense"\n',
        encoding="utf-8",
    )
    assert cli.main([str(project / "src")]) == 2
    assert "configuration error" in capsys.readouterr().err


def test_scope_first_party_check_and_fix_agree(project, capsys):
    """B1: ``check`` was silent on a stdlib import that ``--diff``/``--fix`` rewrote."""
    (project / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0"\n[tool.cleanporter]\nscope = "first-party"\n',
        encoding="utf-8",
    )
    target = project / "src" / "demo" / "m.py"
    source = 'from os.path import join\nprint(join("a", "b"))\n'
    target.write_text(source, encoding="utf-8")
    (project / "src" / "demo" / "consumer.py").write_text(
        "from demo import helpers\ntotal = helpers.THING\n", encoding="utf-8"
    )
    src = str(project / "src")
    assert cli.main([src]) == 0
    assert "CP001" not in capsys.readouterr().out
    assert cli.main(["--diff", src]) == 0
    assert "os.path" not in capsys.readouterr().out
    assert cli.main(["--fix", src]) == 0
    assert target.read_text(encoding="utf-8") == source


def test_missing_path_warns_and_exits_0(project, capsys):
    rc = cli.main([str(project / "nope")])
    captured = capsys.readouterr()
    assert "does not exist" in captured.out + captured.err
    assert rc == 0


def test_strict_promotes_unresolved_to_failure(project, capsys):
    (project / "src" / "demo" / "consumer.py").write_text(
        "from definitely_missing_pkg_xyz import thing\n", encoding="utf-8"
    )
    assert cli.main([str(project / "src")]) == 0
    assert cli.main(["--strict", str(project / "src")]) == 1


def test_fix_still_reports_violations_it_declined(project, capsys):
    (project / "src" / "demo" / "consumer.py").write_text(
        # `THING` is read, so it is a name the fixer would rewrite and the
        # `__all__` guard has something to block on. Without the read it would
        # be kept as never-read and the guard would never be reached.
        'from demo.helpers import THING\n__all__ = ["THING"]\nx = THING\n',
        encoding="utf-8",
    )
    rc = cli.main(["--fix", str(project / "src")])
    err = capsys.readouterr().err
    assert "CP003" in err, "the blocker must be explained"
    assert "CP001" in err, "the unfixed violation must still be reported"
    assert rc == 1


def test_fix_reports_nothing_for_a_fully_fixed_file(project, capsys):
    rc = cli.main(["--fix", str(project / "src")])
    out = capsys.readouterr().out
    assert "CP001" not in out
    assert rc == 0


def test_summary_counts_match_the_printed_lines(project, capsys):
    (project / "src" / "demo" / "consumer.py").write_text(
        "from demo.helpers import THING\nfrom definitely_missing_pkg_xyz import other\n",
        encoding="utf-8",
    )
    cli.main([str(project / "src")])
    out = capsys.readouterr().out
    assert out.count("CP001") == 1
    assert out.count("CP002") == 1
    assert "1 violation(s)" in out
    assert "1 unresolved" in out


def test_unanchorable_relative_import_is_counted(project, capsys):
    (project / "src" / "demo" / "consumer.py").write_text(
        "from ..... import nothing\n", encoding="utf-8"
    )
    cli.main([str(project / "src")])
    out = capsys.readouterr().out
    assert "CP002" in out
    assert "0 unresolved" not in out


def test_strict_exits_1_for_unanchorable_relative_import(project, capsys):
    (project / "src" / "demo" / "consumer.py").write_text(
        "from ..... import nothing\n", encoding="utf-8"
    )
    assert cli.main(["--strict", str(project / "src")]) == 1


def test_non_utf8_source_exits_2(project, capsys):
    (project / "src" / "demo" / "consumer.py").write_bytes(b"\xff\xfe# not utf-8\n")
    assert cli.main([str(project / "src")]) == 2


def test_internal_rewrite_error_does_not_write_a_broken_file(project, monkeypatch):
    from cleanporter import model, rewrite

    target = project / "src" / "demo" / "consumer.py"
    before = target.read_text(encoding="utf-8")

    def fake(rec, resolver, config):
        return rewrite.FixOutcome(
            "error",
            rec.source,
            [model.Finding(rec.path, 1, 0, "?", "?", model.Status.SKIPPED, "internal error")],
        )

    # cli calls `rewrite.fix_record`, resolving the attribute at call time,
    # so patch it on the module that owns it.
    monkeypatch.setattr("cleanporter.rewrite.fix_record", fake)
    assert cli.main(["--fix", str(project / "src")]) == 1
    assert target.read_text(encoding="utf-8") == before


# -- src layout, no path arguments (final review, Critical 1) ---------------


@pytest.fixture
def src_layout(tmp_path: pathlib.Path) -> pathlib.Path:
    """A src-layout project with a `tests/` package, as most repos have."""
    (tmp_path / "src" / "mypkg").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "mypkg"\nversion = "0"\n', encoding="utf-8"
    )
    (tmp_path / "src" / "mypkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "mypkg" / "helpers.py").write_text(
        "class Widget:\n    pass\n", encoding="utf-8"
    )
    (tmp_path / "src" / "mypkg" / "consumer.py").write_text(
        "from .helpers import Widget\nw = Widget()\n", encoding="utf-8"
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "__init__.py").write_text("", encoding="utf-8")
    return tmp_path


def _imports_cleanly(project: pathlib.Path) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, PYTHONPATH=str(project / "src"))
    return subprocess.run(
        [sys.executable, "-c", "import mypkg.consumer"],
        # cwd is *inside* src, so only the real import root is on sys.path --
        # running from the project root would make a bogus `src.` prefix
        # resolve as a namespace package and hide the bug.
        capture_output=True,
        text=True,
        env=env,
        cwd=project / "src",
    )


def test_fix_with_no_path_arguments_keeps_the_package_importable(src_layout, monkeypatch, capsys):
    monkeypatch.chdir(src_layout)
    assert _imports_cleanly(src_layout).returncode == 0, "fixture must import before --fix"

    cli.main(["--fix"])
    capsys.readouterr()

    proc = _imports_cleanly(src_layout)
    assert proc.returncode == 0, proc.stderr
    after = (src_layout / "src" / "mypkg" / "consumer.py").read_text(encoding="utf-8")
    assert after == "from mypkg import helpers\nw = helpers.Widget()\n"


def test_check_on_a_src_layout_names_the_module_without_the_src_prefix(
    src_layout, monkeypatch, capsys
):
    monkeypatch.chdir(src_layout)
    cli.main([])
    # One readouterr() call drains the buffers; a second always returns
    # empty, so both streams must come from the same capture.
    captured = capsys.readouterr()
    assert "src.mypkg" not in captured.out + captured.err


# -- output streams (final review, Important 5) -----------------------------


def test_diff_stdout_carries_only_the_patch(project, monkeypatch, capsys):
    monkeypatch.chdir(project)
    cli.main(["--diff", "src"])
    captured = capsys.readouterr()
    assert captured.out.startswith("--- a/src/demo/consumer.py\n")
    assert "a//" not in captured.out
    assert "CP001" not in captured.out and "checked" not in captured.out
    for line in captured.out.splitlines():
        assert line[:1] in {"-", "+", "@", " "}, line
    assert "CP001" in captured.err
    assert "checked 3 file(s)" in captured.err


def test_diff_headers_are_relative_even_for_an_absolute_path_argument(project, monkeypatch, capsys):
    monkeypatch.chdir(project)
    cli.main(["--diff", str(project / "src")])
    out = capsys.readouterr().out
    assert "--- a/src/demo/consumer.py" in out
    assert "a//" not in out


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_the_diff_can_be_applied_with_git_apply(project, monkeypatch, capsys):
    monkeypatch.chdir(project)
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    cli.main(["--diff", "src"])
    patch = capsys.readouterr().out
    proc = subprocess.run(
        ["git", "apply", "--check", "-"],
        input=patch,
        text=True,
        capture_output=True,
        cwd=project,
    )
    assert proc.returncode == 0, proc.stderr


def test_warnings_go_to_stderr_when_a_patch_is_on_stdout(project, monkeypatch, capsys):
    monkeypatch.chdir(project)
    cli.main(["--diff", "src", "nope"])
    captured = capsys.readouterr()
    assert "path does not exist" in captured.err
    assert "path does not exist" not in captured.out


def test_check_mode_still_reports_on_stdout(project, monkeypatch, capsys):
    monkeypatch.chdir(project)
    cli.main(["src"])
    captured = capsys.readouterr()
    assert "CP001" in captured.out
    assert "checked 3 file(s)" in captured.out


# -- PEP 420 namespace packages (re-review blocker) --------------------------


def _runs(
    project: pathlib.Path, module: str, root: pathlib.Path
) -> subprocess.CompletedProcess[str]:
    """Import *module* with only *root* on sys.path, from outside the tree."""
    return subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True,
        cwd=project,
        env=dict(os.environ, PYTHONPATH=str(root)),
    )


@pytest.fixture
def declared_namespace(tmp_path: pathlib.Path) -> pathlib.Path:
    """A src layout whose package is a namespace package: `--root src` is the
    only thing that says where the import root is."""
    (tmp_path / "src" / "mypkg").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "mypkg"\nversion = "0"\n', encoding="utf-8"
    )
    (tmp_path / "src" / "mypkg" / "other.py").write_text(
        "class Thing:\n    pass\n", encoding="utf-8"
    )
    (tmp_path / "src" / "mypkg" / "mod.py").write_text(
        "from .other import Thing\nt = Thing()\n", encoding="utf-8"
    )
    return tmp_path


def test_an_explicit_root_beats_a_namespace_package_inferred_below_it(
    declared_namespace, monkeypatch, capsys
):
    project = declared_namespace
    monkeypatch.chdir(project)
    assert _runs(project, "mypkg.mod", project / "src").returncode == 0, "must import first"

    cli.main(["--fix", "--root", "src", "."])
    capsys.readouterr()

    # Not `import other`, which compiles and then raises ModuleNotFoundError.
    assert (project / "src" / "mypkg" / "mod.py").read_text(encoding="utf-8") == (
        "from mypkg import other\nt = other.Thing()\n"
    )
    proc = _runs(project, "mypkg.mod", project / "src")
    assert proc.returncode == 0, proc.stderr


def test_a_flat_namespace_package_stays_importable_after_fix(tmp_path, monkeypatch, capsys):
    (tmp_path / "mypkg").mkdir()
    (tmp_path / "mypkg" / "helpers.py").write_text("class Widget:\n    pass\n", encoding="utf-8")
    (tmp_path / "mypkg" / "consumer.py").write_text(
        "from .helpers import Widget\nw = Widget()\n", encoding="utf-8"
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    cli.main(["--fix", "."])
    capsys.readouterr()

    assert (tmp_path / "mypkg" / "consumer.py").read_text(encoding="utf-8") == (
        "from mypkg import helpers\nw = helpers.Widget()\n"
    )
    proc = _runs(tmp_path, "mypkg.consumer", tmp_path)
    assert proc.returncode == 0, proc.stderr


def test_a_namespace_subpackage_reuses_its_existing_relative_import(tmp_path, monkeypatch, capsys):
    (tmp_path / "pkg" / "sub").mkdir(parents=True)
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "sub" / "other.py").write_text("class Thing:\n    pass\n", encoding="utf-8")
    (tmp_path / "pkg" / "sub" / "mod.py").write_text(
        "from . import other\nfrom .other import Thing\nt = Thing()\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)

    cli.main(["--fix", "."])
    captured = capsys.readouterr()

    # The existing binding is reused: no `other_2` alias, and no bogus CP002.
    assert (tmp_path / "pkg" / "sub" / "mod.py").read_text(encoding="utf-8") == (
        "from . import other\nt = other.Thing()\n"
    )
    assert "CP002" not in captured.out + captured.err
    proc = _runs(tmp_path, "pkg.sub.mod", tmp_path)
    assert proc.returncode == 0, proc.stderr


# -- a module and a package that share a name -------------------------------


@pytest.fixture
def shadowed_package(tmp_path: pathlib.Path) -> pathlib.Path:
    """``pkg/`` re-exporting ``helper``, with a stale flat ``pkg.py`` beside it.

    What an older single-file release looks like when it is left in place next
    to a newer packaged one. The corpus ships exactly this: ``click_plugins.py``
    (2.0dev) beside ``click_plugins/`` (1.1.1.2).
    """
    (tmp_path / "pkg.py").write_text('def helper():\n    return "flat"\n', encoding="utf-8")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("from pkg.core import helper\n", encoding="utf-8")
    (tmp_path / "pkg" / "core.py").write_text(
        'def helper():\n    return "packaged"\n', encoding="utf-8"
    )
    (tmp_path / "consumer.py").write_text(
        "from pkg import helper\n\nVALUE = helper()\n", encoding="utf-8"
    )
    return tmp_path


def _consumer_value(project: pathlib.Path) -> subprocess.CompletedProcess[str]:
    """Import ``consumer`` and print what it got, so *which* ``pkg`` won shows."""
    return subprocess.run(
        [sys.executable, "-c", "import consumer; print(consumer.VALUE)"],
        capture_output=True,
        text=True,
        cwd=project,
        env=dict(os.environ, PYTHONPATH=str(project)),
    )


def test_fix_keeps_a_reexport_that_a_module_of_the_same_name_hides(
    shadowed_package, monkeypatch, capsys
):
    """The flat ``pkg.py`` must not decide what ``pkg``'s public surface is.

    Python resolves ``import pkg`` to the package and never looks at
    ``pkg.py``, but the module map kept a single source file per dotted name
    and the flat module was scanned last, so the re-export guard read
    ``pkg.py``, saw no re-export and stood down. ``--fix`` then deleted
    ``pkg.helper`` *and* rewrote ``consumer.py`` to read it, producing an
    ``AttributeError`` at import. In the corpus this broke
    ``celery.bin.celery``, which imports ``with_plugins`` from
    ``click_plugins``.
    """
    project = shadowed_package
    before = _consumer_value(project)
    assert before.stdout.strip() == "packaged", before.stderr

    monkeypatch.chdir(project)
    cli.main(["--fix", "."])
    capsys.readouterr()

    assert (project / "pkg" / "__init__.py").read_text(encoding="utf-8") == (
        "from pkg.core import helper\n"
    ), "the re-export the consumer reads must survive byte-identical"
    assert (project / "consumer.py").read_text(encoding="utf-8") == (
        "import pkg\n\nVALUE = pkg.helper()\n"
    )
    after = _consumer_value(project)
    assert after.returncode == 0, after.stderr
    assert after.stdout.strip() == "packaged"


# -- the cross-file limitation note -----------------------------------------


_NOTE = "cleanporter: note: --fix cannot see dotted references from other files"


def test_fix_notes_the_cross_file_limitation_on_stderr(project, monkeypatch, capsys):
    monkeypatch.chdir(project)
    cli.main(["--fix", "src"])
    captured = capsys.readouterr()
    assert _NOTE in captured.err
    assert "re-run your tests" in captured.err
    # stdout still carries only the patch.
    assert "note:" not in captured.out


def test_no_note_when_fix_writes_nothing(project, monkeypatch, capsys):
    (project / "src" / "demo" / "consumer.py").write_text(
        "from demo import helpers\ntotal = helpers.THING\n", encoding="utf-8"
    )
    monkeypatch.chdir(project)
    cli.main(["--fix", "src"])
    assert _NOTE not in capsys.readouterr().err


def test_no_note_for_a_preview_that_writes_nothing(project, monkeypatch, capsys):
    monkeypatch.chdir(project)
    cli.main(["--diff", "src"])
    captured = capsys.readouterr()
    assert _NOTE not in captured.err + captured.out


def test_a_namespace_package_holding_a_subpackage_is_not_rewritten_to_stdlib(
    tmp_path, monkeypatch, capsys
):
    """`analytics/io/__init__.py` must not be qualified as `io`: rewriting
    `from .readers import read` to `from io import readers` reaches the
    standard library, and the file still imports, so nothing catches it."""
    (tmp_path / "analytics" / "io").mkdir(parents=True)
    (tmp_path / "analytics" / "io" / "readers.py").write_text(
        "def read():\n    return []\n", encoding="utf-8"
    )
    (tmp_path / "analytics" / "io" / "__init__.py").write_text(
        "from .readers import read\n\nvalues = read()\n", encoding="utf-8"
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "tests" / "test_it.py").write_text(
        "from analytics.io import values\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)

    cli.main(["--fix", "."])
    capsys.readouterr()

    assert (tmp_path / "analytics" / "io" / "__init__.py").read_text(encoding="utf-8") == (
        "from analytics.io import readers\n\nvalues = readers.read()\n"
    )
    proc = _runs(tmp_path, "analytics.io", tmp_path)
    assert proc.returncode == 0, proc.stderr


# -- a name that is also one of the package's own submodules ----------------
#
# Inside `pkg/__init__.py` a module-level name *is* the attribute
# `pkg.<name>`, which makes two things unsafe there that are fine anywhere
# else. Every test in this section asserts on what the rewritten package
# *evaluates to*, never on its text alone: the character of the bug is that
# the output looks reasonable, imports without error, and is wrong.


def _package_values(project: pathlib.Path) -> subprocess.CompletedProcess[str]:
    """Report what `pkg` bound, at both of the timings that can differ.

    `VALUE` is read straight after `import pkg`, and `use()` is called after
    `import pkg.serialization`. That second import is the whole point:
    importing a submodule sets it as an attribute of its parent, so a global
    sitting in that attribute's slot is silently replaced right there -- long
    after the rewrite, and in a file that need not be the rewritten one.
    """
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "import pkg; before = pkg.VALUE\nimport pkg.serialization\nprint(before, pkg.use())",
        ],
        capture_output=True,
        text=True,
        cwd=project,
        env=dict(os.environ, PYTHONPATH=str(project)),
    )


def _two_serializations(tmp_path: pathlib.Path, init: str) -> pathlib.Path:
    """`pkg` and `kombu` each holding a `serialization` submodule."""
    (tmp_path / "kombu").mkdir()
    (tmp_path / "kombu" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "kombu" / "serialization.py").write_text(
        'MARK = "kombu"\n\n\ndef loads(x):\n    return x\n', encoding="utf-8"
    )
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "serialization.py").write_text('MARK = "pkg"\n', encoding="utf-8")
    (tmp_path / "pkg" / "__init__.py").write_text(init, encoding="utf-8")
    return tmp_path


def test_fix_does_not_bind_another_module_over_a_submodules_name(tmp_path, monkeypatch, capsys):
    """A new global in `pkg/__init__.py` must not take `pkg.serialization`'s slot.

    Rewriting `from kombu.serialization import loads` to `from kombu import
    serialization` puts *kombu's* module in the attribute belonging to
    `pkg.serialization`. The next line's `from pkg import serialization` then
    reads that attribute instead of importing the submodule -- `from X import
    Y` falls back to importing the submodule only when `X` has no attribute
    `Y` -- so it silently binds the wrong module. Nothing raises. Found in the
    corpus as `celery/security/__init__.py`, where the name meant for
    `celery.security.serialization` became `kombu.serialization`.
    """
    project = _two_serializations(
        tmp_path,
        "from kombu.serialization import loads\n"
        "from pkg.serialization import MARK\n"
        "\n"
        "VALUE = MARK\n"
        "\n"
        "\n"
        "def use():\n"
        "    return loads(1)\n",
    )
    before = _package_values(project)
    assert before.stdout.split() == ["pkg", "1"], before.stderr

    monkeypatch.chdir(project)
    cli.main(["--fix", "."])
    capsys.readouterr()

    after = _package_values(project)
    assert after.returncode == 0, after.stderr
    assert after.stdout.split() == ["pkg", "1"], (
        f"the rewrite changed what the package evaluates to:\n"
        f"{(project / 'pkg' / '__init__.py').read_text(encoding='utf-8')}"
    )


def test_fix_does_not_qualify_through_a_binding_already_in_a_submodules_slot(
    tmp_path, monkeypatch, capsys
):
    """Reusing a binding is subject to the same rule as allocating one.

    The author's `from kombu import serialization` already sits in
    `pkg.serialization`'s slot, and that was harmless only while nothing
    depended on it. Qualifying `loads` through it is what would make it
    load-bearing -- and the first `import pkg.serialization` anywhere
    replaces it, after which `serialization.loads` raises. A fresh alias is
    bound instead, and the author's own import is left untouched.
    """
    project = _two_serializations(
        tmp_path,
        "from kombu import serialization\n"
        "from kombu.serialization import loads\n"
        "\n"
        'VALUE = "pkg"\n'
        "\n"
        "\n"
        "def use():\n"
        "    return loads(1)\n",
    )
    before = _package_values(project)
    assert before.stdout.split() == ["pkg", "1"], before.stderr

    monkeypatch.chdir(project)
    cli.main(["--fix", "."])
    capsys.readouterr()

    after = _package_values(project)
    assert after.returncode == 0, after.stderr
    assert after.stdout.split() == ["pkg", "1"], (
        f"the rewrite leaned on a binding the import system overwrites:\n"
        f"{(project / 'pkg' / '__init__.py').read_text(encoding='utf-8')}"
    )


def test_a_self_referential_import_is_still_rewritten_when_nothing_shadows_it(
    tmp_path, monkeypatch, capsys
):
    """The decline must be about the shadowing, not about self-reference.

    With no competing binding, `from pkg import serialization` inside
    `pkg/__init__.py` finds no such attribute, imports the submodule, and is
    exactly right -- and it needs no alias either, because the name it binds
    is the same object the import system puts in that attribute anyway.
    """
    project = _two_serializations(
        tmp_path,
        "from pkg.serialization import MARK\n\nVALUE = MARK\n\n\ndef use():\n    return 1\n",
    )
    monkeypatch.chdir(project)
    cli.main(["--fix", "."])
    capsys.readouterr()

    assert (
        (project / "pkg" / "__init__.py")
        .read_text(encoding="utf-8")
        .startswith("from pkg import serialization\n")
    ), "a self-referential import nothing shadows is fixed, and without an alias"
    after = _package_values(project)
    assert after.returncode == 0, after.stderr
    assert after.stdout.split() == ["pkg", "1"]


def test_a_binding_the_author_wrote_over_a_submodule_name_is_declined(
    tmp_path, monkeypatch, capsys
):
    """When the shadowing name is the author's, no alias can help.

    The binding has to stay, so `from pkg import serialization` would keep
    reading it. That one import is reported `CP003` and kept byte-identical.
    """
    project = _two_serializations(
        tmp_path,
        "serialization = 42\n"
        "from pkg.serialization import MARK\n"
        "\n"
        "VALUE = MARK\n"
        "\n"
        "\n"
        "def use():\n"
        "    return 1\n",
    )
    monkeypatch.chdir(project)
    cli.main(["--fix", "."])
    captured = capsys.readouterr()

    assert "CP003" in captured.err
    assert "both a submodule of 'pkg' and bound in its __init__" in captured.err
    assert "from pkg.serialization import MARK" in (project / "pkg" / "__init__.py").read_text(
        encoding="utf-8"
    )
    after = _package_values(project)
    assert after.returncode == 0, after.stderr
    assert after.stdout.split() == ["pkg", "1"]


def _lazy_reexport_project(tmp_path: pathlib.Path) -> pathlib.Path:
    """`pkg.sub` re-exports a function under its own submodule's name.

    The lazy re-export idiom, and the shape gt4py's
    `iterator/transforms/concat_where` has: `pkg.sub.mod` is a module on
    disk, and `pkg.sub.mod` the *attribute* is a function.
    """
    (tmp_path / "pkg" / "sub").mkdir(parents=True)
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "sub" / "__init__.py").write_text(
        "from pkg.sub.mod import mod\n", encoding="utf-8"
    )
    (tmp_path / "pkg" / "sub" / "mod.py").write_text(
        "OTHER = 2\n\n\ndef mod():\n    return 1\n", encoding="utf-8"
    )
    (tmp_path / "pkg" / "consumer.py").write_text(
        "from pkg.sub.mod import OTHER\n\n\ndef value():\n    return OTHER\n",
        encoding="utf-8",
    )
    return tmp_path


def _reexport_consumer_value(project: pathlib.Path) -> subprocess.CompletedProcess[str]:
    """Import the consumer *through* its package and print what it computed."""
    return subprocess.run(
        [sys.executable, "-c", "import pkg.consumer; print(pkg.consumer.value())"],
        capture_output=True,
        text=True,
        cwd=project,
        env=dict(os.environ, PYTHONPATH=str(project)),
    )


def test_fix_declines_an_import_whose_replacement_the_parent_package_shadows(
    tmp_path, monkeypatch, capsys
):
    """`from pkg.sub import mod` would bind the *function* `pkg.sub.mod`.

    The rewritten `mod.OTHER` then raises `AttributeError`: source that
    imports, parses and re-parses, and does not run. Reported on gt4py,
    where `concat_where/__init__.py` re-exports
    `transform_to_as_fieldop` under its own module's name.
    """
    project = _lazy_reexport_project(tmp_path)
    (project / "pkg" / "plain.py").write_text("EXTRA = 3\n", encoding="utf-8")
    (project / "pkg" / "consumer.py").write_text(
        "from pkg.plain import EXTRA\n"
        "from pkg.sub.mod import OTHER\n"
        "\n"
        "\n"
        "def value():\n"
        "    return OTHER + EXTRA\n",
        encoding="utf-8",
    )
    before = _reexport_consumer_value(project)
    assert before.stdout.split() == ["5"], before.stderr

    monkeypatch.chdir(project)
    cli.main(["--fix", "."])
    captured = capsys.readouterr()

    assert "CP003" in captured.err
    assert "the replacement 'from pkg.sub import mod'" in captured.err
    # Kept per import, not per file: the sound rewrite on the line above still
    # happens, and only the import that cannot be reached is left alone.
    assert (project / "pkg" / "consumer.py").read_text(encoding="utf-8") == (
        "from pkg import plain\n"
        "from pkg.sub.mod import OTHER\n"
        "\n"
        "\n"
        "def value():\n"
        "    return OTHER + plain.EXTRA\n"
    )
    after = _reexport_consumer_value(project)
    assert after.returncode == 0, after.stderr
    assert after.stdout.split() == ["5"]


def test_fix_still_rewrites_when_the_package_imports_its_own_submodule(
    tmp_path, monkeypatch, capsys
):
    """`from . import mod` binds the module, so the replacement reaches it."""
    project = _lazy_reexport_project(tmp_path)
    (project / "pkg" / "sub" / "__init__.py").write_text("from . import mod\n", encoding="utf-8")

    monkeypatch.chdir(project)
    cli.main(["--fix", "."])
    capsys.readouterr()

    assert (project / "pkg" / "consumer.py").read_text(encoding="utf-8") == (
        "from pkg.sub import mod\n\n\ndef value():\n    return mod.OTHER\n"
    )
    after = _reexport_consumer_value(project)
    assert after.returncode == 0, after.stderr
    assert after.stdout.split() == ["2"]


def test_fix_aliases_a_top_level_import_that_collides_with_a_submodule(
    tmp_path, monkeypatch, capsys
):
    """The undotted `import json` branch is subject to the same rule.

    A package with its own `json.py` gets `import json as json_2`; plain
    `import json` would put the stdlib module in `pkg.json`'s slot, and the
    first `import pkg.json` then replaces it, leaving `json.dumps` an
    `AttributeError` inside this very file.
    """
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "json.py").write_text('MARK = "pkg-json"\n', encoding="utf-8")
    (tmp_path / "pkg" / "__init__.py").write_text(
        "from json import dumps\n\nVALUE = dumps([1])\n\n\ndef use():\n    return dumps([2])\n",
        encoding="utf-8",
    )
    probe = [
        sys.executable,
        "-c",
        "import pkg; before = pkg.VALUE\nimport pkg.json\nprint(before, pkg.use())",
    ]
    env = dict(os.environ, PYTHONPATH=str(tmp_path))
    before = subprocess.run(probe, capture_output=True, text=True, cwd=tmp_path, env=env)
    assert before.stdout.split() == ["[1]", "[2]"], before.stderr

    monkeypatch.chdir(tmp_path)
    cli.main(["--fix", "."])
    capsys.readouterr()

    after = subprocess.run(probe, capture_output=True, text=True, cwd=tmp_path, env=env)
    assert after.returncode == 0, after.stderr
    assert after.stdout.split() == ["[1]", "[2]"], (
        f"the stdlib module was bound over `pkg.json`:\n"
        f"{(tmp_path / 'pkg' / '__init__.py').read_text(encoding='utf-8')}"
    )


# -- [tool.cleanporter.skip] -------------------------------------------------


def _with_skip(project: pathlib.Path, rule: str) -> None:
    (project / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0"\n[tool.cleanporter]\nskip = [' + rule + "]\n",
        encoding="utf-8",
    )


def test_a_skipped_violation_does_not_fail_the_run(project, capsys):
    _with_skip(project, "{ file = '.*consumer[.]py' }")
    assert cli.main([str(project / "src")]) == 0


def test_a_skipped_violation_does_not_fail_under_strict_either(project, capsys):
    """The file must hold an unresolvable import, or `--strict` has no work.

    `CP002` is what `--strict` promotes to a failure, so a fixture where
    everything resolves would pass this test with the rule doing nothing.
    """
    (project / "src" / "demo" / "consumer.py").write_text(
        "from demo.helpers import THING\nfrom definitely_missing_pkg_xyz import other\n"
        "total = THING + other\n",
        encoding="utf-8",
    )
    assert cli.main(["--strict", str(project / "src")]) == 1, "CP002 fails under --strict"
    _with_skip(project, "{ file = '.*consumer[.]py' }")
    assert cli.main(["--strict", str(project / "src")]) == 0


def test_skipped_findings_are_counted_but_not_printed(project, capsys):
    _with_skip(project, "{ file = '.*consumer[.]py' }")
    cli.main([str(project / "src")])
    out = capsys.readouterr().out
    assert "CP004" not in out
    assert "1 skipped by config" in out


def test_show_skipped_prints_them(project, capsys):
    _with_skip(project, "{ file = '.*consumer[.]py', reason = 'not ours' }")
    cli.main(["--show-skipped", str(project / "src")])
    out = capsys.readouterr().out
    assert "CP004" in out
    assert "skip rule #1 (file='.*consumer[.]py'): not ours" in out


def test_fix_leaves_a_skipped_file_byte_identical(project, capsys):
    _with_skip(project, "{ file = '.*consumer[.]py' }")
    target = project / "src" / "demo" / "consumer.py"
    before = target.read_text(encoding="utf-8")
    assert cli.main(["--fix", str(project / "src")]) == 0
    assert target.read_text(encoding="utf-8") == before


def test_fix_explains_an_import_nothing_reads(project, capsys):
    (project / "src" / "demo" / "consumer.py").write_text(
        "from demo.helpers import THING\ndef test_it(THING):\n    return THING\n",
        encoding="utf-8",
    )
    rc = cli.main(["--fix", str(project / "src")])
    err = capsys.readouterr().err
    assert "CP003" in err
    assert "never read in this file" in err
    assert rc == 1


def test_a_rule_matching_the_rewritten_spelling_can_fail_a_run(project, capsys):
    """The one way a `skip` rule *can* change an exit code, now documented.

    `CP004` never counts, but a rule that matches what `--fix` is about to
    write rather than what is in the source declines the file with a `CP003`,
    and that does. A user adding a rule to quieten CI needs to know this can
    go the other way.
    """
    (project / "src" / "demo" / "helpers.py").write_text(
        "THING = 42\n\n\ndef deco(fn):\n    return fn\n", encoding="utf-8"
    )
    (project / "src" / "demo" / "consumer.py").write_text(
        "from demo.helpers import THING, deco\n\n\n@deco\ndef go():\n    return THING\n",
        encoding="utf-8",
    )
    assert cli.main(["--fix", str(project / "src")]) == 0

    (project / "src" / "demo" / "consumer.py").write_text(
        "from demo.helpers import THING, deco\n\n\n@deco\ndef go():\n    return THING\n",
        encoding="utf-8",
    )
    _with_skip(project, "{ decorator = 'helpers[.]deco' }")
    assert cli.main(["--fix", str(project / "src")]) == 1
    err = capsys.readouterr().err
    assert "then covers" in err


# -- third-party packages that print on import (B5) --------------------------


def _noisy_package(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A third-party ``cli_noisy_pkg`` whose ``__init__`` prints a banner."""
    site = tmp_path / "site"
    (site / "cli_noisy_pkg").mkdir(parents=True)
    (site / "cli_noisy_pkg" / "__init__.py").write_text(
        "print('Welcome to noisy 1.0!')\n", encoding="utf-8"
    )
    (site / "cli_noisy_pkg" / "leaf.py").write_text("X = 1\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(site))
    for dotted in ("cli_noisy_pkg", "cli_noisy_pkg.leaf"):
        monkeypatch.delitem(sys.modules, dotted, raising=False)


def test_a_package_that_prints_on_import_stays_out_of_the_patch(project, monkeypatch, capsys):
    _noisy_package(project, monkeypatch)
    (project / "src" / "demo" / "noisy_user.py").write_text(
        "from cli_noisy_pkg import leaf\nvalue = leaf.X\n", encoding="utf-8"
    )
    monkeypatch.chdir(project)
    cli.main(["--diff", "src"])
    captured = capsys.readouterr()
    assert "Welcome to noisy" in captured.err  # the package really was probed
    assert captured.out.startswith("--- a/src/demo/consumer.py\n")
    for line in captured.out.splitlines():
        assert line[:1] in {"-", "+", "@", " "}, line


@pytest.mark.skipif(os.name != "posix", reason="uses a /bin/sh stub interpreter")
def test_a_failed_probe_is_explained_in_a_warning(project, monkeypatch, capsys):
    stub = project / "broken-python"
    stub.write_text("#!/bin/sh\necho 'ImportError: no encodings' >&2\nexit 1\n", encoding="utf-8")
    stub.chmod(0o755)
    (project / "src" / "demo" / "consumer.py").write_text(
        "from functools import partial\nf = partial(print)\n", encoding="utf-8"
    )
    monkeypatch.chdir(project)
    rc = cli.main(["--python", str(stub), "src"])
    out = capsys.readouterr().out
    assert "cleanporter: warning: interpreter probe" in out
    assert "exited with status 1" in out
    assert "ImportError: no encodings" in out
    assert "CP002" in out
    assert rc == 0


def _broken_interpreter(path: pathlib.Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\necho 'stub interpreter ran' >&2\nexit 1\n", encoding="utf-8")
    path.chmod(0o755)


@pytest.mark.skipif(os.name != "posix", reason="uses a /bin/sh stub interpreter")
def test_a_relative_python_in_config_is_found_from_a_subdirectory(project, monkeypatch, capsys):
    """``python = "tools/py"`` means next to pyproject.toml, wherever the run starts.

    The stub fails on purpose so the warning proves which file was executed.
    """
    _broken_interpreter(project / "tools" / "py")
    with (project / "pyproject.toml").open("a", encoding="utf-8") as fh:
        fh.write('[tool.cleanporter]\npython = "tools/py"\n')
    (project / "src" / "demo" / "consumer.py").write_text(
        "from functools import partial\nf = partial(print)\n", encoding="utf-8"
    )
    monkeypatch.chdir(project / "src")
    cli.main(["demo"])
    out = capsys.readouterr().out
    assert f"interpreter probe '{project / 'tools' / 'py'}'" in out
    assert "stub interpreter ran" in out


@pytest.mark.skipif(os.name != "posix", reason="uses a /bin/sh stub interpreter")
def test_the_python_flag_stays_relative_to_the_cwd(project, monkeypatch, capsys):
    """A CLI path argument means what the shell means; only the config key is anchored."""
    _broken_interpreter(project / "src" / "tools" / "py")
    (project / "src" / "demo" / "consumer.py").write_text(
        "from functools import partial\nf = partial(print)\n", encoding="utf-8"
    )
    monkeypatch.chdir(project / "src")
    cli.main(["--python", "tools/py", "demo"])
    out = capsys.readouterr().out
    assert "stub interpreter ran" in out


def test_an_empty_python_flag_is_an_error(project, capsys):
    """It used to be silently ignored, and the configured or current interpreter used."""
    with pytest.raises(SystemExit) as exc:
        cli.main(["--python", "", str(project / "src")])
    assert exc.value.code == 2
    assert "argument --python: must not be empty" in capsys.readouterr().err


def test_paths_from_different_projects_warn_which_config_is_used(project, tmp_path, capsys):
    other = tmp_path / "other"
    (other / "lib").mkdir(parents=True)
    (other / "pyproject.toml").write_text('[tool.cleanporter]\nscope = "first-party"\n', "utf-8")
    (other / "lib" / "mod.py").write_text("import os\n", encoding="utf-8")
    cli.main([str(project / "src"), str(other / "lib")])
    out = capsys.readouterr().out
    assert "cleanporter: warning: the paths belong to different pyproject.toml files" in out
    assert str(project / "pyproject.toml") in out
    assert f"{other / 'lib'} (nearest: {other / 'pyproject.toml'})" in out


def test_no_config_warning_for_paths_in_one_project(project, monkeypatch, capsys):
    monkeypatch.chdir(project)
    cli.main(["--diff", "src", "src/demo/consumer.py"])
    assert "different pyproject.toml" not in capsys.readouterr().err


def test_a_submodule_missing_from_the_checkout_is_never_rewritten(project, capsys):
    """A generated ``_version.py`` is absent from the tree, not an object.

    Calling it an object rewrote ``from demo import _version`` into
    ``demo._version`` -- an ``AttributeError`` whenever ``demo/__init__``
    had not happened to import it.
    """
    init = "try:\n    from demo import _version\nexcept ImportError:\n    _version = None\n"
    consumer = "from demo import _version\n\nprint(_version.version)\n"
    (project / "src" / "demo" / "__init__.py").write_text(init, encoding="utf-8")
    (project / "src" / "demo" / "consumer.py").write_text(consumer, encoding="utf-8")

    assert cli.main(["--diff", str(project / "src")]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "CP002 could not determine whether 'demo._version' is a module" in captured.err
    assert "'demo' binds '_version' by importing its own submodule of that name" in captured.err

    assert cli.main(["--fix", str(project / "src")]) == 0
    assert (project / "src" / "demo" / "consumer.py").read_text(encoding="utf-8") == consumer
    assert (project / "src" / "demo" / "__init__.py").read_text(encoding="utf-8") == init
    assert cli.main(["--strict", str(project / "src")]) == 1


@pytest.mark.parametrize(
    "init",
    [
        '# -*- coding: latin-1 -*-\nS = "caf\xe9"\n'.encode("latin-1"),
        b"\xef\xbb\xbfS = 1\n",
    ],
    ids=["latin-1", "utf-8-bom"],
)
def test_a_shadowing_init_in_another_encoding_still_blocks_the_rewrite(tmp_path, capsys, init):
    """``P/__init__.py`` rebinds ``S``, so ``from app.P import S`` is not the submodule.

    The UTF-8 spelling of this has always been declined. Decoded as UTF-8, a
    latin-1 or BOM-prefixed ``__init__`` parsed as binding nothing, the
    replacement looked safe, and ``--fix`` wrote ``from app.P import S`` plus
    ``S.obj()`` -- an ``AttributeError`` on a string. Only ``use.py`` is
    analysed, so the ``__init__`` is read by the module map alone.
    """
    pkg = tmp_path / "src" / "app"
    (pkg / "P").mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "P" / "__init__.py").write_bytes(init)
    (pkg / "P" / "S.py").write_text("def obj():\n    return 1\n", encoding="utf-8")
    use = pkg / "use.py"
    source = "from app.P.S import obj\n\nobj()\n"
    use.write_text(source, encoding="utf-8")

    assert cli.main(["--fix", str(use)]) == 1
    err = capsys.readouterr().err
    assert "CP003" in err
    assert "cannot be shown to bind the module 'app.P.S'" in err
    assert use.read_text(encoding="utf-8") == source


def test_a_submodule_a_star_import_shadows_is_not_the_replacement(tmp_path, monkeypatch, capsys):
    """``from pkg.helpers import f`` must not become ``from pkg import helpers``.

    ``pkg/__init__.py`` star-imports ``_impl``, which defines a *function*
    ``helpers``, so ``from pkg import helpers`` binds that function -- and
    ``helpers.f()`` raised ``AttributeError``. Found by running the rewrite,
    not by reading it: the result imports and parses.
    """
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("from ._impl import *\n", encoding="utf-8")
    (pkg / "_impl.py").write_text("def helpers():\n    return 0\n", encoding="utf-8")
    (pkg / "helpers.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    source = "from pkg.helpers import f\n\nprint(f())\n"
    (tmp_path / "app.py").write_text(source, encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert cli.main(["--fix", "app.py"]) == 1
    err = capsys.readouterr().err
    assert "CP003" in err
    assert "the replacement 'from pkg import helpers' cannot be shown" in err
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == source
    run = subprocess.run([sys.executable, "app.py"], cwd=tmp_path, capture_output=True, text=True)
    assert run.returncode == 0, run.stderr


def test_a_circular_reexport_gets_the_same_verdict_however_the_run_is_scoped(
    tmp_path, monkeypatch, capsys
):
    """``pkg`` star-imports ``pkg.m``, which imports ``ver`` back from ``pkg``.

    Checking ``app.py`` alone once gave ``CP001`` for ``pkg.m.ver`` -- an
    object, it said -- while checking it next to ``app2.py`` gave ``CP002``:
    the answer depended on which question the run happened to ask first.
    """
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("from .m import *\n\nver = None\n", encoding="utf-8")
    (pkg / "m.py").write_text("from pkg import ver\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("from pkg.m import ver\n\nprint(ver)\n", encoding="utf-8")
    (tmp_path / "app2.py").write_text("from pkg import ver\n\nprint(ver)\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    for paths in (["app.py"], ["app2.py", "app.py"], ["app.py", "app2.py"]):
        assert cli.main(["--strict", *paths]) == 1
        out = capsys.readouterr().out
        assert "CP001" not in out
        assert out.count("CP002") == len(paths)
        assert "part of a circular import" in out


def test_a_module_reexported_under_another_name_is_not_a_violation(project, capsys):
    """Each binding is followed to where it comes from, so a module stays a module.

    Once, any binding at all meant "object", so the first three of these were
    reported ``CP001`` -- ``from os import path`` included, while the
    equivalent ``import json as codec`` was not.
    """
    demo = project / "src" / "demo"
    (demo / "_version.py").write_text('__version__ = "1"\n', encoding="utf-8")
    (demo / "sub").mkdir()
    (demo / "sub" / "__init__.py").write_text("", encoding="utf-8")
    (demo / "sub" / "tools.py").write_text("X = 1\n", encoding="utf-8")
    (demo / "__init__.py").write_text(
        "from . import _version as version\nfrom .sub import tools\nfrom os import path\n"
        "import json as codec\n",
        encoding="utf-8",
    )
    (demo / "consumer.py").write_text(
        "from demo import version, tools, path, codec\n\n"
        "print(version.__version__, tools.X, path.sep, codec.dumps(1))\n",
        encoding="utf-8",
    )
    assert cli.main(["--strict", str(project / "src")]) == 0
    assert "0 violation(s)" in capsys.readouterr().out


# -- file I/O: encodings, newlines, atomic writes ---------------------------

#: A violation in a CRLF file, and what `--fix` must turn it into: only the
#: two changed lines differ, and every line keeps its ``\r\n``.
_CRLF = b'import os\r\nfrom os.path import join\r\n\r\nprint(join("a", "b"))\r\n'
_CRLF_FIXED = b'import os\r\nfrom os import path\r\n\r\nprint(path.join("a", "b"))\r\n'


def _apply_patch(patch: bytes, cwd: pathlib.Path) -> None:
    """Apply *patch* in *cwd* with ``git apply``, else ``patch``, else skip."""
    if shutil.which("git"):
        command = ["git", "apply", "--whitespace=nowarn", "-"]
    elif shutil.which("patch"):  # pragma: no cover - depends on the machine
        command = ["patch", "-p1", "--binary"]
    else:  # pragma: no cover - depends on the machine
        pytest.skip("neither git nor patch is available")
    proc = subprocess.run(command, input=patch, cwd=cwd, capture_output=True, check=False)
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")


def test_fix_keeps_crlf_line_endings(project, capsys):
    target = project / "src" / "demo" / "crlf.py"
    target.write_bytes(_CRLF)
    assert cli.main(["--fix", str(project / "src")]) == 0
    assert target.read_bytes() == _CRLF_FIXED


def test_diff_of_a_crlf_file_applies_to_it(project, monkeypatch, capsysbinary):
    target = project / "src" / "demo" / "crlf.py"
    target.write_bytes(_CRLF)
    monkeypatch.chdir(project)
    assert cli.main(["--diff", "src"]) == 1
    patch = capsysbinary.readouterr().out
    assert b"-from os.path import join\r\n" in patch
    _apply_patch(patch, project)
    assert target.read_bytes() == _CRLF_FIXED


def test_latin1_file_is_fixed_and_written_back_in_latin1(project, capsysbinary):
    # capsysbinary: `--fix` prints the patch too, in Latin-1, which `capsys`
    # would try to read back as UTF-8.
    target = project / "src" / "demo" / "latin.py"
    target.write_bytes(
        b"# -*- coding: latin-1 -*-\nfrom os.path import join\n"
        b'CAFE = "caf\xe9"\nprint(join(CAFE, "b"))\n'
    )
    assert cli.main(["--fix", str(project / "src")]) == 0
    assert target.read_bytes() == (
        b"# -*- coding: latin-1 -*-\nfrom os import path\n"
        b'CAFE = "caf\xe9"\nprint(path.join(CAFE, "b"))\n'
    )


def test_diff_of_a_latin1_file_applies_to_it(project, monkeypatch, capsysbinary):
    """The patch carries Latin-1 bytes, not the UTF-8 spelling of the text."""
    target = project / "src" / "demo" / "latin.py"
    target.write_bytes(b'# coding: latin-1\nfrom os.path import join\nprint(join("\xe9"))\n')
    monkeypatch.chdir(project)
    cli.main(["--diff", "src"])
    patch = capsysbinary.readouterr().out
    assert b'+print(path.join("\xe9"))\n' in patch
    _apply_patch(patch, project)
    assert target.read_bytes() == (
        b'# coding: latin-1\nfrom os import path\nprint(path.join("\xe9"))\n'
    )


def test_utf8_bom_is_preserved(project, capsys):
    target = project / "src" / "demo" / "bom.py"
    target.write_bytes(b'\xef\xbb\xbffrom os.path import join\nprint(join("a"))\n')
    assert cli.main(["--fix", str(project / "src")]) == 0
    assert target.read_bytes() == b'\xef\xbb\xbffrom os import path\nprint(path.join("a"))\n'


def test_diff_of_a_file_without_a_final_newline_applies(project, monkeypatch, capsysbinary):
    """The ``\\ No newline at end of file`` marker, and a BOM, survive the patch."""
    target = project / "src" / "demo" / "bom.py"
    target.write_bytes(b'\xef\xbb\xbffrom os.path import join\nprint(join("a"))')
    monkeypatch.chdir(project)
    cli.main(["--diff", "src"])
    patch = capsysbinary.readouterr().out
    assert b"\\ No newline at end of file\n" in patch
    _apply_patch(patch, project)
    assert target.read_bytes() == b'\xef\xbb\xbffrom os import path\nprint(path.join("a"))'


def test_undecodable_file_is_reported_and_the_rest_still_fixed(project, capsys):
    """One bad file used to abort the run, naming no file at all."""
    bad = project / "src" / "demo" / "bad.py"
    bad.write_bytes(b"x = 1\ny = '\xff'\n")
    assert cli.main(["--fix", str(project / "src")]) == 2
    err = capsys.readouterr().err
    assert f"{bad}:2:0: CP002 file not processed: cannot decode file as utf-8" in err
    assert bad.read_bytes() == b"x = 1\ny = '\xff'\n"
    consumer = (project / "src" / "demo" / "consumer.py").read_text(encoding="utf-8")
    assert consumer == "from demo import helpers\ntotal = helpers.THING\n"


def test_unknown_coding_cookie_is_reported_with_its_path(project, capsys):
    bad = project / "src" / "demo" / "bad.py"
    bad.write_bytes(b"# -*- coding: no-such-codec -*-\nx = 1\n")
    assert cli.main([str(project / "src")]) == 2
    out = capsys.readouterr().out
    assert f"{bad}:1:0: CP002 file not processed: cannot decode file: unknown encoding" in out
    assert "consumer.py:1:0: CP001" in out


def test_encoding_that_does_not_round_trip_is_declined(project, capsys):
    """cp932 reads ``\\x87\\x90`` and ``\\x81\\xe0`` as the same character.

    Writing the decoded text back would turn the first into the second on a
    line the fix never touched, so the file is declined and left alone.
    """
    target = project / "src" / "demo" / "sjis.py"
    original = b"# coding: cp932\nfrom os.path import join\nprint(join('\x87\x90'))\n"
    target.write_bytes(original)
    assert cli.main(["--fix", str(project / "src")]) == 1
    assert "does not round-trip" in capsys.readouterr().err
    assert target.read_bytes() == original


def test_a_file_ending_in_a_lone_cr_is_left_byte_identical(project, capsys):
    """libCST would drop the final ``\\r``, on a line the fix never touched."""
    target = project / "src" / "demo" / "mac.py"
    original = b'from os.path import join\rprint(join("a"))\rX = 1\r'
    target.write_bytes(original)
    assert cli.main(["--fix", str(project / "src")]) == 1
    assert "libCST does not reproduce this file byte for byte" in capsys.readouterr().err
    assert target.read_bytes() == original


def test_a_rewrite_the_files_encoding_cannot_hold_is_declined(tmp_path, monkeypatch, capsys):
    """The absolute spelling names the package from its directory.

    Inside a package's ``__init__``, ``from .readers import read`` becomes
    ``from анализ.io import readers`` -- a name the file itself never spelled,
    and one its declared Latin-1 cannot hold. Written as UTF-8 it would
    contradict the cookie; the file is declined instead.
    """
    package = tmp_path / "анализ"
    (package / "io").mkdir(parents=True)
    (package / "__init__.py").write_bytes(b"")
    (package / "io" / "readers.py").write_bytes(b"def read():\n    return []\n")
    init = package / "io" / "__init__.py"
    original = b"# coding: latin-1\nfrom .readers import read\n\nvalues = read()\n"
    init.write_bytes(original)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["--fix", "."]) == 1
    err = capsys.readouterr().err
    assert "the rewrite needs 'анализ', which the file's encoding (iso-8859-1) cannot" in err
    assert init.read_bytes() == original


def test_first_line_not_utf8_points_at_the_byte(project, capsys):
    bad = project / "src" / "demo" / "bad.py"
    bad.write_bytes(b"y = '\xff'  # no coding declaration\nx = 1\n")
    assert cli.main([str(project / "src")]) == 2
    out = capsys.readouterr().out
    assert (
        f"{bad}:1:0: CP002 file not processed: cannot decode file: "
        "not UTF-8 and no coding declaration: invalid start byte (b'\\xff')"
    ) in out


def test_unknown_codec_on_the_second_line_points_at_it(project, capsys):
    bad = project / "src" / "demo" / "bad.py"
    bad.write_bytes(b"#!/usr/bin/env python\n# coding: no-such-codec\nx = 1\n")
    assert cli.main([str(project / "src")]) == 2
    assert f"{bad}:2:0: CP002 file not processed: cannot decode file: unknown encoding" in (
        capsys.readouterr().out
    )


def test_fix_refuses_a_read_only_file(project, monkeypatch, capsys):
    """The rename needs only a writable directory; the file must be writable too.

    `os.access` is patched because the suite may run as root, for whom every
    file is writable.
    """
    target = project / "src" / "demo" / "consumer.py"
    before = target.read_bytes()
    real_access = os.access

    def access(path, mode, **kwargs):
        if pathlib.Path(path) == target and mode == os.W_OK:
            return False
        return real_access(path, mode, **kwargs)

    monkeypatch.setattr(os, "access", access)
    assert cli.main(["--fix", str(project / "src")]) == 2
    err = capsys.readouterr().err
    assert f"{target}:1:0: CP002 file not processed: cannot write file: Permission denied" in err
    assert target.read_bytes() == before


@pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() != 0, reason="only root can give a file away"
)
def test_fix_preserves_the_owner(project, capsys):
    target = project / "src" / "demo" / "consumer.py"
    os.chown(target, 4242, 4343)
    assert cli.main(["--fix", str(project / "src")]) == 0
    assert (target.stat().st_uid, target.stat().st_gid) == (4242, 4343)


def test_a_temporary_file_that_cannot_be_removed_is_named(project, monkeypatch, capsys):
    """If cleanup fails too, the report says where the stray file is."""
    target = project / "src" / "demo" / "consumer.py"

    def disk_full(fd: int) -> None:
        raise OSError(28, "No space left on device")

    def stuck(self: pathlib.Path, *, missing_ok: bool = False) -> None:
        raise PermissionError(13, "in use")

    monkeypatch.setattr(os, "fsync", disk_full)
    monkeypatch.setattr(pathlib.Path, "unlink", stuck)
    assert cli.main(["--fix", str(project / "src")]) == 2
    err = capsys.readouterr().err
    assert "cleanporter: could not remove the temporary file" in err
    assert f"{target.parent}{os.sep}.consumer.py." in err


def test_fix_preserves_permission_bits(project, capsys):
    target = project / "src" / "demo" / "consumer.py"
    target.chmod(0o751)
    assert cli.main(["--fix", str(project / "src")]) == 0
    assert target.read_text(encoding="utf-8").startswith("from demo import helpers\n")
    assert target.stat().st_mode & 0o7777 == 0o751


def test_fix_writes_through_a_symlink(project, tmp_path, capsys):
    """The link stays a link; the file it points at is what changes."""
    real = tmp_path / "elsewhere.py"
    real.write_text("from os.path import join\nprint(join('a'))\n", encoding="utf-8")
    link = project / "src" / "demo" / "linked.py"
    link.symlink_to(real)
    assert cli.main(["--fix", str(project / "src")]) == 0
    assert link.is_symlink()
    assert real.read_text(encoding="utf-8") == "from os import path\nprint(path.join('a'))\n"


def test_a_failed_write_leaves_the_file_whole(project, monkeypatch, capsys):
    """A crash mid-write loses the temporary file, never the source."""
    target = project / "src" / "demo" / "consumer.py"
    before = target.read_bytes()

    def disk_full(fd: int) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "fsync", disk_full)
    assert cli.main(["--fix", str(project / "src")]) == 2
    captured = capsys.readouterr()
    assert f"{target}:1:0: CP002 file not processed: cannot write file: No space" in captured.err
    assert captured.out == ""  # no patch for a change that did not happen
    assert target.read_bytes() == before
    assert sorted(p.name for p in target.parent.iterdir()) == [
        "__init__.py",
        "consumer.py",
        "helpers.py",
    ]

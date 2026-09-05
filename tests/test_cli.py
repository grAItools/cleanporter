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
    assert "would bind the existing name instead of the submodule" in captured.err
    assert "from pkg.serialization import MARK" in (project / "pkg" / "__init__.py").read_text(
        encoding="utf-8"
    )
    after = _package_values(project)
    assert after.returncode == 0, after.stderr
    assert after.stdout.split() == ["pkg", "1"]


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

"""Fixer behaviour tests: rewrites must be exact and conservative."""

from __future__ import annotations

from cleanporter.config import Config
from cleanporter.fixer import fix_file

from .conftest import discover, make_resolver


def run_fix(config: Config, source: str):
    resolver = make_resolver(config)
    return fix_file(
        source,
        config.root / "target.py",
        resolver=resolver,
        config=config,
        project_roots=discover(config),
    )


def test_basic_rewrite(config):
    src = "from mypkg.helpers import THING\nuse = THING\n"
    outcome = run_fix(config, src)
    assert outcome.status == "fixed"
    assert outcome.new_source == (
        "from mypkg import helpers\nuse = helpers.THING\n"
    )


def test_idempotent_after_fix(config):
    src = "from mypkg.helpers import THING\nuse = THING\n"
    first = run_fix(config, src)
    second = run_fix(config, first.new_source)
    assert second.status == "clean"


def test_existing_parent_from_import_suppresses_new_one(config):
    src = (
        "from mypkg import helpers\n"
        "from mypkg.helpers import THING\n"
        "print(THING)\n"
    )
    outcome = run_fix(config, src)
    assert outcome.status == "fixed"
    # Binding already exists: no duplicate import inserted, refs rewritten.
    assert outcome.new_source.count("from mypkg import helpers") == 1
    assert "print(helpers.THING)" in outcome.new_source


def test_alias_rename_uses_module_chain(config):
    src = "from mypkg.helpers import Widget as Wg\nx = Wg()\n"
    outcome = run_fix(config, src)
    assert outcome.status == "fixed"
    assert outcome.new_source == (
        "from mypkg import helpers\nx = helpers.Widget()\n"
    )


def test_mixed_statement_keeps_module_imports(config):
    src = (
        "from mypkg import helpers\n"
        "from mypkg.sub import data, SUB_OBJECT\n"
        "z = data.VALUE + SUB_OBJECT\n"
    )
    outcome = run_fix(config, src)
    assert outcome.status == "fixed"
    expected = (
        "from mypkg import helpers\n"
        "from mypkg import sub; from mypkg.sub import data\n"
        "z = data.VALUE + sub.SUB_OBJECT\n"
    )
    assert outcome.new_source == expected


def test_module_level_rebinding_blocks_file(config):
    # libcst scope analysis is not flow-sensitive: every access of a rebound
    # module-level name is ambiguous, so the whole file must stay untouched.
    src = (
        "from mypkg.helpers import THING\n"
        "first = THING\n"
        "THING = 5\n"
        "second = THING\n"
    )
    outcome = run_fix(config, src)
    assert outcome.status == "skipped"
    assert any("rebound" in s.message for s in outcome.skips)


def test_function_local_shadowing_is_safe(config):
    src = (
        "from mypkg.helpers import Widget\n"
        "outer = Widget()\n"
        "def f():\n"
        "    Widget = 'shadow'\n"
        "    return Widget\n"
    )
    outcome = run_fix(config, src)
    assert outcome.status == "fixed"
    assert outcome.new_source == (
        "from mypkg import helpers\n"
        "outer = helpers.Widget()\n"
        "def f():\n"
        "    Widget = 'shadow'\n"
        "    return Widget\n"
    )


def test_type_checking_gate_blocks_file(make_project):
    base = make_project()
    config = Config(root=base)
    src = (
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from mypkg.helpers import Widget\n"
        "def f() -> 'Widget':\n"
        "    return None\n"
    )
    outcome = run_fix(config, src)
    assert outcome.status == "skipped"
    assert any("TYPE_CHECKING" in s.message for s in outcome.skips)


def test_type_checking_gate_ok_with_future_annotations(make_project):
    base = make_project()
    config = Config(root=base)
    src = (
        "from __future__ import annotations\n"
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from mypkg.helpers import Widget\n"
        "def g(x: 'Widget') -> None:\n"
        "    del x\n"
    )
    outcome = run_fix(config, src)
    assert outcome.status == "fixed"
    assert "from mypkg import helpers" in outcome.new_source
    # Lazy string annotations are renamed along with the code.
    assert "'helpers.Widget'" in outcome.new_source


def test_string_mention_blocks_whole_file(config):
    src = (
        "from mypkg.helpers import THING\n"
        '__all__ = ["THING"]\n'
    )
    outcome = run_fix(config, src)
    assert outcome.status == "skipped"
    assert any("string literal" in s.message for s in outcome.skips)


def test_one_liner_suite_blocks(config):
    src = "if True: from mypkg.helpers import THING\n"
    outcome = run_fix(config, src)
    assert outcome.status == "skipped"
    assert any("one-liner" in s.message for s in outcome.skips)


def test_same_line_rebind_aborts(config):
    # Prepending `from mypkg import helpers` races with a same-line
    # rebinding of the newly introduced binding name -> whole-file abort.
    src = 'from mypkg.helpers import THING; helpers = "oops"\nprint(THING)\n'
    outcome = run_fix(config, src)
    assert outcome.status == "skipped"


def test_global_declaration_blocks(config):
    src = (
        "from mypkg.helpers import THING\n"
        "def f():\n"
        "    global THING\n"
        "    THING = 3\n"
    )
    outcome = run_fix(config, src)
    assert outcome.status == "skipped"
    assert any("global" in s.message for s in outcome.skips)


def test_syntax_error_reported(config):
    outcome = run_fix(config, "def broken(:\n")
    assert outcome.status == "error"
    assert outcome.skips

"""Fixer behaviour: rewrites must be exact, or not happen at all."""

from __future__ import annotations

from pathlib import Path

import libcst as cst

from cleanporter.analyze import FileRecord, collect_pairs, package_of
from cleanporter.config import Config
from cleanporter.firstparty import ModuleMap
from cleanporter.model import Status
from cleanporter.resolver import Resolver
from cleanporter.rewrite import FixOutcome, fix_record

FIXTURES = Path(__file__).parent / "fixtures"


def outcome(source: str, config: Config | None = None) -> FixOutcome:
    path = FIXTURES / "pkg" / "a.py"
    mm = ModuleMap.from_paths([FIXTURES / "pkg", path])
    resolver = Resolver(mm)
    rec = FileRecord(path, source, cst.parse_module(source), package_of(path, mm))
    resolver.warm(collect_pairs([rec]))
    return fix_record(rec, resolver, config if config is not None else Config())


def test_basic_rewrite_reports_fixed():
    result = outcome("from pkg.sub.mod import Thing\nx = Thing()\n")
    assert result.status == "fixed"
    assert result.fixed == 1
    assert result.source == "from pkg.sub import mod\nx = mod.Thing()\n"
    assert result.blockers == []


def test_compliant_file_reports_clean():
    src = "from pkg.sub import mod\nx = mod.Thing()\n"
    result = outcome(src)
    assert result.status == "clean"
    assert result.source == src


def test_dunder_all_blocks_the_whole_file():
    src = 'from pkg.sub.mod import Thing\n__all__ = ["Thing"]\nx = Thing()\n'
    result = outcome(src)
    assert result.status == "skipped"
    assert result.source == src, "a blocked file must be byte-identical"
    assert [f.status for f in result.blockers] == [Status.SKIPPED]
    assert "string literal" in result.blockers[0].detail


def test_a_blocker_suppresses_otherwise_safe_rewrites_in_the_same_file():
    # 'go' is perfectly safe to rewrite, but the file is all-or-nothing.
    src = (
        "from pkg.sub.mod import Thing, go\n"
        '__all__ = ["Thing"]\n'
        "x = Thing()\n"
        "y = go()\n"
    )
    result = outcome(src)
    assert result.status == "skipped"
    assert result.source == src


def test_blocker_finding_formats_as_cp003():
    src = 'from pkg.sub.mod import Thing\n__all__ = ["Thing"]\n'
    (blocker,) = outcome(src).blockers
    assert blocker.code == "CP003"
    assert "file not rewritten" in blocker.format()


# -- docstring exemption (fix round 1) --------------------------------------


def test_prose_docstring_mentioning_the_name_does_not_block_the_fix():
    src = '"""Wraps Thing nicely."""\nfrom pkg.sub.mod import Thing\nx = Thing()\n'
    result = outcome(src)
    assert result.status == "fixed"
    assert result.blockers == []


def test_docstring_doctest_mentioning_the_name_blocks_the_fix():
    src = (
        '"""Example.\n\n>>> Thing()\n"""\n'
        "from pkg.sub.mod import Thing\n"
        "x = Thing()\n"
    )
    result = outcome(src)
    assert result.status == "skipped"
    assert result.source == src


def test_non_docstring_string_mentioning_the_name_still_blocks_the_fix():
    # Not the module's first statement -> not a docstring, still blocks.
    src = "from pkg.sub.mod import Thing\nx = Thing()\ny = \"Thing\"\n"
    result = outcome(src)
    assert result.status == "skipped"
    assert result.source == src


def test_dunder_all_mention_still_blocks_after_the_docstring_exemption():
    # Regression lock: __all__ is never a docstring and must keep blocking.
    src = 'from pkg.sub.mod import Thing\n__all__ = ["Thing"]\nx = Thing()\n'
    result = outcome(src)
    assert result.status == "skipped"
    assert result.source == src


# -- unpinned semantics (fix round 1) ----------------------------------------


def test_guard_checks_the_local_alias_not_the_original_name():
    src = "from pkg.sub.mod import Thing as T\n" '__all__ = ["T"]\n' "x = T()\n"
    result = outcome(src)
    assert result.status == "skipped"
    assert result.source == src


def test_guard_does_not_fire_on_the_original_name_after_aliasing():
    src = "from pkg.sub.mod import Thing as T\n" '__all__ = ["Thing"]\n' "x = T()\n"
    result = outcome(src)
    assert result.status == "fixed"


def test_exempt_name_mentioned_in_a_string_does_not_block_the_fix():
    # 'go' is exempted by config, so it is never in _fixed_locals; a string
    # naming it must not produce a false blocker for the file's real fix.
    src = (
        "from pkg.sub.mod import Thing, go\n"
        'y = "go"\n'
        "x = Thing()\n"
        "z = go()\n"
    )
    config = Config(exempt_names=frozenset({"go"}))
    result = outcome(src, config)
    assert result.status == "fixed"
    assert result.blockers == []


def test_two_mentions_of_the_same_name_on_one_line_dedup_to_one_blocker():
    src = (
        "from pkg.sub.mod import Thing\n"
        'y = "Thing" + "Thing"\n'
        "x = Thing()\n"
    )
    result = outcome(src)
    assert result.status == "skipped"
    assert len(result.blockers) == 1


# -- scope declarations guard (Task 9) ----------------------------------------


def test_global_declaration_blocks_the_file():
    src = (
        "from pkg.sub.mod import Thing\n"
        "def f():\n"
        "    global Thing\n"
        "    Thing = 3\n"
    )
    result = outcome(src)
    assert result.status == "skipped"
    assert result.source == src
    # Task 10 also flags this input (the `global` statement makes `Thing = 3`
    # a sibling module-scope assignment to the import, a genuine rebinding),
    # and its blocker sorts first by line. Assert both guards' signatures are
    # present so this test still provides signal about Task 10's behaviour
    # (a bare "any 'global'" check would pass even if Task 10's guard were
    # deleted, since Task 9's own guard already puts "global" in its detail).
    details = [b.detail for b in result.blockers]
    assert any("global" in d for d in details)
    assert any("rebound" in d for d in details)


def test_module_level_rebinding_blocks_the_file():
    src = (
        "from pkg.sub.mod import Thing\n"
        "first = Thing\n"
        "Thing = 5\n"
        "second = Thing\n"
    )
    result = outcome(src)
    assert result.status == "skipped"
    assert result.source == src
    assert "rebound" in result.blockers[0].detail


def test_function_local_shadowing_is_safe_and_still_rewritten():
    src = (
        "from pkg.sub.mod import Thing\n"
        "outer = Thing()\n"
        "def f():\n"
        "    Thing = 'shadow'\n"
        "    return Thing\n"
    )
    result = outcome(src)
    assert result.status == "fixed"
    assert result.source == (
        "from pkg.sub import mod\n"
        "outer = mod.Thing()\n"
        "def f():\n"
        "    Thing = 'shadow'\n"
        "    return Thing\n"
    )


def test_collision_with_the_new_module_token_is_aliased_not_broken():
    src = "from pkg.sub.mod import Thing\nmod = 'a local string'\nx = Thing()\n"
    result = outcome(src)
    assert result.status == "fixed"
    assert "import mod as mod_2" in result.source or "mod as mod_2" in result.source
    assert "mod_2.Thing()" in result.source
    assert "mod = 'a local string'" in result.source


def test_import_never_referenced_is_still_removed():
    result = outcome("from pkg.sub.mod import Thing\nx = 1\n")
    assert result.status == "fixed"
    assert "import Thing" not in result.source


# -- fix round 1 (rebinding guard hygiene) -----------------------------------


def test_import_shadowing_a_builtin_is_still_rewritten():
    # libcst never puts a BuiltinAssignment in GlobalScope alongside a name
    # that is also bound there by an import, so the guard must not treat a
    # builtin-shadowing import as a same-scope rebinding.
    src = "from pkg.sub.mod import Thing as list\nx = list()\n"
    result = outcome(src)
    assert result.status == "fixed"
    assert result.source == "from pkg.sub import mod\nx = mod.Thing()\n"


def test_two_imports_of_the_same_name_both_get_blocked():
    # Two ImportFrom statements binding the same module-level name produce
    # two distinct ImportAssignment objects in scope['Thing']; each import
    # sees the other as a sibling assignment it cannot distinguish itself
    # from, so both are conservatively blocked.
    src = (
        "from pkg.sub.mod import Thing\n"
        "from pkg.sub.mod import Thing\n"
        "x = Thing()\n"
    )
    result = outcome(src)
    assert result.status == "skipped"
    assert result.source == src
    assert len(result.blockers) == 2
    assert all("rebound" in b.detail for b in result.blockers)


def test_unparseable_rewrite_is_reverted_and_reported(monkeypatch):
    import cleanporter.rewrite as rewrite_mod

    def boom(source):
        raise SyntaxError("simulated bad output")

    monkeypatch.setattr(rewrite_mod.ast, "parse", boom)

    src = "from pkg.sub.mod import Thing\nx = Thing()\n"
    result = outcome(src)
    assert result.status == "error"
    assert result.source == src, "the original must be handed back untouched"
    assert result.blockers and "did not parse" in result.blockers[0].detail


def test_valid_rewrite_passes_verification():
    result = outcome("from pkg.sub.mod import Thing\nx = Thing()\n")
    assert result.status == "fixed"


def test_parse_error_with_valueerror_is_handled(monkeypatch):
    # On CPython <3.12, ast.parse raises ValueError for embedded null bytes.
    # This test covers that path regardless of the Python version running.
    import cleanporter.rewrite as rewrite_mod

    def boom(source):
        raise ValueError("source code string cannot contain null bytes")

    monkeypatch.setattr(rewrite_mod.ast, "parse", boom)

    src = "from pkg.sub.mod import Thing\nx = Thing()\n"
    result = outcome(src)
    assert result.status == "error"
    assert result.source == src, "the original must be handed back untouched"
    assert result.blockers and "did not parse" in result.blockers[0].detail

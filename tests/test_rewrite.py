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


def test_trailing_comment_is_preserved():
    src = "from pkg.sub.mod import Thing  # keep me\nx = Thing()\n"
    result = outcome(src)
    assert result.status == "fixed"
    assert result.source == "from pkg.sub import mod  # keep me\nx = mod.Thing()\n"


def test_leading_comments_and_blank_lines_are_preserved():
    src = (
        "# leading comment block\n"
        "# second line\n"
        "\n"
        "from pkg.sub.mod import Thing\n"
        "\n"
        "use = Thing\n"
    )
    result = outcome(src)
    assert result.status == "fixed"
    assert result.source == (
        "# leading comment block\n"
        "# second line\n"
        "\n"
        "from pkg.sub import mod\n"
        "\n"
        "use = mod.Thing\n"
    )


def test_trailing_comment_lands_on_the_last_replacement_line():
    # A genuinely two-statement replacement: `go` is exempted so it is kept
    # on a second "from ... import go" line, while `Thing` is rewritten to a
    # module import. new_lines therefore has two elements, and the trailing
    # comment must land on the *last* one (the kept-names import), not the
    # first (the new module import).
    src = "from pkg.sub.mod import Thing, go  # both\nx = Thing()\ny = go()\n"
    config = Config(exempt_names=frozenset({"go"}))
    result = outcome(src, config)
    assert result.status == "fixed"
    assert result.source == (
        "from pkg.sub import mod\n"
        "from pkg.sub.mod import go  # both\n"
        "x = mod.Thing()\n"
        "y = go()\n"
    )


def test_deleting_a_commented_line_blocks_instead_of_dropping_the_comment():
    src = (
        "from pkg.sub import mod\n"
        "from pkg.sub.mod import Thing  # why this exists\n"
        "x = Thing()\n"
    )
    result = outcome(src)
    assert result.status == "skipped"
    assert result.source == src
    assert "comment" in result.blockers[0].detail


def test_deleting_an_uncommented_line_is_fine():
    src = "from pkg.sub import mod\nfrom pkg.sub.mod import Thing\nx = Thing()\n"
    result = outcome(src)
    assert result.status == "fixed"
    assert result.source == "from pkg.sub import mod\nx = mod.Thing()\n"


def test_deleting_a_line_with_leading_comment_blocks_instead_of_dropping_it():
    # Same rationale as the trailing-comment deletion case: when the module
    # is already bound and the line disappears entirely, a *leading* comment
    # has nowhere to go either. Silently discarding it is worse than
    # declining to fix the file.
    src = (
        "from pkg.sub import mod\n"
        "# why this exists\n"
        "from pkg.sub.mod import Thing\n"
        "x = Thing()\n"
    )
    result = outcome(src)
    assert result.status == "skipped"
    assert result.source == src
    assert "comment" in result.blockers[0].detail


def test_deleting_a_line_preceded_only_by_a_blank_line_is_still_fixed():
    # leading_lines also holds blank-line EmptyLine nodes with comment=None;
    # those must not trigger the leading-comment block.
    src = (
        "from pkg.sub import mod\n"
        "\n"
        "from pkg.sub.mod import Thing\n"
        "x = Thing()\n"
    )
    result = outcome(src)
    assert result.status == "fixed"
    assert result.source == "from pkg.sub import mod\nx = mod.Thing()\n"


def test_function_scope_import_is_fixed_in_place():
    src = "def f():\n    from pkg.sub.mod import Thing\n    return Thing()\n"
    result = outcome(src)
    assert result.status == "fixed"
    assert result.source == (
        "def f():\n    from pkg.sub import mod\n    return mod.Thing()\n"
    )


def test_each_function_gets_its_own_import():
    src = (
        "def f():\n"
        "    from pkg.sub.mod import Thing\n"
        "    return Thing()\n"
        "def g():\n"
        "    from pkg.sub.mod import Thing\n"
        "    return Thing()\n"
    )
    result = outcome(src)
    assert result.status == "fixed"
    assert result.source.count("from pkg.sub import mod") == 2


def test_function_scope_reuses_a_module_level_binding():
    src = (
        "from pkg.sub import mod\n"
        "def f():\n"
        "    from pkg.sub.mod import Thing\n"
        "    return Thing()\n"
    )
    result = outcome(src)
    assert result.status == "fixed"
    assert result.source == (
        "from pkg.sub import mod\ndef f():\n    return mod.Thing()\n"
    )


def test_function_scope_avoids_colliding_with_a_local():
    src = (
        "def f():\n"
        "    mod = 'a local'\n"
        "    from pkg.sub.mod import Thing\n"
        "    return mod, Thing()\n"
    )
    result = outcome(src)
    assert result.status == "fixed"
    assert "mod_2" in result.source
    assert "mod = 'a local'" in result.source


def test_class_body_import_is_fixed():
    src = "class C:\n    from pkg.sub.mod import Thing\n    x = Thing\n"
    result = outcome(src)
    assert result.status == "fixed"
    assert "from pkg.sub import mod" in result.source
    assert "x = mod.Thing" in result.source


# -- Task 15 fix-round-1 regressions ---------------------------------------
#
# The three tests below each pin down a collision-model defect the first
# implementation missed. Each is only reachable now that non-global-scope
# imports are rewritten at all (Task 15's headline change); before that,
# these imports were reported but left untouched.


def test_critical_1_global_names_populated_without_a_module_level_import():
    # `_global_names` must come from *any* scope's view of the module, not
    # only from a GlobalScope-scoped import line -- this file's only import
    # is function-local, so a naive "only look at GlobalScope-scoped
    # imports" collection leaves `_global_names` empty and the new binding
    # wrongly reuses (and shadows) the module-level `mod` string.
    src = (
        "mod = 'a module-level string'\n"
        "def f():\n"
        "    from pkg.sub.mod import Thing\n"
        "    return mod, Thing()\n"
    )
    result = outcome(src)
    assert result.status == "fixed"
    assert result.source == (
        "mod = 'a module-level string'\n"
        "def f():\n"
        "    from pkg.sub import mod as mod_2\n"
        "    return mod, mod_2.Thing()\n"
    )


def test_critical_2_a_module_level_token_allocation_is_visible_to_later_functions():
    # `mod` is allocated for the module-level import first; the function's
    # own import resolves to a *different* parent that happens to share the
    # same trailing token ("mod"). The function-scope allocation must see
    # the module-level allocation that just happened and alias around it --
    # a frozen locals|globals snapshot taken before planning began would
    # miss it and collide both bindings on the same name.
    src = (
        "from pkg.sub.mod import Thing\n"
        "def f():\n"
        "    from pkg.other.mod import Other\n"
        "    return Thing(), Other()\n"
    )
    result = outcome(src)
    assert result.status == "fixed"
    assert result.source == (
        "from pkg.sub import mod\n"
        "def f():\n"
        "    from pkg.other import mod as mod_2\n"
        "    return mod.Thing(), mod_2.Other()\n"
    )


def test_critical_3_enclosing_function_scope_local_blocks_a_new_binding():
    # `inner`'s import must not bind `mod` -- that name is a local of the
    # *enclosing* function `outer`, not of `inner` itself, so a collision
    # check that only inspects `inner`'s own local names misses it and
    # silently kills the closure variable.
    src = (
        "def outer():\n"
        "    mod = 'closure string'\n"
        "    def inner():\n"
        "        from pkg.sub.mod import Thing\n"
        "        return mod, Thing()\n"
    )
    result = outcome(src)
    assert result.status == "fixed"
    assert result.source == (
        "def outer():\n"
        "    mod = 'closure string'\n"
        "    def inner():\n"
        "        from pkg.sub import mod as mod_2\n"
        "        return mod, mod_2.Thing()\n"
    )


def test_critical_3_enclosing_function_scope_local_blocks_reusing_an_existing_import():
    # Same root cause as the previous test, but hitting the *other* code
    # path: reusing an existing module-level import instead of allocating a
    # fresh one. `outer`'s local `mod` must still block `inner` from
    # reusing the module-level `from pkg.sub import mod`, or `inner`
    # silently reads `outer`'s string instead of the module.
    src = (
        "from pkg.sub import mod\n"
        "def outer():\n"
        "    mod = 'shadow'\n"
        "    def inner():\n"
        "        from pkg.sub.mod import Thing\n"
        "        return Thing()\n"
    )
    result = outcome(src)
    assert result.status == "fixed"
    assert result.source == (
        "from pkg.sub import mod\n"
        "def outer():\n"
        "    mod = 'shadow'\n"
        "    def inner():\n"
        "        from pkg.sub import mod as mod_2\n"
        "        return mod_2.Thing()\n"
    )


# -- Task 15 fix-round-2 regressions ---------------------------------------
#
# Round 1 fixed the collision model's *upward* walk (a binding must avoid
# names an enclosing scope assigns). It missed the mirror case: a binding
# must also avoid names assigned in a scope *below* it, if that scope is
# where one of the binding's own references actually lives -- otherwise the
# reference resolves to the local, not the import.


def test_downward_shadowing_at_module_scope_aliases_around_the_local():
    # `Thing()` is read from inside `f`, where `mod` is a plain local. The
    # module-level import must not bind `mod` -- it must alias to `mod_2`,
    # or `mod.Thing()` inside `f` would resolve `mod` to the integer `1`.
    src = (
        "from pkg.sub.mod import Thing\n"
        "def f():\n"
        "    mod = 1\n"
        "    return mod, Thing()\n"
    )
    result = outcome(src)
    assert result.status == "fixed"
    assert result.source == (
        "from pkg.sub import mod as mod_2\n"
        "def f():\n"
        "    mod = 1\n"
        "    return mod, mod_2.Thing()\n"
    )


def test_downward_shadowing_one_level_deeper_through_a_closure():
    # Same defect, one scope further down: the import lives in `outer`, and
    # the shadowing local and the read both live in the nested `inner`.
    src = (
        "def outer():\n"
        "    from pkg.sub.mod import Thing\n"
        "    def inner():\n"
        "        mod = 1\n"
        "        return mod, Thing()\n"
        "    return inner\n"
    )
    result = outcome(src)
    assert result.status == "fixed"
    assert result.source == (
        "def outer():\n"
        "    from pkg.sub import mod as mod_2\n"
        "    def inner():\n"
        "        mod = 1\n"
        "        return mod, mod_2.Thing()\n"
        "    return inner\n"
    )


def test_no_spurious_alias_when_all_references_are_at_the_import_s_own_scope():
    # Regression lock: the downward-shadowing check must not fire when
    # every reference is at the same scope as the import itself (the
    # overwhelmingly common case) -- no local anywhere below to protect
    # against, so the plain, unaliased import is still expected.
    src = "from pkg.sub.mod import Thing\nx = Thing()\n"
    result = outcome(src)
    assert result.status == "fixed"
    assert result.source == "from pkg.sub import mod\nx = mod.Thing()\n"

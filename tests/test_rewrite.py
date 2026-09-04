"""Fixer behaviour: rewrites must be exact, or not happen at all."""

from __future__ import annotations

import pathlib

import libcst as cst

from cleanporter import analyze, firstparty, model, rewrite
from cleanporter import config as config_lib
from cleanporter import resolver as resolver_lib

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def outcome(source: str, config: config_lib.Config | None = None) -> rewrite.FixOutcome:
    path = FIXTURES / "pkg" / "a.py"
    mm = firstparty.ModuleMap.from_paths([FIXTURES / "pkg", path])
    resolver = resolver_lib.Resolver(mm)
    rec = analyze.FileRecord(path, source, cst.parse_module(source), analyze.package_of(path, mm))
    resolver.warm(analyze.collect_pairs([rec]))
    return rewrite.fix_record(rec, resolver, config if config is not None else config_lib.Config())


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
    assert [f.status for f in result.blockers] == [model.Status.SKIPPED]
    assert "string literal" in result.blockers[0].detail


def test_a_blocker_suppresses_otherwise_safe_rewrites_in_the_same_file():
    # 'go' is perfectly safe to rewrite, but the file is all-or-nothing.
    src = 'from pkg.sub.mod import Thing, go\n__all__ = ["Thing"]\nx = Thing()\ny = go()\n'
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
    src = '"""Example.\n\n>>> Thing()\n"""\nfrom pkg.sub.mod import Thing\nx = Thing()\n'
    result = outcome(src)
    assert result.status == "skipped"
    assert result.source == src


def test_non_docstring_string_mentioning_the_name_still_blocks_the_fix():
    # Not the module's first statement -> not a docstring, still blocks.
    src = 'from pkg.sub.mod import Thing\nx = Thing()\ny = "Thing"\n'
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
    src = 'from pkg.sub.mod import Thing as T\n__all__ = ["T"]\nx = T()\n'
    result = outcome(src)
    assert result.status == "skipped"
    assert result.source == src


def test_guard_does_not_fire_on_the_original_name_after_aliasing():
    src = 'from pkg.sub.mod import Thing as T\n__all__ = ["Thing"]\nx = T()\n'
    result = outcome(src)
    assert result.status == "fixed"


def test_exempt_name_mentioned_in_a_string_does_not_block_the_fix():
    # 'go' is exempted by config, so it is never in _fixed_locals; a string
    # naming it must not produce a false blocker for the file's real fix.
    src = 'from pkg.sub.mod import Thing, go\ny = "go"\nx = Thing()\nz = go()\n'
    config = config_lib.Config(exempt_names=frozenset({"go"}))
    result = outcome(src, config)
    assert result.status == "fixed"
    assert result.blockers == []


def test_two_mentions_of_the_same_name_on_one_line_dedup_to_one_blocker():
    src = 'from pkg.sub.mod import Thing\ny = "Thing" + "Thing"\nx = Thing()\n'
    result = outcome(src)
    assert result.status == "skipped"
    assert len(result.blockers) == 1


# -- scope declarations guard (Task 9) ----------------------------------------


def test_global_declaration_blocks_the_file():
    src = "from pkg.sub.mod import Thing\ndef f():\n    global Thing\n    Thing = 3\n"
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
    src = "from pkg.sub.mod import Thing\nfirst = Thing\nThing = 5\nsecond = Thing\n"
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
    src = "from pkg.sub.mod import Thing\nfrom pkg.sub.mod import Thing\nx = Thing()\n"
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
    src = "# leading comment block\n# second line\n\nfrom pkg.sub.mod import Thing\n\nuse = Thing\n"
    result = outcome(src)
    assert result.status == "fixed"
    assert result.source == (
        "# leading comment block\n# second line\n\nfrom pkg.sub import mod\n\nuse = mod.Thing\n"
    )


def test_trailing_comment_lands_on_the_last_replacement_line():
    # A genuinely two-statement replacement: `go` is exempted so it is kept
    # on a second "from ... import go" line, while `Thing` is rewritten to a
    # module import. new_lines therefore has two elements, and the trailing
    # comment must land on the *last* one (the kept-names import), not the
    # first (the new module import).
    src = "from pkg.sub.mod import Thing, go  # both\nx = Thing()\ny = go()\n"
    config = config_lib.Config(exempt_names=frozenset({"go"}))
    result = outcome(src, config)
    assert result.status == "fixed"
    assert result.source == (
        "from pkg.sub import mod\nfrom pkg.sub.mod import go  # both\nx = mod.Thing()\ny = go()\n"
    )


def test_deleting_a_commented_line_blocks_instead_of_dropping_the_comment():
    src = "from pkg.sub import mod\nfrom pkg.sub.mod import Thing  # why this exists\nx = Thing()\n"
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
    src = "from pkg.sub import mod\n# why this exists\nfrom pkg.sub.mod import Thing\nx = Thing()\n"
    result = outcome(src)
    assert result.status == "skipped"
    assert result.source == src
    assert "comment" in result.blockers[0].detail


def test_deleting_a_line_preceded_only_by_a_blank_line_is_still_fixed():
    # leading_lines also holds blank-line EmptyLine nodes with comment=None;
    # those must not trigger the leading-comment block.
    src = "from pkg.sub import mod\n\nfrom pkg.sub.mod import Thing\nx = Thing()\n"
    result = outcome(src)
    assert result.status == "fixed"
    assert result.source == "from pkg.sub import mod\nx = mod.Thing()\n"


def test_function_scope_import_is_fixed_in_place():
    src = "def f():\n    from pkg.sub.mod import Thing\n    return Thing()\n"
    result = outcome(src)
    assert result.status == "fixed"
    assert result.source == ("def f():\n    from pkg.sub import mod\n    return mod.Thing()\n")


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
        "from pkg.sub import mod\ndef f():\n    from pkg.sub.mod import Thing\n    return Thing()\n"
    )
    result = outcome(src)
    assert result.status == "fixed"
    assert result.source == ("from pkg.sub import mod\ndef f():\n    return mod.Thing()\n")


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
    src = "from pkg.sub.mod import Thing\ndef f():\n    mod = 1\n    return mod, Thing()\n"
    result = outcome(src)
    assert result.status == "fixed"
    assert result.source == (
        "from pkg.sub import mod as mod_2\ndef f():\n    mod = 1\n    return mod, mod_2.Thing()\n"
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
    # Regression lock, strengthened per fix-round-3: an *unrelated* function
    # `g` has a local `mod = 1`, but nothing inside `g` references `Thing`
    # -- every actual reference to the import lives at the import's own
    # (module) scope. The forbidden "naive fix" the collision model was
    # explicitly built to avoid -- unioning in *all* descendant locals,
    # anywhere in the file -- would wrongly alias here regardless; only the
    # precise, access-site-driven implementation leaves this unaliased. A
    # version with no function locals at all (the original round-2 form of
    # this test) cannot tell the two implementations apart.
    src = "from pkg.sub.mod import Thing\nx = Thing()\ndef g():\n    mod = 1\n    return mod\n"
    result = outcome(src)
    assert result.status == "fixed"
    assert result.source == (
        "from pkg.sub import mod\nx = mod.Thing()\ndef g():\n    mod = 1\n    return mod\n"
    )


def test_downward_shadowing_split_across_two_import_lines_reallocates_not_blocks():
    # `pkg.sub.mod` is imported on two separate lines sharing one scope.
    # Only the second line's reference (`go()`, read from inside `f`, where
    # `mod = 1`) is downward-shadowed; the first line's reference (`Thing()`
    # at module scope) is not. `_binding_for` memoizes one token per
    # (scope, parent), so a naive dedup-by-key early return would let the
    # first line's unshadowed token choice ("mod") silently leak into the
    # second line too -- the single-statement form
    # `from pkg.sub.mod import Thing, go` already handles this correctly
    # (it computes one `extra_avoid` covering both names' accesses before
    # picking a token at all); splitting the import across two lines must
    # not regress that (fix-round-3 Critical). The fix reallocates a fresh
    # token for the second line instead of reusing the shadowed one or
    # blocking -- two imports of one module under different aliases is
    # semantically fine.
    src = (
        "from pkg.sub.mod import Thing\n"
        "from pkg.sub.mod import go\n"
        "x = Thing()\n"
        "def f():\n"
        "    mod = 1\n"
        "    return mod, go()\n"
    )
    result = outcome(src)
    assert result.status == "fixed"
    assert result.source == (
        "from pkg.sub import mod\n"
        "from pkg.sub import mod as mod_2\n"
        "x = mod.Thing()\n"
        "def f():\n"
        "    mod = 1\n"
        "    return mod, mod_2.go()\n"
    )


def test_downward_shadowing_blocks_reusing_an_existing_import_too():
    # Covers the *other* branch of `_binding_for`: reusing a pre-existing
    # module-level import, not allocating a fresh token. `existing in
    # extra_avoid` is the one new predicate in that branch that is
    # unconditional -- it must fire for a `GlobalScope` binding too, unlike
    # the ancestor-shadow check beside it, which is skipped for
    # `GlobalScope`. Without it, `Thing`'s import would wrongly reuse the
    # existing module-level `mod`, even though `f`'s local `mod = 1`
    # shadows it exactly where `Thing()` is actually read.
    src = (
        "from pkg.sub import mod\n"
        "from pkg.sub.mod import Thing\n"
        "def f():\n"
        "    mod = 1\n"
        "    return mod, Thing()\n"
    )
    result = outcome(src)
    assert result.status == "fixed"
    assert result.source == (
        "from pkg.sub import mod\n"
        "from pkg.sub import mod as mod_2\n"
        "def f():\n"
        "    mod = 1\n"
        "    return mod, mod_2.Thing()\n"
    )


_TC_HEAD = "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n"


def test_type_checking_without_future_annotations_blocks():
    src = _TC_HEAD + "    from pkg.sub.mod import Thing\ndef g(x: Thing) -> None: ...\n"
    result = outcome(src)
    assert result.status == "skipped"
    assert result.source == src
    assert "TYPE_CHECKING" in result.blockers[0].detail


def test_type_checking_with_future_annotations_is_fixed():
    src = (
        "from __future__ import annotations\n"
        + _TC_HEAD
        + "    from pkg.sub.mod import Thing\ndef g(x: Thing) -> None: ...\n"
    )
    result = outcome(src)
    assert result.status == "fixed"
    assert "    from pkg.sub import mod" in result.source
    assert "def g(x: mod.Thing) -> None" in result.source


def test_lazy_string_annotation_is_renamed():
    src = (
        "from __future__ import annotations\n"
        + _TC_HEAD
        + "    from pkg.sub.mod import Thing\ndef g(x: 'Thing') -> None: ...\n"
    )
    result = outcome(src)
    assert result.status == "fixed"
    assert "'mod.Thing'" in result.source


def test_alias_in_lazy_annotation_is_renamed_by_local_name():
    src = (
        "from __future__ import annotations\n"
        + _TC_HEAD
        + "    from pkg.sub.mod import Thing as T\ndef g(x: 'T') -> None: ...\n"
    )
    result = outcome(src)
    assert result.status == "fixed"
    assert "'mod.Thing'" in result.source
    assert "'T'" not in result.source


def test_string_outside_an_annotation_still_blocks_under_future_annotations():
    src = 'from __future__ import annotations\nfrom pkg.sub.mod import Thing\n__all__ = ["Thing"]\n'
    result = outcome(src)
    assert result.status == "skipped"
    assert "string literal" in result.blockers[0].detail


def test_annotated_assignment_string_is_renamed():
    src = (
        "from __future__ import annotations\n"
        + _TC_HEAD
        + "    from pkg.sub.mod import Thing\nvalue: 'Thing' = None\n"
    )
    result = outcome(src)
    assert result.status == "fixed"
    assert "value: 'mod.Thing' = None" in result.source


def test_type_checking_binding_reused_by_later_runtime_import():
    # `if TYPE_CHECKING:` is GlobalScope to libcst, same as the module body,
    # so a rewritten TC line can memoize a binding there. A *later* runtime
    # import of the same parent must never reuse a binding that lives only
    # inside the guarded block -- that block never executes, so the runtime
    # reference would raise NameError (fix-round-1 Critical 1). Planning
    # runtime lines before TC lines guarantees the shared binding is the
    # runtime one; here the TC import textually comes first, so this pins
    # the direction a naive textual-order plan would get wrong.
    src = (
        "from __future__ import annotations\n"
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from pkg.sub.mod import Thing\n"
        "from pkg.sub.mod import go\n"
        "def g(x: Thing) -> None:\n"
        "    return go()\n"
    )
    result = outcome(src)
    assert result.status == "fixed"
    assert result.source == (
        "from __future__ import annotations\n"
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    pass\n"
        "from pkg.sub import mod\n"
        "def g(x: mod.Thing) -> None:\n"
        "    return mod.go()\n"
    )


def test_type_checking_binding_reused_by_earlier_runtime_import():
    # The mirror of the previous test: the runtime import textually comes
    # *first* here, so a plan that merely preserved textual order would
    # happen to get this one right while still failing the other -- a
    # single passing order is exactly what hid fix-round-1 Critical 1.
    src = (
        "from __future__ import annotations\n"
        "from typing import TYPE_CHECKING\n"
        "from pkg.sub.mod import go\n"
        "if TYPE_CHECKING:\n"
        "    from pkg.sub.mod import Thing\n"
        "def g(x: Thing) -> None:\n"
        "    return go()\n"
    )
    result = outcome(src)
    assert result.status == "fixed"
    assert result.source == (
        "from __future__ import annotations\n"
        "from typing import TYPE_CHECKING\n"
        "from pkg.sub import mod\n"
        "if TYPE_CHECKING:\n"
        "    pass\n"
        "def g(x: mod.Thing) -> None:\n"
        "    return mod.go()\n"
    )


def test_dotted_prefixed_lazy_string_is_not_corrupted_and_blocks():
    # `\b` alone matches right after a `.` (a non-word character), so a
    # naive pattern would turn `'other.Thing'` into a fabricated
    # `'other.mod.Thing'` -- a dotted path nobody ever bound (fix-round-1
    # Critical 2). The lookbehind must leave it untouched, which drops it
    # out of `skip_ids` and lets the ordinary string-mention guard block
    # the whole file instead of silently corrupting it.
    src = (
        "from __future__ import annotations\n" + _TC_HEAD + "    from pkg.sub.mod import Thing\n"
        "def g(a: 'Thing', b: 'other.Thing') -> None: ...\n"
    )
    result = outcome(src)
    assert result.status == "skipped"
    assert result.source == src
    assert any("string literal" in b.detail for b in result.blockers)


def test_literal_string_argument_is_not_treated_as_a_type_reference():
    # `Literal['Thing']`'s argument is a value, not a type reference.
    # Treating it as one and renaming it would silently change the literal
    # a runtime `==` comparison depends on (fix-round-1 Critical 2). Once
    # excluded from `_annotation_strings`, the string is untouched by the
    # annotation-rewrite pass and falls back to the ordinary string-mention
    # guard, which blocks conservatively instead of guessing.
    src = (
        "from __future__ import annotations\n" + _TC_HEAD + "    from pkg.sub.mod import Thing\n"
        "from typing import Literal\n"
        "def g(a: Literal['Thing']) -> None: ...\n"
    )
    result = outcome(src)
    assert result.status == "skipped"
    assert any("string literal" in b.detail for b in result.blockers)


def test_annotated_metadata_string_is_not_treated_as_a_type_reference():
    # `Annotated[T, ...]` mixes one real type with arbitrary metadata.
    # A metadata string that happens to mention the renamed name in prose
    # must not be silently rewritten (or silently exempted from the
    # string-mention guard) just because it sits inside an annotation slot.
    src = (
        "from __future__ import annotations\n" + _TC_HEAD + "    from pkg.sub.mod import Thing\n"
        "from typing import Annotated\n"
        'def g(a: Annotated[Thing, "Thing"]) -> None: ...\n'
    )
    result = outcome(src)
    assert result.status == "skipped"
    assert any("string literal" in b.detail for b in result.blockers)


def test_annotated_first_slot_lazy_string_is_still_renamed():
    # The narrowing must not over-reach: `Annotated`'s *first* slice element
    # is the genuine type, so a lazy string sitting there is still a real
    # forward reference and must still be renamed.
    src = (
        "from __future__ import annotations\n" + _TC_HEAD + "    from pkg.sub.mod import Thing\n"
        "from typing import Annotated\n"
        "def g(a: Annotated['Thing', 'meta']) -> None: ...\n"
    )
    result = outcome(src)
    assert result.status == "fixed"
    assert "Annotated['mod.Thing', 'meta']" in result.source


def test_forward_ref_inside_list_and_dict_subscript_still_renamed():
    # `Optional['Thing']`, `list['Thing']` and `dict[str, 'Thing']` are
    # ordinary subscripts whose slice elements are genuine types -- the
    # `Literal`/`Annotated` narrowing must not exclude them too.
    src = (
        "from __future__ import annotations\n" + _TC_HEAD + "    from pkg.sub.mod import Thing\n"
        "def g(a: list['Thing'], b: dict[str, 'Thing']) -> None: ...\n"
    )
    result = outcome(src)
    assert result.status == "fixed"
    assert "list['mod.Thing']" in result.source
    assert "dict[str, 'mod.Thing']" in result.source


def test_star_args_lazy_string_annotation_is_renamed():
    # `*args`/`**kwargs` annotations were not absorbed by
    # `_annotation_strings`, so a lazy string there fell through to the
    # string-mention guard and blocked the file needlessly.
    src = (
        "from __future__ import annotations\n" + _TC_HEAD + "    from pkg.sub.mod import Thing\n"
        "def g(*args: 'Thing', **kwargs: 'Thing') -> None: ...\n"
    )
    result = outcome(src)
    assert result.status == "fixed"
    assert "*args: 'mod.Thing'" in result.source
    assert "**kwargs: 'mod.Thing'" in result.source


def test_fully_stringified_literal_annotation_blocks_payload_intact():
    # When the *entire* annotation is one string, there is no `Subscript`
    # node in the CST for the fix-round-1 narrowing to see -- the content
    # only becomes a `Subscript` once parsed. `_rewrite_type_expr` reapplies
    # the same Literal-is-opaque rule to the *parsed* content, so nothing
    # changes and the string is left for the ordinary guard to block
    # (fix-round-2 Critical 2, part A).
    src = (
        "from __future__ import annotations\n" + _TC_HEAD + "    from pkg.sub.mod import Thing\n"
        "from typing import Literal\n"
        "def g(a: \"Literal['Thing']\") -> None: ...\n"
    )
    result = outcome(src)
    assert result.status == "skipped"
    assert result.source == src
    assert any("string literal" in b.detail for b in result.blockers)


def test_fully_stringified_annotated_metadata_blocks_payload_intact():
    # Same root cause as above, for `Annotated`'s metadata half.
    src = (
        "from __future__ import annotations\n" + _TC_HEAD + "    from pkg.sub.mod import Thing\n"
        "from typing import Annotated\n"
        "def g(a: \"Annotated[int, 'Thing']\") -> None: ...\n"
    )
    result = outcome(src)
    assert result.status == "skipped"
    assert result.source == src
    assert any("string literal" in b.detail for b in result.blockers)


def test_fully_stringified_dotted_name_is_not_corrupted():
    # `"other.Thing"` parses to `Attribute(value=Name('other'), attr=Name(
    # 'Thing'))`. `Thing` there is the `.attr` half of an `Attribute`, a
    # syntax slot rather than an independent reference, so
    # `_rewrite_type_expr` never visits it -- nothing changes, and the
    # string blocks instead of becoming a fabricated `other.mod.Thing`.
    src = (
        "from __future__ import annotations\n" + _TC_HEAD + "    from pkg.sub.mod import Thing\n"
        'def g(a: "other.Thing") -> None: ...\n'
    )
    result = outcome(src)
    assert result.status == "skipped"
    assert result.source == src
    assert any("string literal" in b.detail for b in result.blockers)


def test_fully_stringified_list_subscript_inner_ref_is_still_renamed():
    # The narrowing must not over-reach into genuine type positions: a
    # `list[...]` slice is not `Literal`/`Annotated`, so its element is a
    # real type. The inner `'Thing'` is itself a nested forward-reference
    # string once the outer string is parsed, and is recursed into via
    # `_rewrite_string_content` -- proving the parse-based approach handles
    # a doubly-stringified annotation, not just a singly-stringified one.
    src = (
        "from __future__ import annotations\n" + _TC_HEAD + "    from pkg.sub.mod import Thing\n"
        "def g(a: \"list['Thing']\") -> None: ...\n"
    )
    result = outcome(src)
    assert result.status == "fixed"
    assert "a: \"list['mod.Thing']\"" in result.source


def test_unparseable_annotation_string_blocks_instead_of_guessing():
    # "Never guess: an import that cannot be classified is reported, never
    # rewritten" applies just as much to a string that fails to parse as an
    # expression at all -- `cst.parse_expression` raises, so the string is
    # left untouched and the ordinary string-mention guard blocks the file.
    src = (
        "from __future__ import annotations\n" + _TC_HEAD + "    from pkg.sub.mod import Thing\n"
        "def g(a: 'Thing[') -> None: ...\n"
    )
    result = outcome(src)
    assert result.status == "skipped"
    assert result.source == src
    assert any("string literal" in b.detail for b in result.blockers)


def test_double_quoted_lazy_annotation_keeps_its_quote_style():
    src = (
        "from __future__ import annotations\n"
        + _TC_HEAD
        + '    from pkg.sub.mod import Thing\ndef g(a: "Thing") -> None: ...\n'
    )
    result = outcome(src)
    assert result.status == "fixed"
    assert 'a: "mod.Thing"' in result.source
    assert "'mod.Thing'" not in result.source


def test_escaped_quote_annotation_blocks_instead_of_corrupting():
    # `_rewrite_string_content` decodes the string via `evaluated_value`
    # (unescaping `\'` to `'`), renames, and re-wraps the render in the
    # *original* quote character. If that character reappears inside the
    # render -- as it does here, since the content is `list['Thing']` and
    # the outer quote is also `'` -- naively re-wrapping would silently
    # terminate the string early and corrupt the file (fix-round-3 New 1).
    # Before this fix, `fix_record`'s own re-parse safety net caught the
    # resulting syntax error and reverted to "error" status; the correct
    # outcome is a real CP003 block, not an internal-error revert -- a
    # previously reachable, if regex-fragile, fix should degrade to
    # "reported", not to "crashed and recovered".
    src = (
        "from __future__ import annotations\n" + _TC_HEAD + "    from pkg.sub.mod import Thing\n"
        "def g(a: 'list[\\'Thing\\']') -> None: ...\n"
    )
    result = outcome(src)
    assert result.status == "skipped"
    assert result.source == src
    assert any("string literal" in b.detail for b in result.blockers)


def test_nested_unparseable_element_blocks_whole_annotation():
    # `"dict['Thing', 'Thing[']"` parses fine as a whole (both elements are
    # syntactically valid string literals within the outer dict[...]
    # expression); the first element's own *content* ("Thing") parses and
    # renames correctly, but the second's content ("Thing[") does not parse
    # at all. If that nested failure merely left the second element
    # untouched while keeping the first element's rename, the whole
    # annotation would be recorded as fixed with an unclassifiable `'Thing['`
    # surviving -- referencing a name whose import was just rewritten away,
    # and hidden from the string-mention guard by the very `skip_ids` entry
    # that rename produced (fix-round-3 New 2). The nested failure must
    # abort the entire candidate string's rewrite instead.
    src = (
        "from __future__ import annotations\n" + _TC_HEAD + "    from pkg.sub.mod import Thing\n"
        "def g(a: \"dict['Thing', 'Thing[']\") -> None: ...\n"
    )
    result = outcome(src)
    assert result.status == "skipped"
    assert result.source == src
    assert any("string literal" in b.detail for b in result.blockers)


def test_triple_quoted_escaped_quote_annotation_blocks_instead_of_erroring():
    # The round-3 containment check (`node.quote in rendered`) only covers
    # the shape it was written against. Here `node.quote` is `'''` while the
    # render is `'mod.Thing'` -- the *three*-character quote never appears in
    # it, so the check passes, yet re-wrapping produces `''''mod.Thing''''`,
    # which does not parse: the escaped inner quotes are adjacent to the
    # triple-quote boundary. Round-trip verification of the re-wrapped value
    # catches this where enumerating unsafe characters cannot.
    src = (
        "from __future__ import annotations\n" + _TC_HEAD + "    from pkg.sub.mod import Thing\n"
        "def g(a: '''\\'Thing\\'''') -> None: ...\n"
    )
    result = outcome(src)
    assert result.status == "skipped"
    assert result.source == src
    assert any("string literal" in b.detail for b in result.blockers)


def test_newline_in_annotation_string_blocks_instead_of_erroring():
    # `evaluated_value` decodes the `\n` escape into a real newline, which
    # survives re-rendering. The quote character never appears in the render,
    # so the containment check passes -- and a raw newline gets re-wrapped in
    # single quotes, which does not parse. The general class is "any escape
    # that survives decoding and cannot be re-emitted raw"; the quote
    # character is only one member of it.
    src = (
        "from __future__ import annotations\n" + _TC_HEAD + "    from pkg.sub.mod import Thing\n"
        "def g(a: 'list[\\n\"Thing\"]') -> None: ...\n"
    )
    result = outcome(src)
    assert result.status == "skipped"
    assert result.source == src
    assert any("string literal" in b.detail for b in result.blockers)


def test_triple_quoted_plain_annotation_is_still_rewritten():
    # The round-trip check must not over-block: a triple-quoted annotation
    # with nothing problematic in it re-wraps and round-trips fine.
    src = (
        "from __future__ import annotations\n" + _TC_HEAD + "    from pkg.sub.mod import Thing\n"
        "def g(a: '''Thing''') -> None: ...\n"
    )
    result = outcome(src)
    assert result.status == "fixed"
    assert "def g(a: '''mod.Thing''') -> None: ..." in result.source


# -- final whole-branch review ----------------------------------------------


def test_existing_binding_rebound_at_module_scope_is_not_reused():
    """Critical 2: reusing `path` after `path = "/data"` yields a TypeError."""
    src = 'from os import path\nfrom os.path import join\npath = "/data"\nprint(join(path, "x"))\n'
    result = outcome(src)
    assert result.status == "fixed"
    assert "path.join" not in result.source
    assert "from os import path as path_2" in result.source
    assert 'print(path_2.join(path, "x"))' in result.source


def test_deleting_a_rewritten_local_blocks_the_file():
    """Critical 3: `del Thing` must not become `del mod.Thing`."""
    src = "from pkg.sub.mod import Thing\nx = Thing()\ndel Thing\n"
    result = outcome(src)
    assert result.status == "skipped"
    assert result.source == src, "a blocked file must be byte-identical"
    assert "del" in result.blockers[0].detail


def test_a_function_local_alias_never_leaks_into_a_module_level_annotation():
    """Critical 4: `_string_targets` must be scope-aware."""
    src = (
        "from __future__ import annotations\n"
        "\n"
        "def f():\n"
        "    from pkg.sub.mod import Thing\n"
        "    return Thing()\n"
        "\n"
        "class Thing:\n"
        "    pass\n"
        "\n"
        'def g(x: "Thing") -> None: ...\n'
    )
    result = outcome(src)
    assert "mod.Thing" not in result.source.split("def g")[-1]
    assert result.status == "skipped"
    assert result.source == src


def test_an_annotation_inside_the_importing_scope_is_still_rewritten():
    """The scope key must not over-block: same-scope annotations still fix."""
    src = (
        "from __future__ import annotations\n"
        "\n"
        "def f():\n"
        "    from pkg.sub.mod import Thing\n"
        '    def inner(x: "Thing") -> None: ...\n'
        "    return inner, Thing\n"
    )
    result = outcome(src)
    assert result.status == "fixed"
    assert 'def inner(x: "mod.Thing") -> None: ...' in result.source


def test_a_comment_inside_a_parenthesized_import_blocks_the_file():
    """Important 1: regenerating the kept-names line would discard it."""
    src = (
        "from pkg.sub.mod import (\n"
        "    Thing,   # the important thing\n"
        "    go,\n"
        ")\n"
        "x = Thing()\n"
        "y = go()\n"
    )
    result = outcome(src)
    assert result.status == "skipped"
    assert result.source == src, "a blocked file must be byte-identical"
    assert "comment" in result.blockers[0].detail


def test_a_noqa_comment_on_one_imported_name_blocks_the_file():
    src = "from pkg.sub.mod import (\n    Thing,  # noqa: F401\n)\nx = Thing()\n"
    result = outcome(src)
    assert result.status == "skipped"
    assert result.source == src


def test_an_import_with_no_interior_comment_still_fixes():
    src = "from pkg.sub.mod import (\n    Thing,\n    go,\n)\nx = Thing()\ny = go()\n"
    result = outcome(src)
    assert result.status == "fixed"
    assert result.source == "from pkg.sub import mod\nx = mod.Thing()\ny = mod.go()\n"


def test_trivia_inside_an_annotation_string_is_never_dropped():
    """Important 1 (same family): 'Thing  # note' must not become 'mod.Thing'."""
    src = (
        "from __future__ import annotations\n"
        "from pkg.sub.mod import Thing\n"
        'def f(x: "Thing  # note") -> None: ...\n'
        "y = Thing()\n"
    )
    result = outcome(src)
    assert result.status == "skipped"
    assert result.source == src


def test_a_match_capture_pattern_naming_a_rewritten_local_blocks():
    """Important 2: `case VAL:` binds, it does not compare."""
    src = (
        "from pkg.sub.mod import Thing\n"
        "def f(x):\n"
        "    match x:\n"
        "        case Thing:\n"
        "            return 1\n"
        "    return Thing\n"
    )
    result = outcome(src)
    assert result.status == "skipped"
    assert result.source == src
    assert "capture pattern" in result.blockers[0].detail


def test_a_match_value_pattern_is_still_rewritten():
    """No over-block: `case Thing():` is a genuine value/class reference."""
    src = (
        "from pkg.sub.mod import Thing\n"
        "def f(x):\n"
        "    match x:\n"
        "        case Thing():\n"
        "            return 1\n"
        "    return 0\n"
    )
    result = outcome(src)
    assert result.status == "fixed"
    assert "case mod.Thing():" in result.source


# -- TYPE_CHECKING guards ---------------------------------------------------


def test_a_type_checking_plain_import_is_not_a_runtime_binding():
    """`import x` under `if TYPE_CHECKING:` must not be reused as a binding.

    A TYPE_CHECKING block is `GlobalScope` to libcst exactly like the module
    body, so `_build_existing` used to harvest such an import as an
    already-available module. The fixer then emitted no runtime import at all
    and qualified through a name that does not exist at runtime -- a
    `NameError` on the first call. Found by running `_pytest`'s own suite
    against a rewritten copy of it.
    """
    src = (
        "from __future__ import annotations\n"
        "from typing import TYPE_CHECKING\n"
        "from pkg.sub.mod import Thing\n"
        "if TYPE_CHECKING:\n"
        "    import pkg.sub.mod\n"
        "x = Thing()\n"
    )
    result = outcome(src)
    assert result.status == "fixed"
    runtime = result.source.split("if TYPE_CHECKING:")[0]
    assert "from pkg.sub import mod" in runtime, (
        "the rewrite must emit a runtime import, not lean on the "
        f"TYPE_CHECKING one:\n{result.source}"
    )


def test_a_compound_type_checking_guard_is_not_a_runtime_binding():
    """`if TYPE_CHECKING or X:` is not guaranteed to run, so its imports are not bindings.

    Real shapes: `if TYPE_CHECKING or not install_lazy_importer():` (anyio),
    `if sys.version_info >= (3, 11) or TYPE_CHECKING:` (_pytest). Matching
    only a bare `if TYPE_CHECKING:` let these through, and the fixer then
    qualified references through a name that may never be bound.
    """
    src = (
        "from __future__ import annotations\n"
        "from typing import TYPE_CHECKING\n"
        "OTHER = False\n"
        "from pkg.sub.mod import Thing\n"
        "if TYPE_CHECKING or OTHER:\n"
        "    import pkg.sub.mod\n"
        "x = Thing()\n"
    )
    result = outcome(src)
    assert result.status == "fixed"
    runtime = result.source.split("if TYPE_CHECKING or OTHER:")[0]
    assert "from pkg.sub import mod" in runtime, (
        f"a guard that may not run needs its own runtime import:\n{result.source}"
    )


def test_a_negated_type_checking_guard_is_a_runtime_binding():
    """`if not TYPE_CHECKING:` always runs, so its imports are ordinary bindings.

    The conservative direction would be to treat every mention of
    TYPE_CHECKING as suspect; this one shape is genuinely guaranteed, and
    over-blocking it would decline real files for no safety gain.
    """
    src = (
        "from __future__ import annotations\n"
        "from typing import TYPE_CHECKING\n"
        "from pkg.sub.mod import Thing\n"
        "if not TYPE_CHECKING:\n"
        "    from pkg.sub import mod\n"
        "x = Thing()\n"
    )
    result = outcome(src)
    assert result.status == "fixed"
    assert result.source.count("from pkg.sub import mod") == 1, (
        f"the existing runtime binding should be reused:\n{result.source}"
    )


def test_a_negated_non_type_checking_guard_is_ordinary_code():
    """`if not DEBUG:` has nothing to do with type checking and must still fix.

    Reading every `not` as the `not TYPE_CHECKING` idiom declined perfectly
    ordinary files, with a reason that was false about them.
    """
    src = (
        "import os\n"
        'if not os.environ.get("SKIP"):\n'
        "    from pkg.sub.mod import Thing\n"
        "def use():\n    return Thing()\n"
    )
    result = outcome(src)
    assert result.status == "fixed"
    assert "mod.Thing()" in result.source


def test_the_else_of_a_negated_type_checking_guard_is_not_a_runtime_binding():
    """In `if not TYPE_CHECKING: ... else: <imports>`, the else half is the gated one."""
    src = (
        "from __future__ import annotations\n"
        "from typing import TYPE_CHECKING\n"
        "from pkg.sub.mod import Thing\n"
        "if not TYPE_CHECKING:\n"
        "    pass\n"
        "else:\n"
        "    import pkg.sub.mod\n"
        "x = Thing()\n"
    )
    result = outcome(src)
    assert result.status == "fixed"
    runtime = result.source.split("if not TYPE_CHECKING:")[0]
    assert "from pkg.sub import mod" in runtime, (
        f"the else-branch import is not a runtime binding:\n{result.source}"
    )


def test_an_aliased_type_checking_import_is_still_a_guard():
    """`from typing import TYPE_CHECKING as TC` then `if TC:` is the same guard."""
    src = (
        "from __future__ import annotations\n"
        "from typing import TYPE_CHECKING as TC\n"
        "from pkg.sub.mod import Thing\n"
        "if TC:\n"
        "    import pkg.sub.mod\n"
        "x = Thing()\n"
    )
    result = outcome(src)
    assert result.status == "fixed"
    runtime = result.source.split("if TC:")[0]
    assert "from pkg.sub import mod" in runtime, (
        f"an aliased TYPE_CHECKING guard is not a runtime binding:\n{result.source}"
    )

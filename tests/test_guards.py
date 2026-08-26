"""Whole-file safety predicates, in isolation."""

from __future__ import annotations

import libcst as cst

from cleanporter import guards


def _line_of(tree: cst.Module):
    from libcst.metadata import MetadataWrapper, PositionProvider

    positions = MetadataWrapper(tree, unsafe_skip_copy=True).resolve(PositionProvider)
    return lambda node: positions[node].start.line


def _hits(source: str, names: set[str], **kwargs):
    tree = cst.parse_module(source)
    return guards.find_string_mentions(tree, names, _line_of(tree), **kwargs)


def test_dunder_all_mention_is_a_hit():
    hits = _hits('from a import THING\n__all__ = ["THING"]\n', {"THING"})
    assert len(hits) == 1
    assert hits[0][0] == 2
    assert "string literal" in hits[0][1]


def test_getattr_string_mention_is_a_hit():
    hits = _hits('x = getattr(m, "Widget")\n', {"Widget"})
    assert len(hits) == 1
    assert hits[0][0] == 1
    assert "string literal" in hits[0][1]


def test_substring_is_not_a_hit():
    assert _hits('s = "THINGAMAJIG and SOMETHING"\n', {"THING"}) == []


def test_unrelated_string_is_not_a_hit():
    assert _hits('s = "hello"\n', {"THING"}) == []


def test_no_names_means_no_hits():
    assert _hits('__all__ = ["THING"]\n', set()) == []


def test_skip_ids_exempts_a_specific_string_node():
    tree = cst.parse_module('__all__ = ["THING"]\n')
    strings = [n for n in _walk(tree) if isinstance(n, cst.SimpleString)]
    assert guards.find_string_mentions(
        tree, {"THING"}, _line_of(tree), skip_ids=frozenset({id(strings[0])})
    ) == []


def test_module_docstring_mention_is_not_a_hit():
    assert _hits('"""Wraps Thing nicely."""\nfrom a import Thing\n', {"Thing"}) == []


def test_class_docstring_mention_is_not_a_hit():
    src = 'class C:\n    """Uses Thing internally."""\n'
    assert _hits(src, {"Thing"}) == []


def test_function_docstring_mention_is_not_a_hit():
    src = 'def f():\n    """Returns a Thing."""\n'
    assert _hits(src, {"Thing"}) == []


def test_docstring_with_doctest_is_a_hit():
    src = '"""Example.\n\n>>> Thing()\n"""\n'
    hits = _hits(src, {"Thing"})
    assert len(hits) == 1
    assert hits[0][0] == 1
    assert "string literal" in hits[0][1]


def test_non_docstring_string_is_still_a_hit():
    # Not the first statement of the module -> not a docstring at all.
    hits = _hits('x = 1\n"""Thing"""\n', {"Thing"})
    assert len(hits) == 1
    assert hits[0][0] == 2


def test_assigned_string_mentioning_name_is_still_a_hit():
    hits = _hits('x = "Thing"\n', {"Thing"})
    assert len(hits) == 1
    assert hits[0][0] == 1


def _walk(node):
    yield node
    for child in node.children:
        yield from _walk(child)


def _decl_hits(source: str, names: set[str]):
    tree = cst.parse_module(source)
    return guards.find_scope_declarations(tree, names, _line_of(tree))


def test_global_declaration_is_a_hit():
    hits = _decl_hits("def f():\n    global THING\n    THING = 3\n", {"THING"})
    assert len(hits) == 1
    assert hits[0][0] == 2
    assert "global" in hits[0][1]


def test_nonlocal_declaration_is_a_hit():
    src = "def outer():\n    Widget = 1\n    def inner():\n        nonlocal Widget\n"
    hits = _decl_hits(src, {"Widget"})
    assert len(hits) == 1 and "nonlocal" in hits[0][1]


def test_declaration_of_an_unrelated_name_is_not_a_hit():
    assert _decl_hits("def f():\n    global OTHER\n", {"THING"}) == []


def test_multiple_names_in_one_declaration_are_reported_together():
    hits = _decl_hits("def f():\n    global A, B\n", {"A", "B"})
    assert len(hits) == 1 and "A/B" in hits[0][1]

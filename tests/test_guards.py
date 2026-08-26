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
    assert _hits('x = getattr(m, "Widget")\n', {"Widget"})


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


def _walk(node):
    yield node
    for child in node.children:
        yield from _walk(child)

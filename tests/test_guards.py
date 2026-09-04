"""Whole-file safety predicates, in isolation."""

from __future__ import annotations

import libcst as cst
import pytest

from cleanporter import guards


def _line_of(tree: cst.Module):
    from libcst import metadata

    positions = metadata.MetadataWrapper(tree, unsafe_skip_copy=True).resolve(
        metadata.PositionProvider
    )
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
    assert (
        guards.find_string_mentions(
            tree, {"THING"}, _line_of(tree), skip_ids=frozenset({id(strings[0])})
        )
        == []
    )


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


def test_match_captures_flags_every_binding_form():
    tree = cst.parse_module(
        "def f(x):\n"
        "    match x:\n"
        "        case Thing:\n"
        "            return 1\n"
        "        case [*Other]:\n"
        "            return Other\n"
        "        case {**Rest}:\n"
        "            return Rest\n"
        "        case Thing():\n"
        "            return 2\n"
    )
    hits = guards.find_match_captures(tree, {"Thing", "Other", "Rest"}, lambda n: 0)
    assert sorted({h[1].split("'")[1] for h in hits}) == ["Other", "Rest", "Thing"]
    # the class pattern `case Thing():` is a reference, not a binding
    assert len(hits) == 3


def test_match_captures_ignores_unrelated_names():
    tree = cst.parse_module("def f(x):\n    match x:\n        case other:\n            return 1\n")
    assert guards.find_match_captures(tree, {"Thing"}, lambda n: 0) == []


# -- the reference/prose boundary ------------------------------------------
#
# `find_string_mentions` only reports a word match when the string could
# actually *be* a reference to the name (see `_string_references`). Each case
# below records why it falls on the side it does. The unsafe direction is a
# `must_block` case that stops blocking: that is a rename silently breaking
# working code, so these are the cases to add to rather than relax.

_MUST_BLOCK = [
    ('"Thing"', "an __all__ entry or a getattr argument"),
    ('"  Thing  "', "a padded identifier still evaluates"),
    ('"\\n    Thing\\n"', "a multi-line padded identifier"),
    ('"(Thing)"', "a parenthesized identifier"),
    ('"pkg.mod.Thing"', "a dotted path, as monkeypatch.setattr takes"),
    ('"app.Thing"', "a Django-style lazy model reference"),
    ('"Thing.method"', "the head of a dotted attribute chain"),
    ('"self.Thing"', "an attribute spelled like the name"),
    ('"mypkg.cli:main"', "an entry point address (not valid Python)"),
    ('"list[Thing]"', "an eagerly evaluated string annotation"),
    ('"Optional[Thing]"', "an eagerly evaluated string annotation"),
    ('"dict[str, Thing]"', "an eagerly evaluated string annotation"),
    ('"Thing | None"', "a PEP 604 string annotation"),
    ('"Thing, Other"', "a tuple of names"),
    ('"Thing()"', "an eval payload"),
    ('"x = Thing"', "an exec payload, which is a statement not an expression"),
    ('"if Thing: pass"', "an exec payload"),
    ('"lambda: Thing"', "a lambda body"),
    ("\"Sequence['Thing']\"", "a nested forward reference with no CST node of its own"),
    ("\"Callable[..., 'Thing']\"", "a nested forward reference"),
    ("\"{'a': 'Thing'}\"", "a string nested in a dict literal"),
    ('">>> Thing()"', "a doctest is executable"),
    ('">>> from m import Thing"', "a doctest import"),
    ('"from m import Thing"', "code naming the symbol: a template or exec payload"),
    ('"import Thing"', "code naming the symbol"),
    ('b"Thing"', "a bytes literal cannot be decoded, so it cannot be cleared"),
]

_MUST_NOT_BLOCK = [
    ('"default value must be set"', "prose"),
    ('"the default is 5"', "prose"),
    ('"no default provided"', "prose"),
    ('"Cannot include this file"', "prose"),
    ('"Use include to add files"', "documentation prose"),
    ('"expected Type, got int"', "an error message"),
    ('"deprecated since 2.0"', "prose"),
    ('"SELECT include FROM t"', "SQL"),
    ('"<b>Type</b> here"', "HTML"),
    ('"--include=PATTERN"', "CLI help text"),
    ('"%(Thing)s"', "a printf mapping key, not a name reference"),
    ('r"\\bdefault\\b"', "a regex literal"),
    ('"x = 1  # Thing"', "the name appears only in a comment inside the payload"),
    ('"Content-Type: text/html"', "an HTTP header"),
]


# Cases that block even though no rename could actually reach them. They are
# recorded rather than fixed: each one *does* parse as Python, so clearing it
# would mean deciding by intent rather than by structure, which is the guess
# this guard exists to avoid. Over-blocking costs a declined file; the other
# direction costs broken code.
_ACCEPTED_OVER_BLOCK = [
    ('"Thing-case"', "parses as the subtraction `Thing - case`"),
    ('"{Thing}"', "parses as a set literal, though it is a format field"),
    ('"default, include"', "parses as a tuple of two names"),
]


def _mention_names(literal: str) -> set[str]:
    """Every name the guard would report for ``x = <literal>``."""
    tree = cst.parse_module(f"x = {literal}\n")
    names = {"Thing", "Other", "default", "include", "Type", "deprecated", "main"}
    return {h[1].split("'")[1] for h in guards.find_string_mentions(tree, names, _line_of(tree))}


@pytest.mark.parametrize(("literal", "why"), _MUST_BLOCK, ids=[c[0] for c in _MUST_BLOCK])
def test_a_string_that_could_be_a_reference_blocks(literal: str, why: str) -> None:
    assert _mention_names(literal), f"must block ({why}): {literal}"


@pytest.mark.parametrize(("literal", "why"), _MUST_NOT_BLOCK, ids=[c[0] for c in _MUST_NOT_BLOCK])
def test_prose_that_merely_contains_the_word_does_not_block(literal: str, why: str) -> None:
    assert _mention_names(literal) == set(), f"must not block ({why}): {literal}"


def test_strict_ids_make_a_word_match_enough() -> None:
    """A string proven to be code by context blocks even if it cannot parse.

    `"Thing["` is prose-shaped to any content inspection -- it does not parse
    and is not a reference path -- but in an annotation slot it is a
    malformed type, not prose. Only the caller knows the slot, so the caller
    marks it.
    """
    tree = cst.parse_module("def f(a: 'Thing[') -> None: ...\n")
    strings = [n for n in _walk(tree) if isinstance(n, cst.SimpleString)]
    assert guards.find_string_mentions(tree, {"Thing"}, _line_of(tree)) == []
    hits = guards.find_string_mentions(
        tree, {"Thing"}, _line_of(tree), strict_ids=frozenset({id(strings[0])})
    )
    assert len(hits) == 1


@pytest.mark.parametrize(
    ("literal", "why"), _ACCEPTED_OVER_BLOCK, ids=[c[0] for c in _ACCEPTED_OVER_BLOCK]
)
def test_accepted_over_blocking_is_deliberate(literal: str, why: str) -> None:
    """Declining a file is the safe direction; do not relax these."""
    assert _mention_names(literal), f"expected to block ({why}): {literal}"


def test_a_string_nested_two_levels_deep_still_blocks() -> None:
    """Recursion follows forward references that have no CST node of their own."""
    assert _mention_names('"Sequence[\\"Sequence[\'Thing\']\\"]"') == {"Thing"}


def test_a_bytes_literal_is_reported_as_such() -> None:
    tree = cst.parse_module('x = b"Thing"\n')
    hits = guards.find_string_mentions(tree, {"Thing"}, _line_of(tree))
    assert len(hits) == 1
    assert "bytes literal" in hits[0][1]


def test_global_and_match_names_inside_a_payload_block() -> None:
    """Binding forms inside an exec payload name the symbol just as a read does."""
    assert _mention_names('"global Thing"') == {"Thing"}
    assert _mention_names('"match x:\\n    case Thing:\\n        pass"') == {"Thing"}

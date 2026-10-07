# Copyright (c) 2026 grAItools
# SPDX-License-Identifier: BSD-3-Clause
# See LICENSE for the full license text.

"""`analyze.FileFacts`: one walk, the same answers as the walks it replaced.

The oracles below are the per-question visitors `analyze` used before the
facts were collected in a single pass, kept verbatim so the two can be
compared on real code: every Python file in this repository, which between
them hold relative and absolute imports, aliases, star imports, attribute
chains through calls and subscripts, and imports nested in functions,
classes, ``try`` and ``if TYPE_CHECKING``.
"""

from __future__ import annotations

import ast
import pathlib

import libcst as cst
import pytest
from libcst import metadata

from cleanporter import _imports, analyze

REPO = pathlib.Path(__file__).parent.parent
#: The package's own source and the fixtures: every shape listed above, at a
#: cost the suite can pay on every run.
FILES = sorted(
    [*(REPO / "src" / "cleanporter").rglob("*.py"), *(REPO / "tests" / "fixtures").rglob("*.py")],
    key=str,
)
#: Unanchored (every relative import unresolvable) and anchored deep enough
#: for ``from ..x import y`` to resolve.
BASES = ("", "pkg.sub")


def _import_froms(tree: cst.Module) -> list[cst.ImportFrom]:
    found: list[cst.ImportFrom] = []

    class V(cst.CSTVisitor):
        def visit_ImportFrom(self, node: cst.ImportFrom) -> None:
            found.append(node)

    tree.visit(V())
    return found


def _heads(tree: cst.Module) -> set[str]:
    heads: set[str] = set()

    class V(cst.CSTVisitor):
        def visit_ImportFrom(self, node: cst.ImportFrom) -> None:
            if _imports.relative_level(node) == 0:
                heads.add(_imports.dotted(node.module).split(".")[0])

        def visit_Import(self, node: cst.Import) -> None:
            for alias in node.names:
                heads.add(_imports.dotted(alias.name).split(".")[0])

    tree.visit(V())
    heads.discard("")
    return heads


def _bindings(tree: cst.Module, base_pkg: str) -> dict[str, str]:
    bound: dict[str, str] = {}

    class V(cst.CSTVisitor):
        def visit_Import(self, node: cst.Import) -> None:
            for alias in node.names:
                dotted = _imports.dotted(alias.name)
                if alias.asname is not None and isinstance(alias.asname.name, cst.Name):
                    bound[alias.asname.name.value] = dotted
                else:
                    head = dotted.split(".")[0]
                    bound[head] = head

        def visit_ImportFrom(self, node: cst.ImportFrom) -> None:
            parent = _imports.resolve_parent(node, base_pkg)
            if parent is None:
                return
            for name, asname, _alias in _imports.imported_names(node):
                bound[asname or name] = f"{parent}.{name}"

    tree.visit(V())
    return bound


def _attribute_pairs(tree: cst.Module, bound: dict[str, str]) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()

    class V(cst.CSTVisitor):
        def visit_Attribute(self, node: cst.Attribute) -> None:
            try:
                prefix = _imports.dotted(node.value)
            except TypeError:
                return
            head, _dot, rest = prefix.partition(".")
            target = bound.get(head)
            if target is None:
                return
            module = f"{target}.{rest}" if rest else target
            found.add((module, node.attr.value))

    tree.visit(V())
    return found


def _stars(tree: cst.Module, base_pkg: str) -> set[str]:
    found: set[str] = set()
    for node in _import_froms(tree):
        if _imports.is_star(node):
            parent = _imports.resolve_parent(node, base_pkg)
            if parent is not None:
                found.add(parent)
    return found


def _starts_from_metadata(tree: cst.Module) -> dict[cst.ImportFrom, tuple[int, int]]:
    positions = metadata.MetadataWrapper(tree, unsafe_skip_copy=True).resolve(
        metadata.PositionProvider
    )
    return {
        node: (positions[node].start.line, positions[node].start.column)
        for node in _import_froms(tree)
    }


def _nested(source: str) -> list[bool]:
    """Per ``from`` import in source order: is it inside a ``def`` or ``class`` body?"""
    found: list[tuple[int, int, bool]] = []
    scopes = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)

    def walk(node: ast.AST, *, nested: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ImportFrom):
                found.append((child.lineno, child.col_offset, nested))
            walk(child, nested=nested or isinstance(child, scopes))

    walk(ast.parse(source), nested=False)
    return [flag for _line, _col, flag in sorted(found)]


@pytest.mark.parametrize("path", FILES, ids=lambda p: str(p.relative_to(REPO)))
def test_facts_match_the_walks_they_replaced(path: pathlib.Path) -> None:
    source = path.read_text(encoding="utf-8")
    tree = cst.parse_module(source)
    facts = analyze.collect_facts(tree)

    assert list(facts.import_froms) == _import_froms(tree)
    assert [n in facts.nested_import_froms for n in facts.import_froms] == _nested(source)
    assert facts.absolute_import_heads() == _heads(tree)
    assert facts.max_relative_level() == max(
        (_imports.relative_level(n) for n in _import_froms(tree)), default=0
    )
    for base in BASES:
        bound = _bindings(tree, base)
        assert list(facts.module_bindings(base).items()) == list(bound.items()), "same order"
        assert facts.attribute_pairs(base) == _attribute_pairs(tree, bound)
        assert facts.star_imported_modules(base) == _stars(tree, base)

    starts = analyze._import_from_starts(source, tree, facts.import_froms)
    assert starts is not None, "a plain LF file should take the fast path"
    assert starts == _starts_from_metadata(tree)


TRICKY = {
    "empty": "",
    "none": "import os\nx = os.sep\n",
    "semicolons": "x = 1; from a import b; from c import d\n",
    "wide_chars_before": "é = 'ñ'; from a import b\nπ = 3; from .c import (\n    d,\n)\n",
    "astral": "s = '\U0001f600'; from a import b\n",
    "tabs": "if True:\n\tfrom a import b\n\tif x:\n\t\tfrom c import d\n",
    "nested": "def f():\n    class C:\n        from a import b\n    return C\n",
    "continuation": "x = 1 + \\\n    2; from a import b\nfrom \\\n  c import d\n",
    "text_that_looks_like_one": 's = """\nfrom a import b\n"""\nfrom c import d\n',
    "no_trailing_newline": "from a import b",
    "form_feed_own_line": "\x0c\nfrom a import b\n",
    # A form feed or a backslash continuation in a statement's leading
    # whitespace: libcst 1.9 drops it from the code it generates, so its
    # column -- or its line -- is not where the keyword sits in the source.
    "form_feed_before_from": "\x0cfrom a import b\n",
    "form_feed_after_indent": "  \x0cfrom a import b",
    "form_feed_second_line": "x = 1\n\x0cfrom a import b",
    "form_feed_before_semicolon": "\x0cx = 1; from a import b",
    "form_feed_after_semicolon": "x = 1;\x0cfrom a import b\n",
    "continuation_before_statement": "\\\nfrom a import b\n",
    "continuation_before_dedent": "if x:\n    pass\n    \\\n  from a import b\n",
    "type_checking": "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from a import b\n",
    "invalid_escape": "p = '\\d'\nfrom a import b\n",
    "star_and_relative": "from . import *\nfrom ..x import y as z\n",
}


@pytest.mark.parametrize("source", TRICKY.values(), ids=TRICKY.keys())
def test_import_starts_on_awkward_sources(source: str) -> None:
    tree = cst.parse_module(source)
    rec = analyze.FileRecord(pathlib.Path("a.py"), source, tree, "")
    fast = analyze._import_from_starts(source, tree, rec.facts.import_froms)
    expected = _starts_from_metadata(tree)
    assert fast is None or fast == expected, "the fast path is libcst's answer or none"
    assert dict(rec.import_starts) == expected


@pytest.mark.parametrize(
    "source",
    [
        "from a import b\r\nfrom c import d\r\n",  # CRLF: declined outright
        "x = 1\rfrom a import b\n",  # a lone CR is a newline to both, but not proven
        "\ufefffrom a import b\n",  # a BOM left in the text: the keyword check declines
        "match = 1\nprint(f'{match!r:{\"x\"}}')\nfrom a import b\n",
    ],
    ids=["crlf", "lone_cr", "bom", "fstring"],
)
def test_the_record_falls_back_to_metadata_when_unsure(source: str) -> None:
    """Whatever the fast path does with these, the record's answer is libcst's."""
    tree = cst.parse_module(source)
    rec = analyze.FileRecord(pathlib.Path("a.py"), source, tree, "")
    assert dict(rec.import_starts) == _starts_from_metadata(tree)


def test_a_crlf_file_is_never_read_off_ast() -> None:
    source = "from a import b\r\nfrom c import d\r\n"
    tree = cst.parse_module(source)
    nodes = analyze.collect_facts(tree).import_froms
    assert analyze._import_from_starts(source, tree, nodes) is None


@pytest.mark.parametrize(
    "source",
    ["\x0cfrom a import b\n", "\\\nfrom a import b\n"],
    ids=["form_feed", "continuation"],
)
def test_code_libcst_does_not_reproduce_is_never_read_off_ast(source: str) -> None:
    tree = cst.parse_module(source)
    assert tree.code != source, "the premise: libcst drops this whitespace"
    nodes = analyze.collect_facts(tree).import_froms
    assert analyze._import_from_starts(source, tree, nodes) is None


def test_a_file_ast_cannot_parse_is_declined() -> None:
    """libcst accepts grammar the running ``ast`` may not; that must not guess."""
    source = 'from a import b\nx = t"template"\n'  # a 3.14 t-string
    tree = cst.parse_module(source)
    try:
        ast.parse(source)
    except SyntaxError:
        pass
    else:  # pragma: no cover - a Python that accepts t-strings
        pytest.skip("this interpreter parses t-strings")
    nodes = analyze.collect_facts(tree).import_froms
    assert analyze._import_from_starts(source, tree, nodes) is None


def test_a_count_mismatch_is_declined() -> None:
    source = "from a import b\nfrom c import d\n"
    tree = cst.parse_module(source)
    nodes = analyze.collect_facts(tree).import_froms
    assert analyze._import_from_starts(source, tree, nodes[:1]) is None


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("from a import b\ntry:\n    from c import d\nexcept ImportError:\n    pass\n", [0, 0]),
        ("if x:\n    from a import b\nwith y:\n    from c import d\n", [0, 0]),
        ("def f():\n    from a import b\n", [1]),
        ("async def f():\n    from a import b\n", [1]),
        ("class C:\n    from a import b\n", [1]),
        (
            "def f():\n    class C:\n        from a import b\n    return C\nfrom c import d\n",
            [1, 0],
        ),
    ],
    ids=["try", "if-with", "def", "async-def", "class", "nested-then-module"],
)
def test_nested_imports_are_those_inside_a_def_or_class(source: str, expected: list[int]) -> None:
    """Module level is everything outside a ``def``/``class`` body, blocks included."""
    facts = analyze.collect_facts(cst.parse_module(source))
    flags = [n in facts.nested_import_froms for n in facts.import_froms]
    assert flags == [bool(e) for e in expected] == _nested(source)


def test_record_facts_are_collected_once() -> None:
    source = "from a import b\n"
    rec = analyze.FileRecord(pathlib.Path("a.py"), source, cst.parse_module(source), "")
    assert rec.facts is rec.facts
    assert rec.import_starts is rec.import_starts


def _plain_starts_from_metadata(tree: cst.Module) -> dict[cst.Import, tuple[int, int]]:
    positions = metadata.MetadataWrapper(tree, unsafe_skip_copy=True).resolve(
        metadata.PositionProvider
    )
    return {
        node: (positions[node].start.line, positions[node].start.column)
        for node in analyze.collect_facts(tree).plain_imports
    }


@pytest.mark.parametrize(
    "source",
    [
        *TRICKY.values(),
        "import a\nx = 1; import b as c\n",
        "é = 'ñ'; import a.b\n",
        "def f():\n    import a\n",
        "\x0cimport a\n",
        "import a\r\nimport b\r\n",
        "x = 1 + \\\n    2; import a\nimport \\\n  b\n",
    ],
)
def test_plain_import_starts_are_libcsts(source: str) -> None:
    """`FileRecord.plain_import_starts` takes the same fast path, and gives libcst's answer."""
    tree = cst.parse_module(source)
    rec = analyze.FileRecord(pathlib.Path("a.py"), source, tree, "")
    assert dict(rec.plain_import_starts) == _plain_starts_from_metadata(tree)


def test_nested_plain_imports_are_recorded() -> None:
    facts = analyze.collect_facts(
        cst.parse_module("import a\n\n\ndef f():\n    import b\n\n\nclass C:\n    import c\n")
    )
    assert [n in facts.nested_plain_imports for n in facts.plain_imports] == [False, True, True]

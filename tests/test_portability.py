"""The suite writes the bytes it later compares, on every platform.

`pathlib.Path.write_text` translates ``\\n`` to ``os.linesep`` unless it is
given ``newline=``, so on Windows every source file a test wrote came out
CRLF -- and the fixer, which preserves a file's line endings, then produced
CRLF output that no expected string in the suite spells. Twelve tests failed
that way on Windows and on nothing else. Every ``write_text`` in `tests/`
therefore passes ``newline="\\n"`` (or, for CRLF on purpose, the test writes
bytes), and this test keeps it that way.
"""

from __future__ import annotations

import ast
import pathlib

TESTS = pathlib.Path(__file__).resolve().parent


def _unpinned_writes(source: str) -> list[int]:
    """Lines of the ``.write_text(...)`` calls in *source* with no ``newline=``."""
    return [
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "write_text"
        and not any(keyword.arg == "newline" for keyword in node.keywords)
    ]


def test_every_write_text_in_the_suite_pins_its_newlines() -> None:
    offenders = [
        f"{path.name}:{line}"
        for path in sorted(TESTS.glob("*.py"))
        for line in _unpinned_writes(path.read_text(encoding="utf-8"))
    ]
    assert offenders == [], 'pass newline="\\n" (Windows would write CRLF): ' + ", ".join(offenders)


def test_the_check_sees_a_write_without_newline() -> None:
    assert _unpinned_writes('p.write_text("x\\n", encoding="utf-8")\n') == [1]
    assert _unpinned_writes('p.write_text("x\\n", encoding="utf-8", newline="\\n")\n') == []


def _surrogate_literals(source: str) -> list[int]:
    """Lines of the string literals in *source* that hold a lone surrogate."""
    return [
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and any("\ud800" <= char <= "\udfff" for char in node.value)
    ]


def test_no_module_carries_a_lone_surrogate_in_a_string() -> None:
    # Python 3.13 cannot import a module whose docstring holds one (a
    # ``\udcff`` escape where ``\\udcff`` was meant): it fails encoding it.
    offenders = [
        f"{path.relative_to(TESTS.parent)}:{line}"
        for path in sorted((TESTS.parent / "src").rglob("*.py"))
        for line in _surrogate_literals(path.read_text(encoding="utf-8"))
    ]
    assert offenders == [], "escape the backslash of a \\uDxxx escape: " + ", ".join(offenders)

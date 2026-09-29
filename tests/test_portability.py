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

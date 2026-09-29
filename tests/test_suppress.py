"""Inline ``# cleanporter: ignore[...]`` suppressions, and `CP005` for unused ones."""

from __future__ import annotations

import json
import pathlib

import libcst as cst
import pytest
from libcst import metadata

from cleanporter import cli, suppress

HELPERS = "THING = 42\n\n\ndef go():\n    return 1\n\n\nclass Widget:\n    pass\n"


@pytest.fixture
def project(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """A one-package project, with the cwd at its root."""
    (tmp_path / "src" / "demo").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0"\n', encoding="utf-8", newline="\n"
    )
    (tmp_path / "src" / "demo" / "__init__.py").write_text("", encoding="utf-8", newline="\n")
    (tmp_path / "src" / "demo" / "helpers.py").write_text(HELPERS, encoding="utf-8", newline="\n")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _consumer(project: pathlib.Path, source: str) -> pathlib.Path:
    target = project / "src" / "demo" / "consumer.py"
    target.write_text(source, encoding="utf-8", newline="\n")
    return target


def _run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    rc = cli.main(list(argv))
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def _findings(out: str) -> list[tuple[int, str]]:
    """``(line, code)`` of every finding line printed for consumer.py."""
    found: list[tuple[int, str]] = []
    for text in out.splitlines():
        head, sep, rest = text.partition(": CP")
        if sep and "consumer.py:" in head and not text.startswith("cleanporter:"):
            line = int(head.split(":")[-2])
            found.append((line, "CP" + rest[:3]))
    return found


# -- the grammar ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("comment", "codes"),
    [
        ("# cleanporter: ignore[CP001]", ("CP001",)),
        ("# cleanporter: ignore[CP001, CP002]", ("CP001", "CP002")),
        ("#cleanporter:ignore[ CP003 ,CP001 ]", ("CP003", "CP001")),
        ("# cleanporter: ignore[CP001, CP001]", ("CP001",)),
        ("# noqa: F401  # cleanporter: ignore[CP001]", ("CP001",)),
        ("# cleanporter: ignore[CP002]  -- the vendored copy is not importable", ("CP002",)),
        ("#  cleanporter:  ignore[CP001]", ("CP001",)),
    ],
)
def test_a_well_formed_suppression_parses(comment: str, codes: tuple[str, ...]) -> None:
    assert suppress.parse(comment) == (codes, [])


@pytest.mark.parametrize(
    "comment", ["# an ordinary comment", "# see the cleanporter docs", "# noqa: F401"]
)
def test_a_comment_without_a_directive_is_not_one(comment: str) -> None:
    assert suppress.parse(comment) == (None, [])


@pytest.mark.parametrize(
    ("comment", "why"),
    [
        ("# cleanporter: ignore", "bare"),
        ("# cleanporter: ignore  # because", "bare"),
        ("# cleanporter: ignore[]", "has an empty code"),
        ("# cleanporter: ignore[CP001,]", "has an empty code"),
        ("# cleanporter: ignore[cp001]", "not a finding code"),
        ("# Cleanporter: ignore[CP001]", "is not a suppression"),
        ("# CLEANPORTER: ignore[CP001]", "is not a suppression"),
        ("# cleanporter : ignore[CP001]", "is not a suppression"),
        ("# cleanporter: IGNORE[CP001]", "is not a suppression"),
        ("# cleanporter: ignore[CP001]: because", "is not a suppression"),
        ("# cleanporter: ignore[F401]", "not a finding code"),
        ("# cleanporter: ignore[CP009]", "not a finding code a suppression can name"),
        ("# cleanporter: ignore[CP004]", "already a skip"),
        ("# cleanporter: ignore[CP001, CP005]", "cannot itself be suppressed"),
        ("# cleanporter: ignore CP001", "is not a suppression"),
        ("# cleanporter: ignored[CP001]", "is not a suppression"),
        ("# cleanporter: ignore[CP001]x", "is not a suppression"),
        ("# cleanporter: noqa", "is not a suppression"),
    ],
)
def test_a_malformed_suppression_suppresses_nothing(comment: str, why: str) -> None:
    codes, problems = suppress.parse(comment)
    assert codes is None
    assert len(problems) == 1
    assert why in problems[0]


def test_one_bad_directive_voids_the_whole_comment() -> None:
    codes, problems = suppress.parse("# cleanporter: ignore[CP001]  # cleanporter: ignore")
    assert codes is None
    assert len(problems) == 1


def _collect(source: str) -> tuple[cst.Module, suppress.Suppressions]:
    tree = cst.parse_module(source)
    positions = metadata.MetadataWrapper(tree, unsafe_skip_copy=True).resolve(
        metadata.PositionProvider
    )
    return tree, suppress.collect(tree, positions)


def _covered(source: str) -> dict[str, list[int]]:
    """Imported name -> the lines of the suppressions covering it."""
    tree, found = _collect(source)
    out: dict[str, list[int]] = {}

    class Walk(cst.CSTVisitor):
        def visit_ImportFrom(self, node: cst.ImportFrom) -> None:
            assert not isinstance(node.names, cst.ImportStar)
            for alias in node.names:
                name = alias.name
                assert isinstance(name, cst.Name)
                out[name.value] = [s.line for s in found.covering(node, alias)]

    tree.visit(Walk())
    return out


def test_a_trailing_comment_covers_every_name_of_the_statement() -> None:
    source = "from a import B, C  # cleanporter: ignore[CP001]\nfrom a import D\n"
    assert _covered(source) == {"B": [1], "C": [1], "D": []}


def test_the_first_line_of_a_parenthesised_import_covers_the_statement() -> None:
    source = "from a import (  # cleanporter: ignore[CP001]\n    B,\n    C,\n)\n"
    assert _covered(source) == {"B": [1], "C": [1]}


def test_a_later_line_covers_only_the_names_written_on_it() -> None:
    source = (
        "from a import (\n"
        "    B,  # cleanporter: ignore[CP001]\n"
        "    C, D,  # cleanporter: ignore[CP002]\n"
        "    E  # cleanporter: ignore[CP003]\n"
        ")  # cleanporter: ignore[CP001]\n"
    )
    assert _covered(source) == {"B": [2], "C": [3], "D": [3], "E": [4]}


def test_a_backslash_continuation_is_a_later_line_too() -> None:
    source = "from a import B, \\\n    C  # cleanporter: ignore[CP001]\n"
    assert _covered(source) == {"B": [], "C": [2]}


def test_a_suppression_inside_a_string_is_not_a_comment() -> None:
    _tree, found = _collect('x = "# cleanporter: ignore[CP001]"\n')
    assert found.comments == ()


def test_malformed_comments_are_kept_as_problems_with_their_line() -> None:
    _tree, found = _collect("import os\n\nx = 1  # cleanporter: ignore\n")
    assert found.comments == ()
    assert [line for line, _message in found.problems] == [3]


# -- check -----------------------------------------------------------------------


def test_a_suppressed_cp001_is_cp004_and_the_run_passes(
    project: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _consumer(project, "from demo.helpers import THING  # cleanporter: ignore[CP001]\nx = THING\n")
    rc, out, _ = _run(capsys, "src")
    assert rc == 0
    assert _findings(out) == []
    assert "1 skipped by config" in out
    rc, out, _ = _run(capsys, "--show-skipped", "src")
    assert rc == 0
    assert _findings(out) == [(1, "CP004")]
    assert "CP001 suppressed by the inline comment on line 1" in out


def test_a_suppression_covers_only_the_codes_it_names(
    project: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _consumer(
        project,
        "from demo.helpers import THING  # cleanporter: ignore[CP002]\nx = THING\n",
    )
    rc, out, _ = _run(capsys, "src")
    assert rc == 1
    assert sorted(_findings(out)) == [(1, "CP001"), (1, "CP005")]
    assert "no CP002 finding on the imports this comment covers; remove the comment" in out


def test_one_comment_can_name_several_codes(
    project: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _consumer(
        project,
        "from demo.helpers import THING  # cleanporter: ignore[CP001, CP002]\n"
        "from definitely_missing_pkg_xyz import other  # cleanporter: ignore[CP001, CP002]\n"
        "x = THING, other\n",
    )
    rc, out, _ = _run(capsys, "--strict", "--show-skipped", "src")
    # Each comment used one of its codes; the other is unused on that line.
    assert sorted(_findings(out)) == [(1, "CP004"), (1, "CP005"), (2, "CP004"), (2, "CP005")]
    assert "no CP002 finding on the imports this comment covers; remove CP002 from it" in out
    assert rc == 1


def test_a_cp002_can_be_suppressed(
    project: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _consumer(
        project,
        "from definitely_missing_pkg_xyz import other  # cleanporter: ignore[CP002]\nx = other\n",
    )
    rc, out, _ = _run(capsys, "--strict", "--show-skipped", "src")
    assert rc == 0
    assert _findings(out) == [(1, "CP004")]
    assert "CP002 suppressed by the inline comment on line 1" in out


def test_a_cp003_can_be_suppressed(
    project: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _consumer(project, "from demo.helpers import *  # cleanporter: ignore[CP003]\n")
    rc, out, _ = _run(capsys, "src")
    assert rc == 0
    assert _findings(out) == []


def test_per_name_suppression_in_a_parenthesised_import(
    project: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _consumer(
        project,
        "from demo.helpers import (\n"
        "    THING,  # cleanporter: ignore[CP001]\n"
        "    go,\n"
        ")\n"
        "x = THING, go()\n",
    )
    rc, out, _ = _run(capsys, "--show-skipped", "src")
    assert rc == 1
    # Both findings sit at the statement's line; one is suppressed.
    assert sorted(_findings(out)) == [(1, "CP001"), (1, "CP004")]
    assert "'THING' from 'demo.helpers' skipped by configuration" in out
    assert "imports object 'go'" in out


@pytest.mark.parametrize(
    "source",
    [
        # A comment line of its own does not reach the import below it.
        "# cleanporter: ignore[CP001]\nfrom demo.helpers import THING\nx = THING\n",
        # Nor does one on the closing parenthesis.
        "from demo.helpers import (\n    THING,\n)  # cleanporter: ignore[CP001]\nx = THING\n",
    ],
)
def test_a_comment_covering_nothing_is_unused(
    project: pathlib.Path, capsys: pytest.CaptureFixture[str], source: str
) -> None:
    _consumer(project, source)
    rc, out, _ = _run(capsys, "src")
    assert rc == 1
    codes = [code for _line, code in _findings(out)]
    assert sorted(codes) == ["CP001", "CP005"]
    assert "1 unused suppression(s)" in out


def test_an_unused_suppression_alone_fails_the_run(
    project: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _consumer(project, "from demo import helpers  # cleanporter: ignore[CP001]\nx = helpers\n")
    rc, out, _ = _run(capsys, "src")
    assert rc == 1
    assert _findings(out) == [(1, "CP005")]
    assert "consumer.py:1:26: CP005 unused suppression: " in out


def test_a_suppression_on_a_package_surface_import_is_unused(
    project: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A package ``__init__``'s module-level import is compliant: nothing to suppress.

    Its function-local one is an ordinary import, so its comment is used.
    """
    init = project / "src" / "demo" / "__init__.py"
    source = (
        "from demo.helpers import THING  # cleanporter: ignore[CP001]\n"
        "\n"
        "\n"
        "def later():\n"
        "    from demo.helpers import go  # cleanporter: ignore[CP001]\n"
        "\n"
        "    return go()\n"
    )
    init.write_text(source, encoding="utf-8", newline="\n")
    rc, out, _ = _run(capsys, "--show-skipped", "src")
    assert rc == 1
    found = sorted(
        line.split(": ")[1].split()[0] + "@" + line.split(":")[1]
        for line in out.splitlines()
        if "__init__.py:" in line
    )
    assert found == ["CP004@5", "CP005@1"]
    rc, _out, _err = _run(capsys, "--fix", "src")
    assert rc == 1
    assert init.read_text(encoding="utf-8") == source


def test_fix_rewrites_a_local_import_beside_a_surface_suppression(
    project: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The unused module-level comment neither blocks the fix nor moves.

    Its import is the package's surface, so it is never a candidate; the
    function-local `CP001` is, and is rewritten with no `CP003` decline.
    """
    init = project / "src" / "demo" / "__init__.py"
    surface = "from demo.helpers import THING  # cleanporter: ignore[CP001]\n"
    init.write_text(
        surface + "\n\ndef later():\n    from demo.helpers import go\n\n    return go()\n",
        encoding="utf-8",
        newline="\n",
    )
    rc, out, err = _run(capsys, "--fix", "src")
    assert rc == 1  # the CP005 remains
    assert init.read_text(encoding="utf-8") == (
        surface + "\n\ndef later():\n    from demo import helpers\n\n    return helpers.go()\n"
    )
    assert "CP003" not in out + err
    reported = [line for line in (out + err).splitlines() if "__init__.py:" in line]
    assert len(reported) == 1
    assert ":1:" in reported[0]
    assert "CP005" in reported[0]


@pytest.mark.parametrize(
    ("comment", "why"),
    [
        ("# cleanporter: ignore", "bare `# cleanporter: ignore`"),
        ("# cleanporter: ignore[CP042]", "CP042"),
        ("# cleanporter: ignore[CP004]", "CP004 is already a skip"),
    ],
)
def test_a_malformed_suppression_is_a_warning_and_suppresses_nothing(
    project: pathlib.Path, capsys: pytest.CaptureFixture[str], comment: str, why: str
) -> None:
    _consumer(project, f"import os\n\nfrom demo.helpers import THING  {comment}\nx = THING, os\n")
    rc, out, err = _run(capsys, "src")
    assert rc == 1
    assert _findings(out) == [(3, "CP001")]
    warning = next(line for line in (out + err).splitlines() if "suppresses nothing" in line)
    assert warning.startswith("cleanporter: warning: ")
    assert f"{pathlib.PurePath('src', 'demo', 'consumer.py')}:3: " in warning
    assert why in warning


# -- --fix -----------------------------------------------------------------------


def test_fix_keeps_a_suppressed_import_and_fixes_the_rest(
    project: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    kept = "from demo.helpers import THING  # cleanporter: ignore[CP001]\n"
    target = _consumer(project, kept + "from demo.helpers import go\nx = THING, go()\n")
    rc, _out, err = _run(capsys, "--fix", "--show-skipped", "src")
    assert rc == 0
    assert target.read_text(encoding="utf-8") == (
        kept + "from demo import helpers\nx = THING, helpers.go()\n"
    )
    assert [code for _line, code in _findings(err)] == ["CP004"]


def test_fix_leaves_a_fully_suppressed_file_byte_identical(
    project: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = (
        "from demo.helpers import (  # cleanporter: ignore[CP001]\n"
        "    THING,\n"
        "    go,\n"
        ")\n"
        "x = THING, go()\n"
    )
    target = _consumer(project, source)
    rc, _out, _err = _run(capsys, "--fix", "src")
    assert rc == 0
    assert target.read_bytes() == source.encode()


def test_fix_declines_a_statement_holding_a_per_name_comment(
    project: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The fixer never discards a comment; a per-name one cannot survive a rewrite."""
    source = (
        "from demo.helpers import (\n"
        "    THING,  # cleanporter: ignore[CP001]\n"
        "    go,\n"
        ")\n"
        "x = THING, go()\n"
    )
    target = _consumer(project, source)
    rc, _out, err = _run(capsys, "--fix", "src")
    assert rc == 1
    assert target.read_bytes() == source.encode()
    assert "rewriting this import would discard a comment inside it" in err


def test_check_and_fix_agree_on_a_never_read_import(
    project: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Never read: check's `CP001`, the fixer's `CP003`. One `ignore[CP001]` covers both."""
    source = "from demo.helpers import THING  # cleanporter: ignore[CP001]\n"
    target = _consumer(project, source)
    for mode in ([], ["--diff"], ["--fix"]):
        rc, out, err = _run(capsys, *mode, "--show-skipped", "src")
        assert rc == 0, mode
        assert [code for _line, code in _findings(out + err)] == ["CP004"], mode
    assert target.read_bytes() == source.encode()


_MOVED = "the rewrite would move this suppression comment onto imports it does not cover now"


def test_fix_declines_moving_a_comment_onto_the_module_import_it_writes(
    project: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A stale comment would land on the new ``from demo import helpers`` line."""
    source = "from demo.helpers import go  # cleanporter: ignore[CP002]\nx = go()\n"
    target = _consumer(project, source)
    rc, _out, err = _run(capsys, "--fix", "src")
    assert rc == 1
    assert target.read_bytes() == source.encode()
    assert _MOVED in err
    assert sorted(code for _line, code in _findings(err)) == ["CP001", "CP003", "CP005"]


#: Counterexamples: a partial rewrite re-renders the kept names on one line
#: with the statement's trailing comment, which would then cover them.
_MOVING = {
    # A: the comment covers only `Widget`, the name being rewritten.
    "continuation": (
        "from demo.helpers import mystery, \\\n"
        "    Widget  # cleanporter: ignore[CP002]\n"
        "\n"
        "print(Widget, mystery)\n"
    ),
    # B: the comment on the closing parenthesis covers nothing at all.
    "closing-paren": (
        "from demo.helpers import (\n"
        "    mystery,\n"
        "    Widget,\n"
        ")  # cleanporter: ignore[CP002]\n"
        "\n"
        "print(Widget, mystery)\n"
    ),
    # d1: two imports bound to one name, told apart by what they import.
    "same-binding": (
        "from demo.helpers import mystery as m, \\\n"
        "    Widget, other as m  # cleanporter: ignore[CP002]\n"
        "\n"
        "print(Widget, m)\n"
    ),
    # d5: the same import twice; the comment covers only the second.
    "same-import-twice": (
        "from demo.helpers import mystery, \\\n"
        "    Widget, mystery  # cleanporter: ignore[CP002]\n"
        "\n"
        "print(Widget, mystery)\n"
    ),
}

#: What plain ``--strict`` check reports for each `_MOVING` source.
_MOVING_CHECK = {
    "continuation": ["CP001", "CP002", "CP005"],
    "closing-paren": ["CP001", "CP002", "CP005"],
    "same-binding": ["CP001", "CP002", "CP004"],
    "same-import-twice": ["CP001", "CP002", "CP004"],
}


@pytest.mark.parametrize("case", list(_MOVING))
def test_fix_never_moves_a_suppression_onto_kept_names(
    project: pathlib.Path, capsys: pytest.CaptureFixture[str], case: str
) -> None:
    """``--fix --strict`` must not turn the failing run green by re-aiming a comment."""
    source = _MOVING[case]
    target = _consumer(project, source)
    rc_check, out, _ = _run(capsys, "--strict", "--show-skipped", "src")
    check = sorted(code for _line, code in _findings(out))
    assert rc_check == 1
    assert check == _MOVING_CHECK[case]
    rc_fix, _out, err = _run(capsys, "--fix", "--strict", "--show-skipped", "src")
    assert rc_fix == 1
    assert target.read_bytes() == source.encode()
    assert _MOVED in err
    # Check and fix agree on everything but the fixer's own explanation.
    fix = sorted(code for _line, code in _findings(err))
    assert fix == sorted([*check, "CP003"])


def test_a_comment_that_keeps_covering_the_same_names_does_not_block(
    project: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The kept names, and only they, are still covered after the rewrite."""
    source = (
        "from demo.helpers import mystery, Widget  # cleanporter: ignore[CP002]\n"
        "print(Widget, mystery)\n"
    )
    target = _consumer(project, source)
    rc, _out, _err = _run(capsys, "--fix", "--strict", "src")
    assert rc == 0
    assert target.read_text(encoding="utf-8") == (
        "from demo import helpers\n"
        "from demo.helpers import mystery  # cleanporter: ignore[CP002]\n"
        "print(helpers.Widget, mystery)\n"
    )


def test_ignore_cp003_on_a_never_read_import_points_to_cp001(
    project: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Both modes report the same codes; ``--fix`` also says which code to use."""
    source = "from demo.helpers import THING  # cleanporter: ignore[CP003]\n"
    _consumer(project, source)
    _rc, out, _ = _run(capsys, "src")
    assert sorted(code for _line, code in _findings(out)) == ["CP001", "CP005"]
    _rc, _out, err = _run(capsys, "--fix", "src")
    assert sorted(code for _line, code in _findings(err)) == ["CP003", "CP005"]
    assert "a never-read import is suppressed by CP001" in err


def test_a_comment_in_a_file_a_skip_rule_takes_is_never_unused(
    project: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (project / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0"\n'
        "[tool.cleanporter]\nskip = [{ file = '.*consumer[.]py' }]\n",
        encoding="utf-8",
        newline="\n",
    )
    _consumer(project, "from demo.helpers import THING  # cleanporter: ignore[CP002]\nx = THING\n")
    rc, out, _ = _run(capsys, "src")
    assert rc == 0
    assert _findings(out) == []


# -- --format json ---------------------------------------------------------------


def test_json_reports_suppressions(
    project: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _consumer(
        project,
        "from demo.helpers import THING  # cleanporter: ignore[CP001]\n"
        "from demo import helpers  # cleanporter: ignore[CP001]\n"
        "x = THING, helpers\n",
    )
    rc, out, _ = _run(capsys, "--format", "json", "--show-skipped", "src")
    document = json.loads(out)
    assert isinstance(document, dict)
    assert rc == document["exit_code"] == 1
    counts = document["counts"]
    assert isinstance(counts, dict)
    assert (counts["skipped_by_config"], counts["unused_suppressions"]) == (1, 1)
    findings = document["findings"]
    assert isinstance(findings, list)
    assert [(f["line"], f["code"], f["status"], f["level"]) for f in findings] == [
        (1, "CP004", "skipped-by-config", "note"),
        (2, "CP005", "unused-suppression", "error"),
    ]
    unused = findings[1]
    assert (unused["parent"], unused["name"], unused["column"]) == ("", "", 26)
    assert unused["detail"].startswith("no CP001 finding")

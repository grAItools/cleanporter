#!/usr/bin/env python
# Copyright (c) 2026 grAItools
# SPDX-License-Identifier: BSD-3-Clause
# See LICENSE for the full license text.

"""Run ``cleanporter --fix`` over a gt4py checkout and re-run gt4py's own tests.

The same idea as ``run.py`` -- rewrite real code, then *execute* it, and count
only failures that are new -- pointed at a moving upstream instead of pinned
wheels. gt4py earns its own check because it is where every unsafe-rewrite
class in issue #2 came from, and because those bugs are invisible to
everything else here: they produce code that imports and parses, so the
fixer's own re-parse backstop passes them, and only running the suite says
otherwise.

## Why a checkout of ``main`` rather than a pin

The corpus is pinned so that a red run means *this* repository changed. This
is the opposite question: does the fixer still hold up against code that is
moving? A new construct in gt4py, or a new spelling of an old one, can reach a
guard nothing here has exercised. Weekly, so the answer is never more than a
week stale, and never on the pull-request path, where "upstream changed" is
not an answer anyone wants to a docs typo.

## The skip rules are part of the test

A gt4py function body under ``@field_operator`` is re-parsed by gt4py's own
frontend, which rejects a module-qualified call outright, and a ``conftest.py``
namespace *is* pytest's fixture registry. cleanporter cannot detect either --
that is what ``[tool.cleanporter.skip]`` exists for -- so this writes the rules
a gt4py user is expected to write (`SKIP_CONFIG`) before running the fixer.
Without them the check fails every week for a reason that is not a defect,
which is the fastest way to teach everyone to ignore it.

## Usage

    uv run corpus/gt4py_check.py /path/to/gt4py

The checkout must already have its own environment -- ``uv sync
--no-default-groups --extra next --group test`` inside it -- and ``--python``
defaults to the ``.venv`` that creates. The checkout is modified in place: it
is expected to be a scratch clone.

Three things are checked rather than trusted, all of them ways this could
otherwise report success having proved nothing: the interpreter must import
the package from source *in* the checkout, not from an installed copy of it
(`_verify_environment`); the checkout must have no local modifications, or a
previous run's rewrite is part of the baseline (`_verify_pristine`); and
``--fix`` must actually rewrite something, or both suites ran the same tree.
Each is an operational error, exit 2.

Exits 0 when the rewrite broke nothing, 1 when a test that passed now fails,
2 if the harness could not run.
"""

from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys
import tomllib
import xml.etree.ElementTree as ET

EXIT_OK, EXIT_REGRESSION, EXIT_ERROR = 0, 1, 2

#: What a gt4py user is expected to declare, and what the issue-#2 write-up
#: measured. Bare last-component patterns on purpose: they match both
#: ``@field_operator`` and ``@gtx.field_operator``, where a rule written
#: against the dotted spelling would silently miss every file that imports the
#: decorator by name.
SKIP_CONFIG = """
# Added by cleanporter's weekly gt4py check -- see corpus/gt4py_check.py.
[tool.cleanporter]
skip = [
    { decorator = 'field_operator|scan_operator|program', reason = "GT4Py re-parses these bodies" },
    { file = '.*conftest\\.py', reason = "pytest collects fixtures from this namespace" },
]
"""

#: Default selection: what the issue-#2 report measured before and after. They
#: need no compilation toolchain, and they are the part of the suite that
#: actually executes rewritten stencil code.
DEFAULT_TESTS = ["tests/next_tests/unit_tests"]

#: The package the checkout is expected to provide, for `_verify_environment`.
PACKAGE = "gt4py"

#: How much of a failed run to show. Enough for the first traceback, not the
#: whole suite.
_TAIL = 60


def _parse_junit(report: pathlib.Path) -> tuple[dict[str, str], int]:
    """``{test id: "failure" | "error"}`` and the number of tests that ran.

    Ids rather than a pass/fail tally, because the tally hides a swap -- one
    test starting to fail while another starts passing -- and because the
    point of a weekly report a human reads once is that it *names* what
    broke. Skips and xfails carry their own element and are not failures.
    """
    root = ET.parse(report).getroot()  # noqa: S314 - pytest wrote this file
    outcomes: dict[str, str] = {}
    total = 0
    for case in root.iter("testcase"):
        total += 1
        ident = f"{case.get('classname', '')}::{case.get('name', '')}"
        for kind in ("failure", "error"):
            if case.find(kind) is not None:
                outcomes[ident] = kind
                break
    return outcomes, total


def _run_suite(
    checkout: pathlib.Path, python: pathlib.Path, tests: list[str], report: pathlib.Path
) -> tuple[dict[str, str], int, str]:
    """Run the selection under gt4py's own interpreter; never raises on failure.

    A non-zero exit is expected on both sides of the comparison -- upstream
    ``main`` is allowed to be red, and this only reports what *changed*.
    """
    # A report left by an earlier invocation would otherwise be read as this
    # run's result: pytest overwrites it for an ordinary failure, but not when
    # a conftest raises at import and it never gets that far.
    report.unlink(missing_ok=True)
    log = report.with_suffix(".log")
    proc = subprocess.run(
        [
            str(python),
            "-m",
            "pytest",
            *tests,
            "-q",
            "--no-header",
            "-p",
            "no:cacheprovider",
            f"--junit-xml={report}",
        ],
        cwd=checkout,
        capture_output=True,
        text=True,
        check=False,
    )
    output = proc.stdout + proc.stderr
    # Kept next to the report: the harness prints only the tail of a failed
    # run, and the rest of a 2000-test run is what someone reading the weekly
    # result a day later actually needs.
    log.write_text(output, encoding="utf-8")
    if not report.is_file():
        return {}, 0, output
    outcomes, total = _parse_junit(report)
    return outcomes, total, output


def _verify_environment(checkout: pathlib.Path, python: pathlib.Path) -> bool:
    """True when *python* imports the package from *checkout* and not elsewhere.

    Without this the check can pass while proving nothing. An environment
    installed from one tree and pointed at a *copy* of it -- the obvious way
    to keep a pristine reference around -- keeps its editable install aimed at
    the original, so the tests run rewritten test files against pristine
    library code and report that the rewrite broke nothing. Found by doing
    exactly that. A green run has to mean the rewritten code was the code that
    ran.
    """
    proc = subprocess.run(
        [str(python), "-c", f"import {PACKAGE}, pathlib; print(pathlib.Path({PACKAGE}.__file__))"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        print(f"gt4py: {python} cannot import {PACKAGE}:\n{proc.stderr[-2000:]}", file=sys.stderr)
        return False
    imported = pathlib.Path(proc.stdout.strip()).resolve()
    # Under the checkout is not enough: a *non-editable* install lands in
    # ``<checkout>/.venv/.../site-packages``, which satisfies that test while
    # being a copy the rewrite never touches -- the same vacuous pass by a
    # different route. Only source the fixer can reach counts.
    if not imported.is_relative_to(checkout) or "site-packages" in imported.parts:
        print(
            f"gt4py: {python} imports {PACKAGE} from {imported}, which is not source "
            f"under {checkout} -- the rewrite would not be what runs. Install the "
            "environment from the checkout being tested, as an editable install.",
            file=sys.stderr,
        )
        return False
    return True


def _verify_pristine(checkout: pathlib.Path) -> bool:
    """True when the checkout has no local modifications yet.

    A second run over a tree the first one rewrote compares rewritten code to
    rewritten code: the fixer finds little left to do and the comparison
    passes having tested nothing. In CI the checkout is always fresh, so this
    is for the documented local usage. ``git`` not being there, or the path
    not being a repository, is not itself a reason to refuse.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(checkout), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        # No git binary at all, which is the first case the docstring above
        # promises to tolerate -- and `subprocess.run` raises here before
        # there is a return code to read.
        print(f"gt4py: cannot tell whether {checkout} is pristine: {exc}")
        return True
    if proc.returncode != 0:
        # Out loud: git missing, not a repository, or a runner's "dubious
        # ownership" refusal all land here, and a guard that switched itself
        # off without a word is its own kind of silent pass.
        print(f"gt4py: cannot tell whether {checkout} is pristine: {proc.stderr.strip()}")
        return True
    if proc.stdout.strip():
        print(
            f"gt4py: {checkout} has local modifications, so a previous run's rewrite "
            "would be part of the baseline. Re-clone it, or `git checkout .` and "
            "delete the untracked files first.",
            file=sys.stderr,
        )
        return False
    return True


def _configure(checkout: pathlib.Path) -> bool:
    """Give the checkout the skip rules a gt4py user would write.

    A ``[tool.cleanporter]`` table that is already there is left alone: if
    gt4py ever adopts the tool, its own configuration is the one this should
    be testing, not a copy of it that has drifted.

    False when the file cannot be read as TOML at all. Letting that raise
    would exit 1, which this script defines as "a test that passed now
    fails" -- red for the wrong reason is only half as useful as red.
    """
    pyproject = checkout / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    # Parsed, not grepped. ``"[tool.cleanporter]" in text`` is wrong in both
    # directions: a project that writes only ``[tool.cleanporter.skip]`` would
    # get a second table appended, and a duplicate table is a TOML error that
    # takes the whole run down; a mention in a comment would suppress the
    # rules and turn every week red for no defect.
    try:
        configured = tomllib.loads(text).get("tool", {}).get("cleanporter")
    except tomllib.TOMLDecodeError as exc:
        print(f"gt4py: {pyproject} is not valid TOML: {exc}", file=sys.stderr)
        return False
    if configured is not None:
        print("gt4py: the checkout already configures cleanporter; using its rules")
        return True
    pyproject.write_text(text.rstrip("\n") + "\n" + SKIP_CONFIG, encoding="utf-8")
    print("gt4py: wrote the expected [tool.cleanporter.skip] rules into pyproject.toml")
    return True


def _fix(checkout: pathlib.Path, python: pathlib.Path) -> list[str] | None:
    """Rewrite the checkout in place, returning what it rewrote.

    None when cleanporter could not run.

    ``--python`` points at gt4py's environment so the resolver's probe sees
    the real dependency set, while cleanporter itself keeps running on the
    interpreter that has it installed.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "cleanporter", "--fix", "--python", str(python), "."],
        cwd=checkout,
        capture_output=True,
        text=True,
        check=False,
    )
    # 0 = clean, 1 = findings remain (expected). Anything else is the tool
    # failing to run at all, which is itself the news -- and so is a traceback
    # under exit 1, which is what an unhandled error on the way *into* the run
    # looks like (a malformed pyproject.toml, say, which `_configure` is in a
    # position to create).
    if proc.returncode not in (0, 1) or "Traceback (most recent call last)" in proc.stderr:
        print(
            f"gt4py: cleanporter failed (exit {proc.returncode}):\n{proc.stderr[-4000:]}",
            file=sys.stderr,
        )
        return None
    prefix = "fixed: "
    return [line[len(prefix) :] for line in proc.stderr.splitlines() if line.startswith(prefix)]


def _report(
    before: tuple[dict[str, str], int], after: tuple[dict[str, str], int], output: str
) -> bool:
    """Print the difference. True when the rewrite broke nothing."""
    before_failures, before_total = before
    after_failures, after_total = after
    print(f"\ntests: {before_total} before, {after_total} after")
    print(f"failures: {len(before_failures)} before, {len(after_failures)} after")

    new = {ident: kind for ident, kind in after_failures.items() if ident not in before_failures}
    ok = True
    if new:
        ok = False
        print(f"  {len(new)} test(s) that passed before now fail:")
        for ident, kind in sorted(new.items())[:25]:
            print(f"    [{kind}] {ident}")
    # A test that stopped being collected cannot show up as a new failure, and
    # a suite that shrank has not been proven unchanged -- the same reason
    # `run.py` refuses to call a bundled suite that produced no tally a pass.
    if after_total < before_total:
        ok = False
        print(f"  {before_total - after_total} test(s) are no longer collected")
    if not ok:
        print("\nlast lines of the post-fix run:")
        for line in output.splitlines()[-_TAIL:]:
            print(f"  {line}")
    return ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("checkout", help="path to a gt4py checkout with its environment installed")
    parser.add_argument("--python", help="interpreter gt4py is installed in (default: its .venv)")
    parser.add_argument(
        "--tests",
        action="append",
        metavar="PATH",
        help=f"test path to run, repeatable (default: {' '.join(DEFAULT_TESTS)})",
    )
    args = parser.parse_args(argv)

    checkout = pathlib.Path(args.checkout).resolve()
    # Resolved because `_fix` and `_run_suite` run with `cwd=checkout`, where a
    # relative interpreter path would mean something else entirely.
    python = (
        pathlib.Path(args.python).resolve()
        if args.python
        else checkout / ".venv" / "bin" / "python"
    )
    tests = args.tests or DEFAULT_TESTS
    if not (checkout / "pyproject.toml").is_file():
        print(f"gt4py: {checkout} is not a checkout (no pyproject.toml)", file=sys.stderr)
        return EXIT_ERROR
    if not python.is_file():
        print(f"gt4py: {python} does not exist; install the checkout first", file=sys.stderr)
        return EXIT_ERROR

    if not _verify_environment(checkout, python):
        return EXIT_ERROR
    if not _verify_pristine(checkout):
        return EXIT_ERROR

    work = checkout / ".cleanporter-check"
    work.mkdir(exist_ok=True)

    print(f"gt4py: running {' '.join(tests)} on the pristine checkout ...", flush=True)
    before_failures, before_total, before_output = _run_suite(
        checkout, python, tests, work / "before.xml"
    )
    if before_total == 0:
        # Every comparison below would then be vacuously true.
        print(f"gt4py: the selection collected nothing:\n{before_output[-4000:]}", file=sys.stderr)
        return EXIT_ERROR
    if len(before_failures) == before_total:
        # Nothing can newly fail if nothing passed. An environment that has
        # rotted so far that every module errors at import produces exactly
        # this, and it would otherwise be the greenest run of the year.
        print(
            f"gt4py: all {before_total} test(s) already fail, so no failure could be "
            f"new; the checkout's environment is the thing to fix:\n{before_output[-4000:]}",
            file=sys.stderr,
        )
        return EXIT_ERROR
    print(f"gt4py: {before_total} test(s), {len(before_failures)} already failing")

    if not _configure(checkout):
        return EXIT_ERROR
    print("gt4py: running cleanporter --fix over the checkout ...", flush=True)
    changed = _fix(checkout, python)
    if changed is None:
        return EXIT_ERROR
    # Both suites running *different trees* is the premise of the comparison,
    # and that is not quite the same as the rewrite reaching code the tests
    # execute: a rewrite confined to `scripts/` proves nothing about
    # `tests/next_tests/unit_tests`, and a dispatch with a narrow selection can
    # produce exactly that. `run.py` refuses a bundled suite with no tally for
    # the same reason -- a comparison that could not have come out differently
    # is not a pass. Zero rewrites anywhere is the same failure, one step
    # earlier: either the fixer declined every import in gt4py, or it never got
    # that far, and both are news.
    if not changed:
        print(
            "gt4py: --fix rewrote nothing, so the two runs are the same tree and prove nothing",
            file=sys.stderr,
        )
        return EXIT_ERROR
    relevant = [path for path in changed if path.startswith(("src/", *(f"{t}/" for t in tests)))]
    if not relevant:
        print(
            f"gt4py: --fix rewrote {len(changed)} file(s), none of them under src/ or "
            f"{' or '.join(tests)}, so both runs exercised the same code",
            file=sys.stderr,
        )
        return EXIT_ERROR
    print(f"gt4py: rewrote {len(changed)} file(s), {len(relevant)} of them under test")

    print("gt4py: re-running the same tests ...", flush=True)
    after = _run_suite(checkout, python, tests, work / "after.xml")
    ok = _report((before_failures, before_total), after[:2], after[2])
    print(
        "\ngt4py: OK, the rewrite broke nothing"
        if ok
        else "\ngt4py: REGRESSION -- the rewrite broke tests that passed, see above"
    )
    return EXIT_OK if ok else EXIT_REGRESSION


if __name__ == "__main__":
    sys.exit(main())

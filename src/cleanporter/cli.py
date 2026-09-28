"""Command-line interface: check, and optionally fix, from-imports.

Layers the configuration in one direction -- ``[tool.cleanporter]`` first, then
the flags that override it -- runs the check/fix loop, and picks the exit code:
0 clean, 1 anything left to fix, 2 operational error. A file the fixer declined
(`CP003`) counts toward the 1 -- it is a declined violation, not a note --
while `CP002` counts only under ``--strict``.

Stream contract: when a patch goes to stdout (``--diff``, ``--fix``) stdout
carries *only* the patch, so ``cleanporter --diff src/ | git apply`` works, and
findings, warnings and notes go to stderr. Plain check mode produces no patch,
so its report stays on stdout. The patch is written as bytes, in each file's
own encoding and line endings, so that it applies to the file on disk.

A file that cannot be read, decoded, parsed or written is reported with its
path and makes the exit code 2, and every other file is still processed.
"""

from __future__ import annotations

import argparse
import dataclasses
import difflib
import os
import pathlib
import sys
from collections.abc import Iterable
from typing import TextIO

import libcst as cst

import cleanporter
from cleanporter import _source, analyze, model, rewrite
from cleanporter import config as config_lib

#: Printed to stderr after `--fix` writes anything (see `run`).
_CROSS_FILE_NOTE = (
    "cleanporter: note: --fix cannot see dotted references from other files; re-run your tests"
)

_EXIT_OK = 0
_EXIT_VIOLATIONS = 1
_EXIT_ERROR = 2


def _non_empty(value: str) -> str:
    """An argument that must say something: ``--python ""`` was silently ignored."""
    if not value:
        msg = "must not be empty; omit the flag to use the current interpreter"
        raise argparse.ArgumentTypeError(msg)
    return value


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cleanporter",
        description=(
            "Check that 'from ... import ...' statements import modules only "
            "(Google Python Style Guide section 2.2) and optionally rewrite "
            "violations."
        ),
        epilog=(
            "examples:\n"
            "  cleanporter src/\n"
            "  cleanporter --diff src/\n"
            "  cleanporter --fix src/\n\n"
            "exit codes: 0 ok, 1 violations remain, 2 operational error.\n"
            "configure under [tool.cleanporter] in pyproject.toml."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("paths", nargs="*", default=["."], help="files or directories to process")
    parser.add_argument(
        "--fix", action="store_true", help="rewrite violations in place where provably safe"
    )
    parser.add_argument(
        "--diff",
        action="store_true",
        help="show the rewrite as a unified diff without writing "
        "(ignored if --fix is also given: --fix wins and writes)",
    )
    parser.add_argument(
        "--python",
        default=None,
        type=_non_empty,
        help="interpreter used to classify stdlib/third-party names",
    )
    parser.add_argument(
        "--exempt",
        action="append",
        default=[],
        metavar="MODULE",
        help="additional module whose members may be imported by name",
    )
    parser.add_argument(
        "--root",
        action="append",
        default=[],
        metavar="PATH",
        help="additional first-party import root",
    )
    parser.add_argument(
        "--strict", action="store_true", help="also fail on imports that could not be classified"
    )
    parser.add_argument(
        "--show-skipped",
        action="store_true",
        help="list the imports a [tool.cleanporter.skip] rule took out (CP004)",
    )
    parser.add_argument(
        "--version", action="version", version=f"cleanporter {cleanporter.__version__}"
    )
    return parser


def _diff_path(path: pathlib.Path) -> str:
    """*path* as a diff header should spell it: relative to the cwd, POSIX.

    Headers used to be built as ``f"a/{path}"`` straight from the record,
    so an absolute path argument produced ``a//home/you/pkg/file.py`` -- a
    doubled slash neither ``patch`` nor ``git apply`` can strip -- and the
    patch could not be applied from anywhere.
    """
    try:
        relative = os.path.relpath(path, pathlib.Path.cwd())
    except ValueError:  # pragma: no cover - different drive on Windows
        return pathlib.Path(path).as_posix().lstrip("/")
    return pathlib.Path(relative).as_posix()


def _apply_overrides(config: config_lib.Config, args: argparse.Namespace) -> config_lib.Config:
    return dataclasses.replace(
        config,
        exempt_modules=config.exempt_modules | frozenset(args.exempt),
        source_roots=config.source_roots + tuple(args.root),
        python=args.python or config.python,
        treat_unresolved_as_error=config.treat_unresolved_as_error or args.strict,
    )


def run(args: argparse.Namespace) -> int:
    anchor = pathlib.Path(args.paths[0]).resolve()
    try:
        config = _apply_overrides(config_lib.load_config(anchor), args)
    except config_lib.ConfigError as exc:
        print(f"cleanporter: configuration error: {exc}", file=sys.stderr)
        return _EXIT_ERROR

    # Everything that is not the patch -- warnings, parse errors, findings, the
    # summary -- goes here; see the stream contract in the module docstring.
    report = sys.stderr if (args.fix or args.diff) else sys.stdout

    paths = [pathlib.Path(p) for p in args.paths]
    mismatch = config_lib.mismatch_warning(paths)
    if mismatch is not None:
        print(f"cleanporter: warning: {mismatch}", file=report)
    records, resolver, parse_errors, warnings = analyze.build(paths, config)
    for warning in warnings:
        print(f"cleanporter: warning: {warning}", file=report)

    for error in sorted(parse_errors, key=lambda f: (str(f.path), f.line)):
        print(error.format(), file=report)

    findings: list[model.Finding] = []
    changed = 0
    for rec in records:
        current = rec
        unread: frozenset[str] = frozenset()
        if args.fix or args.diff:
            outcome = rewrite.fix_record(current, resolver, config)
            if outcome.status == "fixed":
                current, write_error = _apply(
                    current, outcome.source, write=args.fix, report=report
                )
                if write_error is None:
                    changed += 1
                else:
                    parse_errors.append(write_error)
            findings.extend(outcome.blockers)
            unread = outcome.unread
        findings.extend(analyze.analyze_record(current, resolver, config, unread))
    # A probe batch outside `analyze.build`'s warm-up (a lookup it did not
    # foresee) can fail too; say why as well.
    for warning in resolver.take_warnings():
        print(f"cleanporter: warning: {warning}", file=report)

    if args.fix and changed:
        # The one place the tool changes something it cannot fully check: a
        # dotted reference living in *another* file (`monkeypatch.setattr(
        # "pkg.mod.name", ...)`, an entry point, an importlib lookup) is
        # invisible to a per-file guard. Documented in the README, but a user
        # who only ever reads --help would never see it. stderr, so a piped
        # patch on stdout stays a patch.
        print(_CROSS_FILE_NOTE, file=sys.stderr)

    findings.sort(key=lambda f: (str(f.path), f.line, f.column, f.code))
    for finding in findings:
        # CP004 is the author's own configuration reporting back, so it is
        # counted but not printed: a project that skips a thousand imports
        # deliberately should not have to read about it on every run.
        if finding.status is model.Status.SKIPPED_BY_CONFIG and not args.show_skipped:
            continue
        print(finding.format(), file=report)

    violations = sum(f.status is model.Status.VIOLATION for f in findings)
    skipped = sum(f.status is model.Status.SKIPPED for f in findings)
    unresolved = sum(f.status is model.Status.UNRESOLVED for f in findings)
    by_config = sum(f.status is model.Status.SKIPPED_BY_CONFIG for f in findings)
    print(file=report)
    print(
        f"checked {len(records)} file(s)"
        + (f", fixed {changed}" if args.fix else "")
        + f": {violations} violation(s), {skipped} not rewritten, "
        f"{unresolved} unresolved, {by_config} skipped by config",
        file=report,
    )

    if parse_errors:
        return _EXIT_ERROR
    hard = violations + skipped + (unresolved if config.treat_unresolved_as_error else 0)
    return _EXIT_VIOLATIONS if hard else _EXIT_OK


def _apply(
    rec: analyze.FileRecord, source: str, *, write: bool, report: TextIO
) -> tuple[analyze.FileRecord, model.Finding | None]:
    """Print *rec*'s rewrite as a patch and, if *write*, put it on disk.

    Returns the record for what is now on disk, and an error finding if the
    write failed -- in which case the file is untouched (the write is atomic),
    no patch is printed for it, and the run goes on to the next file.

    The patch and the file are both bytes in the file's own encoding and
    newline convention, so a CRLF, Latin-1 or BOM-carrying file gets a patch
    that applies to it and a rewrite that changes only the lines it had to.
    """
    before = rec.raw if rec.raw is not None else rec.source.encode(rec.encoding)
    after = source.encode(rec.encoding)
    if write:
        try:
            _source.write_atomic(rec.path, after)
        except OSError as exc:
            try:
                notes = exc.__notes__  # e.g. a temporary file that could not be removed
            except AttributeError:
                notes = []
            error = model.Finding(
                rec.path,
                1,
                0,
                "?",
                "?",
                model.Status.UNRESOLVED,
                "; ".join([f"cannot write file: {exc.strerror or exc}", *notes]),
            )
            print(error.format(), file=report)
            return rec, error
    name = os.fsencode(_diff_path(rec.path))
    _write_patch(
        difflib.diff_bytes(
            difflib.unified_diff,
            _source.patch_lines(before),
            _source.patch_lines(after),
            fromfile=b"a/" + name,
            tofile=b"b/" + name,
        )
    )
    if not write:
        return rec, None
    print(f"fixed: {rec.path}", file=report)
    # Report against what is now on disk.
    return _reparse(rec, source, after), None


def _write_patch(lines: Iterable[bytes]) -> None:
    r"""Write patch *lines* to stdout as the bytes they are.

    Bytes, not text: a Latin-1 file's patch has to carry Latin-1 bytes to
    apply to it, and a CRLF line's ``\r`` has to survive -- a text-mode stdout
    would re-encode the first and, on Windows, double the second. A last line
    with no newline gets the ``\ No newline at end of file`` marker that
    ``patch`` and ``git apply`` expect, instead of running into the next
    file's header.
    """
    chunks: list[bytes] = []
    for line in lines:
        chunks.append(line)
        if not line.endswith(b"\n"):
            chunks.append(b"\n\\ No newline at end of file\n")
    data = b"".join(chunks)
    sys.stdout.flush()
    try:
        buffer = sys.stdout.buffer
    except AttributeError:  # stdout swapped for a text-only stream (io.StringIO)
        sys.stdout.write(data.decode("utf-8", "surrogateescape"))
        return
    buffer.write(data)
    buffer.flush()


def _reparse(rec: analyze.FileRecord, source: str, raw: bytes | None = None) -> analyze.FileRecord:
    """The same file, re-read from what was just written to it.

    Everything the record was built with is carried over, and that includes
    ``qualname`` -- without it the post-fix pass would forget which module
    this file *is*, and report a re-export it had correctly declined to touch
    as an ordinary violation -- and the encoding the file is written in. The
    skip regions are deliberately *not* carried over: they are line spans,
    and the lines have just moved.
    """
    return analyze.FileRecord(
        rec.path,
        source,
        cst.parse_module(source),
        rec.base_pkg,
        rec.qualname,
        root=rec.root,
        skip_rules=rec.skip_rules,
        encoding=rec.encoding,
        raw=raw,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        return run(args)
    except (OSError, UnicodeDecodeError) as exc:
        print(f"cleanporter: error: {exc}", file=sys.stderr)
        return _EXIT_ERROR
    except KeyboardInterrupt:  # pragma: no cover
        return _EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

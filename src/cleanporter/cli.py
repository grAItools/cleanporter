"""Command-line interface: check, and optionally fix, from-imports."""

from __future__ import annotations

import argparse
import difflib
import os
import sys
from dataclasses import replace
from pathlib import Path

import libcst as cst

from . import __version__
from .analyze import FileRecord, analyze_record, build
from .config import Config, ConfigError, load_config
from .model import Finding, Status
from .rewrite import fix_record

#: Printed to stderr after `--fix` writes anything (see `run`).
_CROSS_FILE_NOTE = (
    "cleanporter: note: --fix cannot see dotted references from other files; "
    "re-run your tests"
)

_EXIT_OK = 0
_EXIT_VIOLATIONS = 1
_EXIT_ERROR = 2


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
    parser.add_argument("paths", nargs="*", default=["."],
                        help="files or directories to process")
    parser.add_argument("--fix", action="store_true",
                        help="rewrite violations in place where provably safe")
    parser.add_argument("--diff", action="store_true",
                        help="show the rewrite as a unified diff without writing "
                             "(ignored if --fix is also given: --fix wins and writes)")
    parser.add_argument("--python", default=None,
                        help="interpreter used to classify stdlib/third-party names")
    parser.add_argument("--exempt", action="append", default=[], metavar="MODULE",
                        help="additional module whose members may be imported by name")
    parser.add_argument("--root", action="append", default=[], metavar="PATH",
                        help="additional first-party import root")
    parser.add_argument("--strict", action="store_true",
                        help="also fail on imports that could not be classified")
    parser.add_argument("--version", action="version",
                        version=f"cleanporter {__version__}")
    return parser


def _diff_path(path: Path) -> str:
    """*path* as a diff header should spell it: relative to the cwd, POSIX.

    Headers used to be built as ``f"a/{path}"`` straight from the record,
    so an absolute path argument produced ``a//home/you/pkg/file.py`` -- a
    doubled slash neither ``patch`` nor ``git apply`` can strip -- and the
    patch could not be applied from anywhere.
    """
    try:
        relative = os.path.relpath(path, Path.cwd())
    except ValueError:  # pragma: no cover - different drive on Windows
        return Path(path).as_posix().lstrip("/")
    return Path(relative).as_posix()


def _apply_overrides(config: Config, args: argparse.Namespace) -> Config:
    return replace(
        config,
        exempt_modules=config.exempt_modules | frozenset(args.exempt),
        source_roots=config.source_roots + tuple(args.root),
        python=args.python or config.python,
        treat_unresolved_as_error=config.treat_unresolved_as_error or args.strict,
    )


def run(args: argparse.Namespace) -> int:
    anchor = Path(args.paths[0]).resolve()
    try:
        config = _apply_overrides(load_config(anchor), args)
    except ConfigError as exc:
        print(f"cleanporter: configuration error: {exc}", file=sys.stderr)
        return _EXIT_ERROR

    # When a patch is going to stdout, stdout carries *only* the patch:
    # everything else -- warnings, parse errors, findings, the summary --
    # goes to stderr, so `cleanporter --diff src/ | git apply` works. In
    # plain check mode there is no patch, so the report stays on stdout.
    report = sys.stderr if (args.fix or args.diff) else sys.stdout

    paths = [Path(p) for p in args.paths]
    records, resolver, parse_errors, warnings = build(paths, config)
    for warning in warnings:
        print(f"cleanporter: warning: {warning}", file=report)

    for error in sorted(parse_errors, key=lambda f: (str(f.path), f.line)):
        print(error.format(), file=report)

    findings: list[Finding] = []
    changed = 0
    for rec in records:
        if args.fix or args.diff:
            outcome = fix_record(rec, resolver, config)
            if outcome.status == "fixed":
                changed += 1
                # Diff first: rec.source is still the original here.
                sys.stdout.writelines(
                    difflib.unified_diff(
                        rec.source.splitlines(keepends=True),
                        outcome.source.splitlines(keepends=True),
                        fromfile=f"a/{_diff_path(rec.path)}",
                        tofile=f"b/{_diff_path(rec.path)}",
                    )
                )
                if args.fix:
                    rec.path.write_text(outcome.source, encoding="utf-8")
                    print(f"fixed: {rec.path}", file=report)
                    # Report against what is now on disk.
                    rec = _reparse(rec, outcome.source)
            findings.extend(outcome.blockers)
        findings.extend(analyze_record(rec, resolver, config))

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
        print(finding.format(), file=report)

    violations = sum(f.status is Status.VIOLATION for f in findings)
    skipped = sum(f.status is Status.SKIPPED for f in findings)
    unresolved = sum(f.status is Status.UNRESOLVED for f in findings)
    print(file=report)
    print(
        f"checked {len(records)} file(s)"
        + (f", fixed {changed}" if args.fix else "")
        + f": {violations} violation(s), {skipped} not rewritten, "
        f"{unresolved} unresolved",
        file=report,
    )

    if parse_errors:
        return _EXIT_ERROR
    hard = violations + skipped + (unresolved if config.treat_unresolved_as_error else 0)
    return _EXIT_VIOLATIONS if hard else _EXIT_OK


def _reparse(rec: FileRecord, source: str) -> FileRecord:
    return FileRecord(rec.path, source, cst.parse_module(source), rec.base_pkg)


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

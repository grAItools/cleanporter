"""Command-line interface: check, and optionally fix, from-imports.

Layers the configuration in one direction -- ``[tool.cleanporter]`` first, then
the flags that override it -- hands the run to `engine.run`, prints what it
reports, and picks the exit code: 0 clean, 1 anything left to fix, 2
operational error. The run itself -- fixing, writing, re-analysing, counting --
is the engine's; this module is only its shell. A file the fixer declined
(`CP003`) counts toward the 1 -- it is a declined violation, not a note --
while `CP002` counts only under ``--strict``.

Stream contract: when a patch goes to stdout (``--diff``, ``--fix``) stdout
carries *only* the patch, so ``cleanporter --diff src/ | git apply`` works, and
findings, warnings and notes go to stderr. Plain check mode produces no patch,
so its report stays on stdout. Notes (``cleanporter: note:`` -- the
interpreter detected for the probe, the ``--fix`` reminder) go to stderr in
every mode. The patch is written as bytes, in each file's own encoding and
line endings, so that it applies to the file on disk.

A file that cannot be read, decoded, parsed or written is reported with its
path and makes the exit code 2, and every other file is still processed.
"""

from __future__ import annotations

import argparse
import dataclasses
import pathlib
import sys
from typing import TextIO

import cleanporter
from cleanporter import config as config_lib
from cleanporter import engine, model

#: Printed to stderr after `--fix` writes anything (see `run`).
_CROSS_FILE_NOTE = (
    "cleanporter: note: --fix cannot see dotted references from other files; re-run your tests"
)

#: `--whole-project` with no pyproject.toml to say where the project starts.
_NO_PROJECT = (
    "--whole-project needs a pyproject.toml to mark the project root; none is above any listed path"
)

_EXIT_ERROR = 2  # the rest of the exit-code rule is `engine.RunResult.exit_code`


def _non_empty(value: str) -> str:
    """An argument that must say something: ``--python ""`` was silently ignored."""
    if not value:
        msg = "must not be empty; omit the flag to detect the project's interpreter"
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
        metavar="PATH|auto|self",
        help="interpreter used to classify stdlib/third-party names: a path or command, "
        "'auto' to detect the project's ($UV_PROJECT_ENVIRONMENT or .venv at the project "
        "root, or at the uv workspace root for a member, else $VIRTUAL_ENV; the default), "
        "or 'self' for cleanporter's own",
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
        "--whole-project",
        action="store_true",
        help="analyse the whole project (the directory of pyproject.toml) for evidence, but "
        "fix, report and count only the given paths; for pre-commit, which passes only "
        "changed files",
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


def _apply_overrides(config: config_lib.Config, args: argparse.Namespace) -> config_lib.Config:
    return dataclasses.replace(
        config,
        exempt_modules=config.exempt_modules | frozenset(args.exempt),
        source_roots=config.source_roots + tuple(args.root),
        python=args.python or config.python,
        treat_unresolved_as_error=config.treat_unresolved_as_error or args.strict,
    )


def _mode(args: argparse.Namespace) -> engine.Mode:
    if args.fix:
        return engine.Mode.FIX
    return engine.Mode.DIFF if args.diff else engine.Mode.CHECK


class _Printer(engine.Listener):
    """Prints what the engine reports as it happens: the patch to stdout, the rest to *report*."""

    def __init__(self, report: TextIO) -> None:
        self._report = report

    def warning(self, message: str) -> None:
        print(f"cleanporter: warning: {message}", file=self._report)

    def note(self, message: str) -> None:
        # stderr in every mode, like the --fix note: it is about the run, not
        # a finding, and check mode's stdout report stays findings only.
        print(f"cleanporter: note: {message}", file=sys.stderr)

    def error(self, finding: model.Finding) -> None:
        print(finding.format(), file=self._report)

    def patch(self, patch: engine.FilePatch) -> None:
        _write_patch(patch.diff)
        if patch.written:
            print(f"fixed: {patch.path}", file=self._report)


def _project_anchor(
    paths: list[pathlib.Path],
) -> tuple[pathlib.Path, list[pathlib.Path]] | None:
    """For `--whole-project`: the project directory, and *paths* with its path first.

    The project is that of the first listed path with a pyproject.toml above
    it -- looked up from the path as written, not from a symlink's target --
    so the answer does not depend on the order pre-commit lists files in. That
    path goes first because the engine names the first path's configuration
    in its mismatch warning. ``None`` when no path has one.
    """
    for path in paths:
        pyproject = config_lib.find_pyproject(path, resolve=False)
        if pyproject is not None:
            return pyproject.parent, [path, *(p for p in paths if p is not path)]
    return None


def run(args: argparse.Namespace) -> int:
    paths = [pathlib.Path(p) for p in args.paths]
    anchor = paths[0].resolve()
    if args.whole_project:
        found = _project_anchor(paths)
        if found is None:
            # Without one, the "project" would be the first path's directory:
            # a partial tree presented as the whole, which the flag exists to avoid.
            print(f"cleanporter: error: {_NO_PROJECT}", file=sys.stderr)
            return _EXIT_ERROR
        anchor, paths = found
    try:
        config = _apply_overrides(config_lib.load_config(anchor), args)
    except config_lib.ConfigError as exc:
        print(f"cleanporter: configuration error: {exc}", file=sys.stderr)
        return _EXIT_ERROR

    mode = _mode(args)
    # Everything that is not the patch -- warnings, parse errors, findings, the
    # summary -- goes here; see the stream contract in the module docstring.
    report = sys.stdout if mode is engine.Mode.CHECK else sys.stderr
    result = engine.run(
        paths, config, mode, listener=_Printer(report), whole_project=args.whole_project
    )

    if result.wrote:
        # The one place the tool changes something it cannot fully check: a
        # dotted reference living in *another* file (`monkeypatch.setattr(
        # "pkg.mod.name", ...)`, an entry point, an importlib lookup) is
        # invisible to a per-file guard. Documented in the README, but a user
        # who only ever reads --help would never see it. stderr, so a piped
        # patch on stdout stays a patch.
        print(_CROSS_FILE_NOTE, file=sys.stderr)

    for finding in result.findings:
        # CP004 is the author's own configuration reporting back, so it is
        # counted but not printed: a project that skips a thousand imports
        # deliberately should not have to read about it on every run.
        if finding.status is model.Status.SKIPPED_BY_CONFIG and not args.show_skipped:
            continue
        print(finding.format(), file=report)

    print(file=report)
    print(
        f"checked {result.files_checked} file(s)"
        + (f", fixed {result.changed}" if mode is engine.Mode.FIX else "")
        + f": {result.violations} violation(s), {result.skipped} not rewritten, "
        f"{result.unresolved} unresolved, {result.skipped_by_config} skipped by config",
        file=report,
    )

    return result.exit_code(strict=config.treat_unresolved_as_error)


def _write_patch(data: bytes) -> None:
    r"""Write a patch to stdout as the bytes it is.

    Bytes, not text: a Latin-1 file's patch has to carry Latin-1 bytes to
    apply to it, and a CRLF line's ``\r`` has to survive -- a text-mode stdout
    would re-encode the first and, on Windows, double the second.
    """
    sys.stdout.flush()
    try:
        buffer = sys.stdout.buffer
    except AttributeError:  # stdout swapped for a text-only stream (io.StringIO)
        sys.stdout.write(data.decode("utf-8", "surrogateescape"))
        return
    buffer.write(data)
    buffer.flush()


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

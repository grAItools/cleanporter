"""One run, end to end, as a library call: `run` paths under a config in a `Mode`.

`run` is everything the command line does except printing and choosing an
exit code. It builds the `project.Project`, then, per file in discovery order:

* under `Mode.DIFF` or `Mode.FIX`, asks `rewrite.fix_record` for the rewrite
  and, when there is one, makes a `FilePatch` of it -- and under `Mode.FIX`
  first writes it to disk, atomically, and re-parses what was written. A file
  with no `CP001` costs little more than under `Mode.CHECK`: the fixer answers
  it from the check's own decisions, without the scope analysis a rewrite
  needs;
* analyses the file as it now is (`analyze.analyze_record`), with whatever
  the fixer learned about names nothing reads, so a declined import is
  reported with its reason.

It returns a `RunResult`: the findings sorted as the report prints them, the
patches, the file-level errors, the warnings and notes, and the counts.
`cli.run` is a thin shell over it, and anything else that wants a real run --
an editor integration, a pre-commit wrapper, a test -- calls this rather than
copying the loop.

A `Listener` hears about warnings, file-level errors and patches *as they
happen*, in the order the command line prints them, so a caller streaming
output (the CLI writes each patch the moment it exists) sees exactly what it
would see if it printed from inside the loop. Everything a listener is told
is also in the `RunResult`; a caller that only wants the result passes none.

A file that cannot be written under `Mode.FIX` is left untouched (the write
is atomic), is reported as a file-level error, and is analysed as it still
is; its patch is kept in the result, marked with the error
(`FilePatch.write_error`), but the listener hears only the error -- the
command prints no patch for a file it did not change. Every other file is
processed as usual. `RunResult.exit_code` is the command's exit-code rule.

**Whole-project runs** (``whole_project=True``, ``--whole-project``) separate
the files a run *reads* from the files it *reports*. A pre-commit hook is
handed only the changed files, but a run over only those is a run over a
partial tree, and two things a run knows come from the files it is given:

* the cross-file *use* evidence (`resolver.Resolver.is_load_bearing`): a
  re-export that only an unlisted file imports looks unused, so ``--fix``
  would rewrite it and break that file at import time;
* the first-party module map's *import roots*, which are inferred from the
  files given: a sibling top-level package none of whose files was listed is
  not first-party, so its imports go to the interpreter probe -- which answers
  from whatever it finds installed under that name, a stale non-editable
  copy or an unrelated distribution -- and the absolute-import evidence that
  demotes a PEP 420 namespace root (`firstparty.ModuleMap.demote_roots`) is
  missing, so a file under one can be given the wrong dotted name.

So a whole-project run builds the `project_lib.Project` a full run over the
project root (`config.Config.root`: the directory of the ``pyproject.toml``
in use) would build, and then fixes, reports and counts only the files under
the listed paths: every rewrite is judged on that run's evidence. Evidence
still stops at the root, so a consumer in another project (a sibling uv
workspace member, say) is as invisible as it is to any run.

Every listed path, and every file a listed directory expands to, must
therefore lie inside the root, both as written (absolute, symlinks kept) and
resolved. One that does not -- a stray script
under no ``pyproject.toml``, a symlink into another project -- would be
judged on evidence that is not its own tree's: its neighbours, or its real
project's consumers, are never read, so a fix could delete what they import.
It is refused, and so is the whole run: each such path is a
`RunResult.errors` entry (exit 2) and nothing is analysed or written.

The root is walked like any directory, so a listed file the
configuration excludes (or one in a skipped directory) is not reported: a
hook handed every changed file honours ``exclude`` as ``cleanporter .`` run
from the project root does. A file elsewhere in the tree that cannot be read
or parsed is a warning rather than an error, since the run does not report on
it -- but its imports are then missing from the evidence, and the warning
says so.
"""

from __future__ import annotations

import dataclasses
import difflib
import enum
import os
import pathlib
from collections.abc import Iterable

import libcst as cst

from cleanporter import analyze, discover, model, rewrite
from cleanporter import config as config_lib
from cleanporter import project as project_lib

from . import _source


class Mode(enum.Enum):
    """What a run does with the violations it can rewrite."""

    #: Report only; nothing is rewritten, no patch is made.
    CHECK = "check"
    #: Compute every rewrite as a `FilePatch`, and write nothing.
    DIFF = "diff"
    #: Compute every rewrite, write it to disk, and report what is now there.
    FIX = "fix"


@dataclasses.dataclass(frozen=True)
class FilePatch:
    """One file's rewrite, as bytes in the file's own encoding and newlines."""

    path: pathlib.Path
    #: The unified diff, headed ``a/<path>`` / ``b/<path>`` with the path
    #: relative to the working directory at the time of the run, ready for
    #: ``patch -p1`` or ``git apply``.
    diff: bytes
    #: True when the rewrite was written to disk (`Mode.FIX`).
    written: bool
    #: The file's contents before and after the rewrite.
    before: bytes = dataclasses.field(repr=False)
    after: bytes = dataclasses.field(repr=False)
    #: Under `Mode.FIX`, why the rewrite could not be written (``written`` is
    #: then False and the file is untouched); the same finding is in
    #: `RunResult.errors`. The patch is kept so the rewrite is not lost: it
    #: still applies to the file as it is on disk.
    write_error: model.Finding | None = None


@dataclasses.dataclass(frozen=True)
class RunResult:
    """Everything a run found and did. Made by `run`."""

    mode: Mode
    #: How many files were read and parsed (and so checked).
    files_checked: int
    #: Every finding, `CP004` included, sorted by path, line, column, code.
    findings: tuple[model.Finding, ...]
    #: One per rewritten file, in discovery order -- under `Mode.FIX`
    #: including a file whose write failed, marked by its `FilePatch.write_error`.
    patches: tuple[FilePatch, ...]
    #: A finding per file that could not be read, decoded, parsed or written:
    #: those that failed to load (sorted), then write failures in file order
    #: (also in `write_errors`). Any of them makes `exit_code` 2.
    errors: tuple[model.Finding, ...]
    #: Every warning, in the order it arose.
    warnings: tuple[str, ...]
    #: Every note, in the order it arose: which interpreter was detected for
    #: the probe, and why (see `Config.python`). Informational, never a failure.
    notes: tuple[str, ...] = ()
    #: The files counted by `files_checked`, in discovery order.
    checked: tuple[pathlib.Path, ...] = ()
    #: With a baseline applied (`cleanporter.baseline.apply`), how many
    #: findings it took out of `findings`; ``None`` when none was.
    baselined: int | None = None
    #: With a baseline applied, how many of its entries matched no finding.
    stale_baseline: int | None = None

    def _count(self, status: model.Status) -> int:
        return sum(f.status is status for f in self.findings)

    @property
    def violations(self) -> int:
        """`CP001` findings."""
        return self._count(model.Status.VIOLATION)

    @property
    def skipped(self) -> int:
        """`CP003` findings: violations the fixer declined, or cannot fix."""
        return self._count(model.Status.SKIPPED)

    @property
    def unresolved(self) -> int:
        """`CP002` findings (file-level errors are in `errors`, not here)."""
        return self._count(model.Status.UNRESOLVED)

    @property
    def skipped_by_config(self) -> int:
        """`CP004` findings."""
        return self._count(model.Status.SKIPPED_BY_CONFIG)

    @property
    def unused_suppressions(self) -> int:
        """`CP005` findings."""
        return self._count(model.Status.UNUSED_SUPPRESSION)

    @property
    def alias_mismatches(self) -> int:
        """`CP006` findings."""
        return self._count(model.Status.ALIAS_MISMATCH)

    @property
    def write_errors(self) -> tuple[model.Finding, ...]:
        """The `errors` that are failed writes under `Mode.FIX`, in file order."""
        return tuple(p.write_error for p in self.patches if p.write_error is not None)

    @property
    def changed(self) -> int:
        """Files rewritten: written under `Mode.FIX`, diffed under `Mode.DIFF`."""
        return sum(p.write_error is None for p in self.patches)

    @property
    def wrote(self) -> bool:
        """Whether this run changed anything on disk."""
        return any(p.written for p in self.patches)

    def exit_code(self, *, strict: bool = False) -> int:
        """The command's exit code for this result: 0 clean, 1 violations, 2 errors.

        2 when any file could not be read, decoded, parsed or written;
        otherwise 1 when a `CP001`, `CP003`, `CP005` or `CP006` remains -- or, under
        *strict* (``--strict`` / ``treat_unresolved_as_error``), a `CP002`;
        otherwise 0. `CP004` never counts. Only `findings` count, so a code
        `Config.select` / `Config.ignore` left out, or a finding a baseline
        took out, cannot fail the run.
        """
        if self.errors:
            return 2
        hard = (
            self.violations
            + self.skipped
            + self.unused_suppressions
            + self.alias_mismatches
            + (self.unresolved if strict else 0)
        )
        return 1 if hard else 0


class Listener:
    """Told about each warning, file-level error and patch as the run makes it.

    Every method does nothing; override the ones you want. The calls come in
    this order: in a whole-project run, first the listed paths (or files of a
    listed directory) outside the root, after which a refused run stops; then
    the warning that the paths belong to different projects, then (in a
    whole-project run) the listed paths that do not exist, then the
    notes (the interpreter detected for the probe), then the warnings from
    building the project (a missing path, nesting roots, a failed warm-up
    probe), then the files that could not be loaded (sorted by path; in a
    whole-project run an unlisted one is a warning instead), then per file a
    write error or a patch followed by its malformed suppression comments,
    then warnings from probes the warm-up did not foresee.
    """

    def warning(self, message: str) -> None:
        """A warning, without the ``cleanporter: warning:`` prefix."""

    def note(self, message: str) -> None:
        """A note, without the ``cleanporter: note:`` prefix."""

    def error(self, finding: model.Finding) -> None:
        """A file that could not be read, decoded, parsed or written."""

    def patch(self, patch: FilePatch) -> None:
        """A file's rewrite, just made -- and under `Mode.FIX`, just written.

        Not called for a rewrite whose write failed: that is reported to
        `error` instead, and its patch is in `RunResult.patches` with its
        `FilePatch.write_error`.
        """


#: A listener that ignores everything.
_SILENT = Listener()


@dataclasses.dataclass
class _Tally:
    """What `run` accumulates while it walks the files."""

    listener: Listener
    warnings: list[str] = dataclasses.field(default_factory=list)
    notes: list[str] = dataclasses.field(default_factory=list)
    errors: list[model.Finding] = dataclasses.field(default_factory=list)
    findings: list[model.Finding] = dataclasses.field(default_factory=list)
    patches: list[FilePatch] = dataclasses.field(default_factory=list)

    def warn(self, message: str) -> None:
        self.warnings.append(message)
        self.listener.warning(message)

    def note(self, message: str) -> None:
        self.notes.append(message)
        self.listener.note(message)

    def fail(self, finding: model.Finding) -> None:
        self.errors.append(finding)
        self.listener.error(finding)


def run(
    paths: list[pathlib.Path],
    config: config_lib.Config,
    mode: Mode = Mode.CHECK,
    *,
    listener: Listener | None = None,
    whole_project: bool = False,
) -> RunResult:
    """Check -- and under *mode*, diff or fix -- the Python files under *paths*.

    *config* is used as given: the command line's overrides are already
    applied to it (see `config.load_config` for reading ``[tool.cleanporter]``).
    Raises what reading the paths themselves can raise (`OSError`); a file
    that cannot be read, parsed or written is a `RunResult.errors` entry
    instead, and the run goes on. `Config.select` and `Config.ignore` filter
    the findings reported, never what is fixed; `Config.baseline` is not read
    here (see `cleanporter.baseline.apply`).

    With *whole_project*, the whole tree under ``config.root`` is read for
    evidence and only the files under *paths* are fixed, reported and counted;
    a path outside the root, as written or resolved -- listed, or found in a
    listed directory -- refuses the whole run with a `RunResult.errors` entry
    per such path (see the module docstring).
    """
    tally = _Tally(listener or _SILENT)
    listed: list[pathlib.Path] = []
    missing: list[str] = []
    if whole_project:
        listed, missing = discover.iter_python_files(paths, config)
        # The listed paths and every file a listed directory expands to: a
        # symlink inside a listed directory can lead out of the root too.
        escaped = [p for p in dict.fromkeys([*paths, *listed]) if _escapes(p, config.root)]
        for path in escaped:
            tally.fail(_escape_error(path, config.root))
        if escaped:
            # Refuse the run, not just the file: see the module docstring.
            return RunResult(mode, 0, (), (), tuple(tally.errors), ())
    # A whole-project run places a listed path by where it was listed, not by
    # a symlink's target (see `config.find_pyproject`).
    mismatch = config_lib.mismatch_warning(paths, resolve=not whole_project)
    if mismatch is not None:
        tally.warn(mismatch)
    for warning in missing:
        tally.warn(warning)
    if whole_project:
        # Findings name files as a run over ``.`` would, not by absolute path.
        analysed = [_relative(config.root)]
        reported: frozenset[pathlib.Path] | None = frozenset(f.resolve() for f in listed)
    else:
        analysed, reported = paths, None
    # Warmed with the whole tree's pairs, not just the reported files': probe
    # verdicts can depend on the batch they are asked in (`Resolver.warm`).
    project = project_lib.build(analysed, config)
    for note in project.notes:
        tally.note(note)
    for warning in project.warnings:
        tally.warn(warning)
    for error in sorted(project.errors, key=lambda f: (str(f.path), f.line)):
        if reported is None or error.path.resolve() in reported:
            tally.fail(error)
        else:
            tally.warn(
                f"{error.path}: {error.detail}; not a listed file, so the run goes on, "
                "but its imports are missing from the cross-file evidence"
            )

    records = [r for r in project.records if reported is None or r.path.resolve() in reported]
    for rec in records:
        _process(rec, project, mode, tally)
    # A probe batch outside the project's warm-up (a lookup it did not
    # foresee) can fail too; say why as well.
    for warning in project.resolver.take_warnings():
        tally.warn(warning)

    # `select` / `ignore` choose what is reported and counted, and nothing
    # else: the fixer above has already rewritten what it would have anyway.
    findings = [f for f in tally.findings if config.reports(f.code)]
    findings.sort(key=lambda f: (str(f.path), f.line, f.column, f.code))
    return RunResult(
        mode,
        len(records),
        tuple(findings),
        tuple(tally.patches),
        tuple(tally.errors),
        tuple(tally.warnings),
        tuple(tally.notes),
        tuple(r.path for r in records),
    )


def _relative(path: pathlib.Path) -> pathlib.Path:
    """*path* relative to the cwd, or as it is when it cannot be."""
    try:
        return pathlib.Path(os.path.relpath(path, pathlib.Path.cwd()))
    except ValueError:  # pragma: no cover - different drive on Windows
        return path


def _escapes(path: pathlib.Path, root: pathlib.Path) -> bool:
    """Whether listed *path* lies outside *root* as written or as resolved.

    As written, the absolute path with ``..`` folded but symlinks kept must
    have the root among its ancestors -- compared as files, so a root
    reached through a symlinked directory still counts. Resolved, it must
    sit under the resolved root, so a symlink cannot lead out of it.
    """
    return not (path.resolve().is_relative_to(root.resolve()) and _written_inside(path, root))


def _written_inside(path: pathlib.Path, root: pathlib.Path) -> bool:
    """Whether *root* is among the ancestors of *path* as written (see `_escapes`)."""
    real_root = root.resolve()
    written = _absolute(path)
    return any(_same_dir(a, real_root) for a in (written, *written.parents))


def _absolute(path: pathlib.Path) -> pathlib.Path:
    """*path* made absolute with ``..`` folded and symlinks kept.

    `os.path.abspath`, not `pathlib.Path.absolute`: it folds ``..``, so the
    parents of the result are real ancestors; `pathlib.Path.resolve` would
    follow the symlinks this keeps.
    """
    return pathlib.Path(os.path.abspath(path))  # noqa: PTH100 -- see the docstring


def _same_dir(path: pathlib.Path, real_root: pathlib.Path) -> bool:
    if path == real_root:
        return True
    try:
        return path.is_dir() and path.samefile(real_root)
    except OSError:
        return False


def _escape_error(path: pathlib.Path, root: pathlib.Path) -> model.Finding:
    """The `RunResult.errors` entry refusing a whole-project run over *path*."""
    # Inside as written, so outside only once resolved: a symlink leads out.
    how = " through a symlink" if _written_inside(path, root) else ""
    return model.Finding(
        path,
        1,
        0,
        "?",
        "?",
        model.Status.UNRESOLVED,
        f"outside the project root {_relative(root)}{how}; --whole-project judges one "
        "project's files on that project's evidence, so nothing was analysed or written. "
        "Keep it out of this hook with `files:` or `exclude:`",
    )


def _process(
    rec: analyze.FileRecord, project: project_lib.Project, mode: Mode, tally: _Tally
) -> None:
    """Fix (per *mode*) and analyse one file, into *tally*."""
    current = rec
    unread: frozenset[str] = frozenset()
    if mode is not Mode.CHECK:
        outcome = rewrite.fix_record(current, project.resolver, project.config)
        if outcome.status == "fixed":
            current = _apply(current, outcome.source, write=mode is Mode.FIX, tally=tally)
        tally.findings.extend(outcome.blockers)
        unread = outcome.unread
    # Read off the file as it now is, so the lines match its findings'.
    for warning in current.suppressions.warnings(current.path):
        tally.warn(warning)
    tally.findings.extend(analyze.analyze_record(current, project.resolver, project.config, unread))


def _apply(
    rec: analyze.FileRecord, source: str, *, write: bool, tally: _Tally
) -> analyze.FileRecord:
    """Make *rec*'s rewrite a patch and, if *write*, put it on disk.

    Returns the record for what is now on disk. A failed write is a
    file-level error, in which case the file is untouched (the write is
    atomic), and its patch is kept with the error on it but not announced to
    the listener: what the command prints for it is the error alone.

    The patch and the file are both bytes in the file's own encoding and
    newline convention, so a CRLF, Latin-1 or BOM-carrying file gets a patch
    that applies to it and a rewrite that changes only the lines it had to.
    """
    before = rec.raw if rec.raw is not None else rec.source.encode(rec.encoding)
    after = source.encode(rec.encoding)
    error = _write(rec.path, after) if write else None
    patch = FilePatch(
        rec.path, _diff(rec.path, before, after), write and error is None, before, after, error
    )
    tally.patches.append(patch)
    if error is not None:
        tally.fail(error)
        return rec
    tally.listener.patch(patch)
    if not write:
        return rec
    # Report against what is now on disk.
    return _reparse(rec, source, after)


def _write(path: pathlib.Path, data: bytes) -> model.Finding | None:
    """Write *data* to *path* atomically; the file-level error if that fails."""
    try:
        _source.write_atomic(path, data)
    except OSError as exc:
        try:
            notes = exc.__notes__  # e.g. a temporary file that could not be removed
        except AttributeError:
            notes = []
        return model.Finding(
            path,
            1,
            0,
            "?",
            "?",
            model.Status.UNRESOLVED,
            "; ".join([f"cannot write file: {exc.strerror or exc}", *notes]),
        )
    return None


def _diff(path: pathlib.Path, before: bytes, after: bytes) -> bytes:
    """The unified diff from *before* to *after*, headed with *path* (`_diff_path`)."""
    name = os.fsencode(_diff_path(path))
    return _patch_bytes(
        difflib.diff_bytes(
            difflib.unified_diff,
            _source.patch_lines(before),
            _source.patch_lines(after),
            fromfile=b"a/" + name,
            tofile=b"b/" + name,
        )
    )


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


def _patch_bytes(lines: Iterable[bytes]) -> bytes:
    r"""Patch *lines* joined into the bytes a patch file holds.

    A last line with no newline gets the ``\ No newline at end of file``
    marker that ``patch`` and ``git apply`` expect, instead of running into
    the next file's header.
    """
    chunks: list[bytes] = []
    for line in lines:
        chunks.append(line)
        if not line.endswith(b"\n"):
            chunks.append(b"\n\\ No newline at end of file\n")
    return b"".join(chunks)


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
        declared_root=rec.declared_root,
        root_hint=rec.root_hint,
    )

r"""Baselines: the findings a project has accepted for now, and taking them out of a run.

A project adopting cleanporter on an existing codebase has hundreds of
findings it cannot fix in one change. ``--write-baseline FILE`` records them;
``--baseline FILE`` (or ``baseline = "..."`` under ``[tool.cleanporter]``)
then takes every recorded finding out of the report and the exit code, so the
check gates *new* findings from the first day and the backlog is paid down at
its own pace.

**Check runs only.** A baseline is written from, and applied to, a
`engine.Mode.CHECK` result. Under ``--fix`` and ``--diff`` the same import
can be reported differently -- a name nothing reads is a `CP001` in a check
and a `CP003` once the fixer has looked -- and the fixer adds file-level
`CP003` findings that name no import at all, so a check run's baseline does
not describe them. `entries` and `apply` refuse any other mode.

**Identity.** A finding is recorded by what it is, not where it is: its path
relative to the project root (`config.Config.root`), spelled with ``/`` on
every platform; its code; and the ``parent`` and ``name`` it imports -- which
already identify the import, whatever its spelling. No line or column, and
nothing of the statement's text, so a finding survives the lines above it
moving, and any reformatting of its statement: parentheses, line breaks,
backslash continuations, the order of its names, another name added to it,
or a relative spelling swapped for the absolute one. It comes back when the
import changes what it imports (another module or name) or moves to another
file.

`CP001` and `CP003` are one class for matching (`_matching_code`): whether
the fixer would decline an import can depend on cross-file evidence -- a
re-export is load-bearing only when the run includes a file that imports it
-- so a baseline written from ``.`` must still hold for a run over ``src``.
The code recorded stays the one reported, for the reader of the file.

Identical identities are a multiset, not a set: two imports of one name from
one module in one file are two entries, and a third one added later is
reported. Which of the three is reported is not tracked -- with no line in
the key, the entries cannot say which import is the new one -- so the one
reported may be an old import rather than the one just added.

A `CP005` (an unused inline suppression) is about a comment, not an import:
its ``parent`` and ``name`` are empty, so its identity is its file and code,
and a file's unused suppressions are a multiset of identical entries -- the
count is accepted, not the lines. It matches only another `CP005`.

**Staleness.** An entry that matches no finding -- the import was fixed,
changed or deleted -- is *stale*. It is not a failure: fixing a finding must
never fail a run. `apply` counts the stale entries (and says so in a note)
so the file can be rewritten with ``--write-baseline`` to drop them. Only an
entry that *could* have matched counts: one for a file this run did not
check, or for codes it does not report (`config.Config.reports`), is not
stale, merely not looked at.

**Format.** JSON, ``{"version": 1, "findings": [...]}``, one object per
entry with ``path``, ``code``, ``parent`` and ``name``, sorted, indented,
``\n``-terminated and UTF-8 -- deterministic, so the file diffs well in
version control. `CP004` is never written: it never fails a run, so there is
nothing to accept.
"""

from __future__ import annotations

import collections
import contextlib
import dataclasses
import json
import os
import pathlib

from cleanporter import config as config_lib
from cleanporter import engine, model

from . import _source

#: The ``version`` this module writes and the only one it reads.
FORMAT_VERSION = 1

_FIELDS = ("path", "code", "parent", "name")

#: Codes that match each other's entries: see the module docstring.
_SAME_CLASS = {"CP003": "CP001"}


class BaselineError(ValueError):
    """A baseline file that cannot be read, or is not a baseline."""


@dataclasses.dataclass(frozen=True, order=True)
class Entry:
    """One accepted finding's identity (see the module docstring)."""

    #: Relative to the project root, POSIX-spelled.
    path: str
    #: The code it was reported with; `CP001` and `CP003` match each other.
    code: str
    parent: str
    name: str


def _matching_code(code: str) -> str:
    return _SAME_CLASS.get(code, code)


def _codes_of_class(code: str) -> set[str]:
    """Every code that matches an entry recorded with *code*."""
    target = _matching_code(code)
    return {target} | {c for c, into in _SAME_CLASS.items() if into == target}


def _match_key(e: Entry) -> tuple[str, str, str, str]:
    return (e.path, _matching_code(e.code), e.parent, e.name)


def relative_path(path: pathlib.Path, root: pathlib.Path) -> str:
    """*path* relative to *root*, with ``/`` separators on every platform.

    Both are resolved first, so a finding named relative to the cwd and one
    named absolutely key alike. A path that cannot be made relative (another
    drive on Windows) keeps its absolute POSIX spelling.
    """
    resolved = path.resolve()
    try:
        relative = os.path.relpath(resolved, root.resolve())
    except ValueError:  # pragma: no cover - different drive on Windows
        return resolved.as_posix()
    return pathlib.PurePath(relative).as_posix()


def entry(finding: model.Finding, root: pathlib.Path) -> Entry:
    """*finding*'s baseline identity in a project rooted at *root*."""
    return Entry(relative_path(finding.path, root), finding.code, finding.parent, finding.name)


def _require_check(result: engine.RunResult) -> None:
    if result.mode is not engine.Mode.CHECK:
        msg = f"a baseline describes a check run, not a {result.mode.value} run"
        raise ValueError(msg)


def entries(result: engine.RunResult, root: pathlib.Path) -> list[Entry]:
    """What a baseline of *result* records: every finding but `CP004`, sorted.

    Raises `ValueError` unless *result* is from a `engine.Mode.CHECK` run.
    """
    _require_check(result)
    return sorted(
        entry(f, root) for f in result.findings if f.status is not model.Status.SKIPPED_BY_CONFIG
    )


def dumps(baseline: list[Entry]) -> str:
    """*baseline* as the file's text (see the module docstring)."""
    document = {
        "version": FORMAT_VERSION,
        "findings": [dataclasses.asdict(e) for e in sorted(baseline)],
    }
    return json.dumps(document, indent=2) + "\n"


def write(path: pathlib.Path, baseline: list[Entry]) -> None:
    r"""Write *baseline* to *path*, atomically, with ``\n`` newlines everywhere.

    Replacing an existing file is atomic (`_source.write_atomic`). A new
    one is created empty first, since the atomic write replaces a file, and
    removed again if the write fails, so a failure leaves nothing behind.
    """
    created = not path.exists()
    path.touch(exist_ok=True)
    try:
        _source.write_atomic(path, dumps(baseline).encode("utf-8"))
    except BaseException:
        if created:
            with contextlib.suppress(OSError):
                path.unlink()
        raise


def loads(text: str) -> list[Entry]:
    """The entries of a baseline file's *text*; `BaselineError` if it is not one."""
    try:
        document: object = json.loads(text)
    except json.JSONDecodeError as exc:
        msg = f"not JSON: {exc}"
        raise BaselineError(msg) from exc
    if not isinstance(document, dict):
        msg = "not a baseline: expected a JSON object"
        raise BaselineError(msg)
    version: object = document.get("version")
    if version != FORMAT_VERSION:
        msg = f"unsupported baseline version {version!r}; this cleanporter reads {FORMAT_VERSION}"
        raise BaselineError(msg)
    findings: object = document.get("findings")
    if not isinstance(findings, list):
        msg = "not a baseline: 'findings' must be a list"
        raise BaselineError(msg)
    return [_entry(item, position) for position, item in enumerate(findings, start=1)]


def _entry(item: object, position: int) -> Entry:
    if not isinstance(item, dict):
        msg = f"findings[{position}] must be an object"
        raise BaselineError(msg)
    values: list[str] = []
    for field in _FIELDS:
        value: object = item.get(field)
        if not isinstance(value, str):
            msg = f"findings[{position}].{field} must be a string"
            raise BaselineError(msg)
        values.append(value)
    path, code, parent, name = values
    return Entry(path, code, parent, name)


def load(path: pathlib.Path) -> list[Entry]:
    """The entries of the baseline file at *path*; `BaselineError` if it cannot be."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        msg = f"cannot read baseline {path}: {exc}"
        raise BaselineError(msg) from exc
    try:
        return loads(text)
    except BaselineError as exc:
        msg = f"baseline {path}: {exc}"
        raise BaselineError(msg) from exc


def apply(
    result: engine.RunResult, baseline: list[Entry], config: config_lib.Config
) -> engine.RunResult:
    """*result* without the findings *baseline* accepts, counting what it took and left.

    Pure: nothing is read or printed. Each entry takes out at most one
    finding (a multiset). The returned result's `engine.RunResult.baselined`
    and `engine.RunResult.stale_baseline` say how many were taken out and
    how many entries that could have matched did not; when any are stale, a
    note saying so is added to its `engine.RunResult.notes`. Raises
    `ValueError` unless *result* is from a `engine.Mode.CHECK` run.
    """
    _require_check(result)
    remaining = collections.Counter(_match_key(e) for e in baseline)
    kept: list[model.Finding] = []
    for finding in result.findings:
        key = _match_key(entry(finding, config.root))
        if remaining[key] > 0:
            remaining[key] -= 1
        else:
            kept.append(finding)
    checked = {relative_path(p, config.root) for p in result.checked}
    stale = sum(
        count
        for (path, code, _, _), count in remaining.items()
        if count > 0 and path in checked and any(config.reports(c) for c in _codes_of_class(code))
    )
    notes = result.notes
    if stale:
        notes = (*notes, stale_note(stale))
    return dataclasses.replace(
        result,
        findings=tuple(kept),
        notes=notes,
        baselined=len(result.findings) - len(kept),
        stale_baseline=stale,
    )


def stale_note(count: int) -> str:
    """The note `apply` adds for *count* stale entries."""
    what = "entry matches" if count == 1 else "entries match"
    return (
        f"{count} baseline {what} no finding any more (fixed, changed or removed); "
        "rewrite the baseline with --write-baseline to drop them"
    )

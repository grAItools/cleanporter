r"""Baselines: the findings a project has accepted for now, and taking them out of a run.

A project adopting cleanporter on an existing codebase has hundreds of
findings it cannot fix in one change. ``--write-baseline FILE`` records them;
``--baseline FILE`` (or ``baseline = "..."`` under ``[tool.cleanporter]``)
then takes every recorded finding out of the report and the exit code, so the
check gates *new* findings from the first day and the backlog is paid down at
its own pace.

**Identity.** A finding is recorded by what it is, not where it is: its path
relative to the project root (`config.Config.root`), spelled with ``/`` on
every platform; its code; its parent and name; and a hash of the text of the
``from`` statement it is about, with every run of whitespace collapsed to one
space (`statement_hash`). No line or column, so a finding survives the lines
above it moving. Editing the statement -- another name, another alias, a
different module -- changes the hash, and every finding of that statement
comes back: whoever touches an import is asked to fix it. A finding with no
statement (a file-level `CP003` from the fixer) hashes the empty text.

Identical identities are a multiset, not a set: two copies of one statement
in a file are two entries, and a third copy added later is reported.

**Staleness.** An entry that matches no finding -- the import was fixed,
edited or deleted -- is *stale*. It is not a failure: fixing a finding must
never fail a run. `apply` counts the stale entries (and says so in a note)
so the file can be rewritten with ``--write-baseline`` to drop them. Only an
entry that *could* have matched counts: one for a file this run did not
check, or for a code it does not report (`config.Config.reports`), is not
stale, merely not looked at.

**Format.** JSON, ``{"version": 1, "findings": [...]}``, one object per
entry with ``path``, ``code``, ``parent``, ``name`` and ``statement`` (the
hash), sorted, indented, ``\n``-terminated and UTF-8 -- deterministic, so the
file diffs well in version control. `CP004` is never written: it never fails
a run, so there is nothing to accept.
"""

from __future__ import annotations

import collections
import dataclasses
import hashlib
import json
import os
import pathlib

from cleanporter import config as config_lib
from cleanporter import engine, model

#: The ``version`` this module writes and the only one it reads.
FORMAT_VERSION = 1

_FIELDS = ("path", "code", "parent", "name", "statement")


class BaselineError(ValueError):
    """A baseline file that cannot be read, or is not a baseline."""


@dataclasses.dataclass(frozen=True, order=True)
class Entry:
    """One accepted finding's identity (see the module docstring)."""

    #: Relative to the project root, POSIX-spelled.
    path: str
    code: str
    parent: str
    name: str
    #: `statement_hash` of the finding's ``from`` statement.
    statement: str


def statement_hash(statement: str) -> str:
    """A short, stable hash of *statement* with its whitespace collapsed."""
    normal = " ".join(statement.split())
    # surrogatepass: a statement is source text, and may name an odd path.
    return hashlib.sha256(normal.encode("utf-8", "surrogatepass")).hexdigest()[:16]


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
    return Entry(
        relative_path(finding.path, root),
        finding.code,
        finding.parent,
        finding.name,
        statement_hash(finding.statement),
    )


def entries(result: engine.RunResult, root: pathlib.Path) -> list[Entry]:
    """What a baseline of *result* records: every finding but `CP004`, sorted."""
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
    r"""Write *baseline* to *path*, as bytes so the newlines are ``\n`` everywhere."""
    path.write_bytes(dumps(baseline).encode("utf-8"))


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
    path, code, parent, name, statement = values
    return Entry(path, code, parent, name, statement)


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
    note saying so is added to its `engine.RunResult.notes`.
    """
    remaining = collections.Counter(baseline)
    kept: list[model.Finding] = []
    for finding in result.findings:
        key = entry(finding, config.root)
        if remaining[key] > 0:
            remaining[key] -= 1
        else:
            kept.append(finding)
    checked = {relative_path(p, config.root) for p in result.checked}
    stale = sum(
        count
        for key, count in remaining.items()
        if count > 0 and key.path in checked and config.reports(key.code)
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
        f"{count} baseline {what} no finding any more (fixed, edited or removed); "
        "rewrite the baseline with --write-baseline to drop them"
    )

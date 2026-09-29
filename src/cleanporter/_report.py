r"""Machine-readable reports of a finished run: ``--format json|sarif|github``.

Every function here is pure: it takes an `engine.RunResult` and the few
facts about the invocation the result does not carry (``--strict``,
``--show-skipped``, the working directory), and returns the text to put on
stdout. `cli.run` decides *when* to print it -- once, after the run -- and
keeps streaming warnings, notes and ``fixed:`` lines to stderr as it always
has, so a human watching a CI log still sees them.

The three formats agree with the text report, and with each other, on what
is shown and what it means:

* the findings shown are the text report's: every one but `CP004`, which
  appears only under ``--show-skipped`` -- though the counts include it
  either way, as the summary line's do;
* a finding's severity is its effect on the exit code: `CP001`, `CP003` and
  `CP005` fail the run, so they are errors; `CP002` is a warning, or an error under
  ``--strict``; `CP004` never fails a run, so it is a note;
* a file that could not be processed is not a finding, in the text report
  or here: it is listed apart (``errors`` in JSON, a tool execution
  notification in SARIF) and it is what makes the exit code 2.

Columns: `model.Finding.column` is 0-based, counted in code points as libcst
counts and as the text report prints it, and so is JSON's ``column``. SARIF
and GitHub count from 1, and get ``column + 1``.

Paths are whatever the filesystem holds, and on POSIX that need not be
UTF-8: a name Python decoded with ``surrogateescape`` carries lone
surrogates. JSON escapes them (``\udcff``); SARIF percent-encodes the
original bytes (`_uri`); GitHub's ``file=`` gets them backslash-escaped. None
of the three can fail to encode, so an odd filename cannot turn a report into
a traceback.

The module is private: its output formats are the documented interface, not
these functions.
"""

from __future__ import annotations

import base64
import dataclasses
import json
import os
import pathlib
import urllib.parse
from collections.abc import Callable

import cleanporter
from cleanporter import engine, model

#: Bumped when a key is removed or changes meaning; adding a key does not.
JSON_FORMAT_VERSION = 1

#: Where each finding code is documented; SARIF's ``helpUri``.
HELP_URI = "https://graitools.github.io/cleanporter/usage/#finding-codes"
INFORMATION_URI = "https://graitools.github.io/cleanporter/"
SARIF_SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"

type Json = bool | int | str | list[Json] | dict[str, Json] | None


@dataclasses.dataclass(frozen=True)
class _Rule:
    """One SARIF rule: a finding code and what to say about it."""

    id: str
    name: str
    short: str
    full: str
    level: str


_RULES: tuple[_Rule, ...] = (
    _Rule(
        "CP001",
        "object-import",
        "An object is imported by name; import its module instead.",
        "Google Python Style Guide section 2.2: 'from P import S' is used only when S is a "
        "module. cleanporter proved that S is not a module, so the import should name the "
        "module and each use should go through it. `cleanporter --fix` rewrites it where that "
        "is provably safe.",
        "error",
    ),
    _Rule(
        "CP002",
        "unresolved",
        "Could not prove whether the imported name is a module or an object.",
        "cleanporter never guesses: the parent could not be imported by the probe "
        "interpreter, the name is both a submodule and a binding, or a first-party name has "
        "no evidence on disk. Never rewritten; fails the run only under --strict.",
        "warning",
    ),
    _Rule(
        "CP003",
        "not-rewritten",
        "A violation deliberately not rewritten; it still fails the run.",
        "Structurally a violation, but the fixer declined to rewrite it, for the reason in "
        "the message (a wildcard import, a re-export, a rewrite it cannot prove safe). The "
        "file is left byte-identical; a human decides what to do.",
        "error",
    ),
    _Rule(
        "CP004",
        "skipped-by-config",
        "Taken out of the run by a skip rule or an inline suppression; never fails the run.",
        "The import matched a skip rule in [tool.cleanporter.skip], or a "
        "'# cleanporter: ignore[CODE]' comment named the code of its finding, so that "
        "finding was not reported. This is the project's own configuration reporting "
        "back, listed only under --show-skipped.",
        "note",
    ),
    _Rule(
        "CP005",
        "unused-suppression",
        "An inline suppression comment suppressed nothing; it fails the run.",
        "A '# cleanporter: ignore[CODE]' comment names a code no finding on the imports "
        "it covers has: the finding was fixed, or the comment sits on the wrong line. "
        "Remove it (or the unused code) before it silences the next finding to land there.",
        "error",
    ),
)
_RULE_INDEX = {rule.id: index for index, rule in enumerate(_RULES)}

#: The GitHub workflow command for each level.
_GITHUB_COMMANDS = {"error": "error", "warning": "warning", "note": "notice"}


@dataclasses.dataclass(frozen=True)
class _Options:
    """What a report needs to know about the invocation beyond the result."""

    strict: bool
    show_skipped: bool
    #: The directory SARIF's ``SRCROOT`` and GitHub's ``file=`` are relative to.
    cwd: pathlib.Path


def render(
    fmt: str,
    result: engine.RunResult,
    *,
    strict: bool,
    show_skipped: bool,
    cwd: pathlib.Path,
) -> str:
    """*result* in format *fmt* (``json``, ``sarif`` or ``github``), ending in a newline."""
    renderers: dict[str, Callable[[engine.RunResult, _Options], str]] = {
        "json": _json,
        "sarif": _sarif,
        "github": _github,
    }
    return renderers[fmt](result, _Options(strict, show_skipped, cwd))


def _shown(result: engine.RunResult, options: _Options) -> list[model.Finding]:
    """The findings the text report would print: `CP004` only under ``--show-skipped``."""
    return [
        f
        for f in result.findings
        if options.show_skipped or f.status is not model.Status.SKIPPED_BY_CONFIG
    ]


def level(finding: model.Finding, *, strict: bool) -> str:
    """``error``, ``warning`` or ``note``: the finding's effect on the exit code."""
    if finding.status is model.Status.UNRESOLVED:
        return "error" if strict else "warning"
    if finding.status is model.Status.SKIPPED_BY_CONFIG:
        return "note"
    return "error"


def _dump(document: Json) -> str:
    # ASCII: a surrogate-escaped path, or a stdout that is not UTF-8, cannot break it.
    return json.dumps(document, indent=2) + "\n"


# -- json ---------------------------------------------------------------------


def _json(result: engine.RunResult, options: _Options) -> str:
    document: dict[str, Json] = {
        "tool": {"name": "cleanporter", "version": cleanporter.__version__},
        "format_version": JSON_FORMAT_VERSION,
        "mode": result.mode.value,
        "strict": options.strict,
        "exit_code": result.exit_code(strict=options.strict),
        "counts": {
            "files_checked": result.files_checked,
            "changed": result.changed,
            "violations": result.violations,
            "not_rewritten": result.skipped,
            "unresolved": result.unresolved,
            "skipped_by_config": result.skipped_by_config,
            "unused_suppressions": result.unused_suppressions,
            "errors": len(result.errors),
        },
        "findings": [_json_finding(f, strict=options.strict) for f in _shown(result, options)],
        "errors": [_json_error(e) for e in result.errors],
        "warnings": list[Json](result.warnings),
        "notes": list[Json](result.notes),
        "patches": [_json_patch(p) for p in result.patches],
    }
    return _dump(document)


def _json_finding(finding: model.Finding, *, strict: bool) -> Json:
    return {
        "code": finding.code,
        "status": finding.status.value,
        "level": level(finding, strict=strict),
        "path": str(finding.path),
        "line": finding.line,
        "column": finding.column,
        "parent": finding.parent,
        "name": finding.name,
        "message": finding.message,
        "detail": finding.detail,
    }


def _json_error(error: model.Finding) -> Json:
    return {
        "code": error.code,
        "path": str(error.path),
        "line": error.line,
        "column": error.column,
        "message": error.message,
    }


def _json_patch(patch: engine.FilePatch) -> Json:
    """A patch's diff as text when it is UTF-8, else base64 -- never lossily decoded.

    The diff is bytes in the file's own encoding (see `engine.FilePatch`),
    so a Latin-1 file's patch is not text JSON can carry faithfully.
    """
    try:
        diff, encoding = patch.diff.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        diff, encoding = base64.b64encode(patch.diff).decode("ascii"), "base64"
    write_error = patch.write_error.message if patch.write_error is not None else None
    return {
        "path": str(patch.path),
        "written": patch.written,
        "write_error": write_error,
        "diff_encoding": encoding,
        "diff": diff,
    }


# -- sarif --------------------------------------------------------------------


def _relative(path: pathlib.Path, cwd: pathlib.Path) -> str | None:
    """*path* relative to *cwd*, POSIX-spelled; ``None`` when it is outside *cwd*."""
    absolute = path if path.is_absolute() else cwd / path
    try:
        relative = os.path.relpath(absolute, cwd)
    except ValueError:  # pragma: no cover - different drive on Windows
        return None
    posix = pathlib.PurePath(relative).as_posix()
    return None if posix == ".." or posix.startswith("../") else posix


def _uri(posix: str) -> str:
    """*posix*, a POSIX-spelled path, as a URI path: its bytes, percent-encoded.

    Encoding the *bytes* (`os.fsencode`) is what makes a filename that is not
    valid UTF-8 -- carried in a `str` as lone surrogates -- a URI rather than
    a `UnicodeEncodeError`.
    """
    return urllib.parse.quote(os.fsencode(posix), safe="/")


def _file_uri(path: pathlib.PurePath, *, directory: bool = False) -> str:
    r"""The ``file:`` URI of the absolute *path*; with a trailing ``/`` for a *directory*.

    Built by hand rather than by `pathlib.Path.as_uri`, which fails on a
    surrogate-escaped name. A Windows drive letter keeps its colon, as
    ``as_uri`` keeps it (``file:///C:/x``): percent-encoded, ``C%3A`` is a
    spelling SARIF viewers do not map back to the file. A UNC path names its
    server as the URI's authority, again as ``as_uri`` does:
    ``\\server\share\x`` is ``file://server/share/x``. An extended-length
    path to a drive, ``\\?\C:\x``, is that drive's ``file:///C:/x``.
    """
    posix = path.as_posix()
    drive = ""
    unc = False
    if path.drive.endswith(":"):  # C:/x -> /C: + /x; \\?\C:\x -> /C: + /x
        drive, posix = f"/{path.drive[-2:]}", posix[len(path.drive) :]
    elif path.drive.startswith("\\\\"):  # UNC, //server/share/x: the server is the authority
        unc = True
    if directory and not posix.endswith("/"):
        posix = f"{posix}/"
    if unc:
        return f"file:{_uri(posix)}"
    return f"file://{drive}{_uri(posix)}"


def _sarif_location(path: pathlib.Path, line: int, column: int, cwd: pathlib.Path) -> Json:
    relative = _relative(path, cwd)
    artifact: dict[str, Json]
    if relative is None:
        artifact = {"uri": _file_uri(path if path.is_absolute() else cwd / path)}
    else:
        artifact = {"uri": _uri(relative), "uriBaseId": "SRCROOT"}
    return {
        "physicalLocation": {
            "artifactLocation": artifact,
            "region": {"startLine": line, "startColumn": column + 1},
        }
    }


def _sarif(result: engine.RunResult, options: _Options) -> str:
    rules: list[Json] = [_sarif_rule(rule) for rule in _RULES]
    results: list[Json] = [
        _sarif_result(f, strict=options.strict, cwd=options.cwd) for f in _shown(result, options)
    ]
    notifications: list[Json] = [
        {
            "level": "error",
            "message": {"text": error.message},
            "locations": [_sarif_location(error.path, error.line, error.column, options.cwd)],
        }
        for error in result.errors
    ]
    notifications += [_notification("warning", w) for w in result.warnings]
    notifications += [_notification("note", n) for n in result.notes]
    document: dict[str, Json] = {
        "$schema": SARIF_SCHEMA,
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "cleanporter",
                        "version": cleanporter.__version__,
                        "informationUri": INFORMATION_URI,
                        "rules": rules,
                    }
                },
                "originalUriBaseIds": {"SRCROOT": {"uri": _file_uri(options.cwd, directory=True)}},
                "columnKind": "unicodeCodePoints",
                "invocations": [
                    {
                        "executionSuccessful": not result.errors,
                        "exitCode": result.exit_code(strict=options.strict),
                        "toolExecutionNotifications": notifications,
                    }
                ],
                "results": results,
            }
        ],
    }
    return _dump(document)


def _sarif_rule(rule: _Rule) -> Json:
    return {
        "id": rule.id,
        "name": rule.name,
        "shortDescription": {"text": rule.short},
        "fullDescription": {"text": rule.full},
        "help": {
            "text": f"{rule.full} See {HELP_URI}",
            "markdown": f"{rule.full}\n\nSee [finding codes]({HELP_URI}).",
        },
        "helpUri": HELP_URI,
        "defaultConfiguration": {"level": rule.level},
    }


def _notification(level: str, text: str) -> Json:
    """A tool execution notification with no location: a warning or a note."""
    return {"level": level, "message": {"text": text}}


def _sarif_result(finding: model.Finding, *, strict: bool, cwd: pathlib.Path) -> Json:
    properties: dict[str, Json] = {"parent": finding.parent, "name": finding.name}
    return {
        "ruleId": finding.code,
        "ruleIndex": _RULE_INDEX[finding.code],
        "level": level(finding, strict=strict),
        "message": {"text": finding.message},
        "locations": [_sarif_location(finding.path, finding.line, finding.column, cwd)],
        "properties": properties,
    }


# -- github -------------------------------------------------------------------


def escape_data(value: str) -> str:
    """*value* escaped as a workflow command's message."""
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def escape_property(value: str) -> str:
    """*value* escaped as a workflow command's property (``file=``, ``title=``)."""
    return escape_data(value).replace(":", "%3A").replace(",", "%2C")


def _github_command(command: str, finding: model.Finding, cwd: pathlib.Path) -> str:
    """One annotation, titled with the finding's code."""
    relative = _relative(finding.path, cwd)
    spelled = relative if relative is not None else str(finding.path)
    # A surrogate-escaped name cannot be written to stdout; spell its bytes.
    file = os.fsencode(spelled).decode("utf-8", "backslashreplace")
    properties = ",".join(
        f"{key}={escape_property(value)}"
        for key, value in (
            ("file", file),
            ("line", str(finding.line)),
            ("col", str(finding.column + 1)),
            ("title", finding.code),
        )
    )
    return f"::{command} {properties}::{escape_data(finding.message)}"


def _github(result: engine.RunResult, options: _Options) -> str:
    lines = [_github_command("error", error, options.cwd) for error in result.errors]
    lines += [
        _github_command(_GITHUB_COMMANDS[level(f, strict=options.strict)], f, options.cwd)
        for f in _shown(result, options)
    ]
    return "".join(f"{line}\n" for line in lines)

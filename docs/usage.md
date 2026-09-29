# Usage

cleanporter is one command. By default it *checks*; passing `--fix` makes it
*rewrite*.

```text
cleanporter [--fix] [--diff] [--python PATH] [--exempt MODULE] [--root PATH]
            [--strict] [--whole-project] [--show-skipped]
            [--select CODES] [--ignore CODES]
            [--baseline FILE] [--write-baseline FILE]
            [--format {text,json,sarif,github}] [--version] [paths ...]
```

## Positional arguments

| Argument | Meaning |
| --- | --- |
| `paths` | Files or directories to process. Defaults to `.`. Directories are walked recursively for `*.py` files; a path you name explicitly is always processed, even if it is excluded by configuration. |

## Flags

| Flag | Meaning |
| --- | --- |
| `--fix` | Rewrite violations in place, but only where the rewrite is provably safe. Files that cannot be proven safe are left byte-for-byte unchanged and reported as `CP003`. |
| `--diff` | Show the rewrite as a unified diff on stdout without writing anything. Ignored when `--fix` is also given: `--fix` wins and writes (it prints the same diff on its way through). |
| `--python PATH` | Interpreter used to classify stdlib and third-party names: a path or command, `auto` (the default: detect the project's own, [as described here](configuration.md#the-probe-interpreter)), or `self` (the one running cleanporter). |
| `--exempt MODULE` | An additional module whose members may be imported by name. Repeatable. Adds to, never replaces, the [default exemptions](configuration.md#default-exemptions) and anything in `exempt_modules`. |
| `--root PATH` | An additional first-party import root — a directory that is on `sys.path` for the code being analysed. Repeatable. Adds to whatever the analysed paths themselves imply, and to `source_roots`. A relative value is resolved against the directory holding your `pyproject.toml`, not against the current directory. |
| `--strict` | Also fail (exit `1`) on imports that could not be classified (`CP002`). Equivalent to turning on `treat_unresolved_as_error` for this run. |
| `--whole-project` | Read the whole project for evidence, but fix, report and count only the files under `paths`. The project is the directory of the `pyproject.toml` the listed paths sit under — looked up from each path as written, not from a symlink's target — and every listed path that has one must share it: paths from two projects (a nested `examples/` project beside its parent, say) exit `2`, as does a list where none has one. Every listed path, and every file a listed directory expands to, must also lie inside that directory, both as written and with symlinks resolved: one that does not (a script under no `pyproject.toml`, a symlink into another project) is reported as `file not processed`, and the whole run exits `2` with nothing analysed or written. The project is walked as `cleanporter .` run from that directory would walk it. A listed file the walk leaves out — matched by `exclude`, or in a skipped directory — is not reported. A file elsewhere in the tree that cannot be parsed is a warning, not exit `2`. Built for [pre-commit](pre-commit.md), which passes only the changed files: see there for what a run over those alone gets wrong. |
| `--show-skipped` | List the imports a [`skip` rule](configuration.md#skip-rules) or an [inline suppression](configuration.md#inline-suppressions) took out of the run (`CP004`). They are counted in the summary either way; this prints them, which is how you check what a pattern actually swallowed. |
| `--select CODES` | Report and count only these finding codes: comma-separated (`CP001,CP003`), case-insensitive, repeatable. Replaces `select` from the configuration. It filters the *report*: `--fix` still rewrites exactly what it would have, and the exit code follows only the selected codes — `--select CP002` exits `1` on a `CP002` only under `--strict`, as without it. `CP004` is still printed only under `--show-skipped`. An unknown code is a usage error (exit `2`). |
| `--ignore CODES` | Neither report nor count these finding codes; the same syntax as `--select`, applied after it, and replacing `ignore` from the configuration. Reporting only, too: `--fix --ignore CP001` still rewrites every `CP001` it can — it just does not list the ones it cannot. |
| `--baseline FILE` | Leave out the findings recorded in a baseline file (written by `--write-baseline`), so only *new* findings are reported and counted. Replaces `baseline` from the configuration; a relative path is read from the current directory. A recorded finding that is gone is counted in a note on stderr, never a failure. A missing or malformed file, or no `pyproject.toml` above the first path, exits `2`. Check runs only: with `--fix` or `--diff` it is a usage error, and a configured `baseline` is skipped under them with a note. See [Adopting cleanporter on an existing codebase](#adopting-cleanporter-on-an-existing-codebase). |
| `--write-baseline FILE` | Record the current findings in `FILE` instead of reporting them, and exit `0`. Records what the run reports — after `--select` and `--ignore`, before any baseline, and never `CP004` — so the file always holds the complete current state. Check mode only: combined with `--fix`, `--diff` or `--format` it is a usage error (exit `2`). Needs a `pyproject.toml` above the first path. A run in which a file could not be read or parsed writes nothing and exits `2`. |
| `--format FORMAT` | How to report: `text` (the default, the human report described on this page), `json`, `sarif` (SARIF 2.1.0, for code scanning) or `github` (GitHub Actions workflow commands, which annotate the lines in a pull request). A structured format puts one document on stdout and nothing else; see [Machine-readable output](#machine-readable-output). `--diff` cannot be combined with `sarif` or `github`, not even alongside `--fix`. |
| `--version` | Print the version and exit. |
| `--help` | Print usage and exit. |

Every flag except `--python`, `--select`, `--ignore`, `--baseline`,
`--write-baseline`, `--format` and `--version` is additive with the
configuration file rather than overriding it — see
[Configuration](configuration.md#how-cli-flags-layer-on-top-of-config).
`--format` has no configuration key at all: the format is chosen by whoever
reads the output — a CI step, an editor, a terminal — not by the project, and a
`pyproject.toml` that turned every local run into JSON would be a surprise.

## Finding codes

Each reported line has the shape
`PATH:LINE:COLUMN: CODE message`.

| Code | Status | Meaning |
| --- | --- | --- |
| `CP001` | `VIOLATION` | An object is imported by name. This is the rule being enforced, and it is what blocks CI. The message names the module to import instead; for a relative import it also quotes the replacement statement, spelled relative as `--fix` would write it (`import the module ('from . import helpers') and use 'helpers.Widget'`), since the absolute module name it reports is only as good as the inferred import root. |
| `CP002` | `UNRESOLVED` | cleanporter could not determine whether the symbol is a module: a third-party parent it cannot import, a name that is both a submodule and a binding in its package's `__init__`, or a first-party name that is neither on disk nor bound in its parent to something cleanporter can follow (a generated `_version.py`, a `_pb2`, an out-of-tree extension, another portion of a namespace package). The message says which evidence was missing. Never rewritten. Only counts toward the failure exit code under `--strict` / `treat_unresolved_as_error`. The same code also marks a whole file that was not processed (`file not processed: …`, when it could not be read, decoded, parsed or written); those lines are not findings and always make the exit code `2`, with or without `--strict` — see [Exit codes](#exit-codes). |
| `CP003` | `SKIPPED` | Structurally a violation, deliberately not rewritten. Under `--fix` or `--diff` it is the "declined, because…" note explaining why a file, or one import in it, was left alone; a few reasons that belong to the import itself are reported in every mode (below). |
| `CP004` | `SKIPPED_BY_CONFIG` | Matched a [`skip` rule](configuration.md#skip-rules), so it was never analysed — or an [inline suppression](configuration.md#inline-suppressions) (`# cleanporter: ignore[CP001]`) named the code of its finding, and the message says which code and which comment. Counted in the summary, printed only under `--show-skipped`, and **never** part of the exit code — you asked for it. |
| `CP005` | `UNUSED_SUPPRESSION` | An [inline suppression](configuration.md#inline-suppressions) names a code that no finding on the imports it covers has — the finding was fixed, or the comment is on the wrong line. Reported at the comment. Like `CP001`, it makes the run exit `1`, and it cannot itself be suppressed: remove the comment, or the code, instead. |

Examples of each:

```text
src/mypkg/consumer.py:3:0: CP001 imports object 'Widget' from module 'mypkg.helpers'; import the module and use 'helpers.Widget'
src/mypkg/gpu.py:5:0: CP002 could not determine whether 'cupy.ndarray' is a module: 'cupy' is not importable in the target interpreter
src/mypkg/api.py:11:0: CP003 file not rewritten: local 'Widget' is rebound in the same scope
src/mypkg/stencils.py:4:0: CP004 'broadcast' from 'gt4py.next' skipped by configuration: skip rule #1 (decorator='field_operator'): DSL bodies are re-parsed by the frontend
src/mypkg/compat.py:7:31: CP005 unused suppression: no CP002 finding on the imports this comment covers; remove the comment
```

`CP002` findings are only produced for imports cleanporter actually looked at:
exempt modules and (under `scope = "first-party"`) stdlib and third-party
modules are passed over before they are classified. Under that scope the probe
still classifies a third-party name that one of your own modules re-exports,
because an import of it from your package depends on what it is.

Nothing at all is reported for a module-level import in a package's
`__init__.py`: it is the package's public surface, never reported or
rewritten (see [A package's `__init__.py`](configuration.md#a-packages-__init__py)).
An import inside a function there is checked like any other.

!!! note "`CP003` findings count toward the failure exit code"

    A file the fixer declined still contains a violation, so `CP003` is not
    purely informational: like `CP001`, it makes the run exit `1`.

    Most `CP003` findings are the fixer explaining a decision, so they only
    appear under `--fix` or `--diff`. The exceptions are reasons that belong
    to the import itself, which are reported as `CP003` in every mode: a
    wildcard import (`from x import *`), for which no module import is a
    replacement; an explicit or load-bearing re-export, or one a string in
    another file names by its dotted path; a replacement that
    cannot be shown to bind the module it names; and `from . import C` where
    the package is top-level, which has no relative replacement (unless its
    import root is declared; see
    [Known limitations](safety.md#known-limitations)).

!!! note "`CP004` findings never count"

    A `CP004` is your own configuration reporting back, not a problem found in
    your code, so it never contributes to the failure exit code — not even
    under `--strict`. It is still counted in the summary line, because a rule
    broad enough to swallow a project should be visible without having to go
    looking for it.

    That is a claim about `CP004`, not about `skip` rules in general. A rule
    can still change a run's exit code, in one direction: if it matches the
    spelling `--fix` is about to *write* rather than the one in your source,
    the file is declined with a `CP003`, which does count. See
    [when a rewrite would create its own skipped region](safety.md#what-a-skip-rule-can-and-cannot-do).

!!! note "`CP005` findings count toward the failure exit code"

    A suppression that suppresses nothing is stale: left in place, it would
    silently swallow the next finding of its code to land on its line. So it
    fails the run like a `CP001`, whatever the other flags. A *malformed*
    suppression — `# cleanporter: ignore` with no codes, an unknown code, or
    `CP004`/`CP005` in the brackets — is not a finding but a warning naming
    its file and line, and it suppresses nothing.

## Exit codes

| Code | Meaning |
|-----:| --- |
| `0` | Clean — nothing remains to report. |
| `1` | Violations found (or left behind after `--fix`). |
| `2` | Operational error: a file that could not be read, decoded, parsed or (under `--fix`) written, a malformed `[tool.cleanporter]` table, a usage error (an unknown `--select` / `--ignore` code, say), a baseline file that is missing or malformed, or `--baseline` / `--write-baseline` (or a configured `baseline`) with no `pyproject.toml` above the first path. |

In short: 0 = clean, 1 = violations, 2 = operational error.

Exit `2` takes precedence: if any input file could not be processed, the run
reports `2` regardless of what else it found. A path on the command line that
does not exist is a *warning*, not an error — it is reported and skipped.

A file that cannot be processed is reported on its own line, in the same
`PATH:LINE:COLUMN: CODE message` shape as a finding, and **every other file is
still checked and fixed**:

```text
src/mypkg/legacy.py:12:0: CP002 file not processed: cannot decode file as utf-8: invalid start byte (b'\xff')
src/mypkg/odd.py:1:0: CP002 file not processed: cannot decode file: unknown encoding: latin-9x
src/mypkg/locked.py:1:0: CP002 file not processed: cannot write file: Permission denied
```

These lines are not counted as `CP002` findings in the summary; they are what
makes the exit code `2`.

## Encodings and line endings

Files are read as bytes and decoded the way Python itself decodes them: a
UTF-8 BOM or a [PEP 263](https://peps.python.org/pep-0263/) coding declaration
(`# -*- coding: latin-1 -*-`) is honoured, and anything else is UTF-8. There is
no newline translation. `--fix` writes a file back in its own encoding, with
its BOM if it had one, and keeps its line endings — CRLF stays CRLF — so every
line the fix did not change is byte-identical.

Precisely: every line the fix does not touch keeps its own line ending, and so
does a line it edits in place. A line the fix *inserts* takes libCST's default
newline, which is the file's first line ending. So a file that is uniformly
CRLF (or uniformly LF) stays that way; in a file with mixed endings, an
inserted line gets whatever the first line of the file ends with.

If a rewrite could not be written back that way, the file is declined with a
`CP003` instead of being written lossily. That covers:

- a file in one of the few legacy multi-byte encodings (such as `cp932`) that
  read two byte sequences as one character, where decoding and re-encoding
  would change a line the fix never touched;
- a rewrite that needs a character the declared encoding cannot hold —
  `from . import C` in a subpackage becomes `from .. import <subpackage>`,
  naming it after its directory, which may be non-ASCII in a file declared
  `latin-1`;
- a file libCST does not reproduce byte for byte, such as one whose last line
  ends in a lone `\r`.

`--fix` writes each file atomically: to a temporary file in the same directory,
fsynced, then renamed over the original with the original's permission bits
(and its owner and group, where the process is allowed to set them), after
which the directory is fsynced too. A crash or a full disk mid-write leaves
the original intact. A file you cannot write is refused and reported
(`cannot write file: Permission denied`), exactly as before, even though the
directory would allow the rename. A symlink is written through — the file it
points to changes, and the link stays a link.

Not preserved: extended attributes and ACLs, which the new file does not
inherit, and hard links — the rename gives the path a new inode, so any other
name for the old file keeps the old contents.

## Where output goes

This matters if you intend to pipe anything.

- **Plain check mode** (no `--fix`, no `--diff`): there is no patch, so
  warnings, findings and the summary all go to **stdout**, as usual. Notes
  (`cleanporter: note:`, such as the detected interpreter) go to **stderr**
  in every mode.
- **`--diff` or `--fix`**: **stdout carries only the patch.** Warnings, parse
  errors, findings, the `fixed: <path>` lines and the summary are all
  redirected to **stderr**. Diff headers are relative to the current working
  directory, so the stream is a valid patch that `git apply` accepts.
  Anything a third-party package prints while it is imported to classify an
  import goes to stderr too (see [How it works](how-it-works.md)).
- **`--format json`, `sarif` or `github`**: **stdout carries only the
  document**, written once the run is over. Findings and unprocessable files
  are in it; warnings, notes, unprocessable files (again), the `fixed: <path>`
  lines and the summary go to **stderr**, as they do under `--diff`. See
  [Machine-readable output](#machine-readable-output).

The patch is written as raw bytes, each file's lines in that file's own
encoding and line endings, so it applies to a CRLF, BOM-prefixed or Latin-1
file as it stands on disk. A file without a trailing newline gets the usual
`\ No newline at end of file` marker.

That is what makes this work:

```bash
cleanporter --diff src/ | git apply
```

All diffs for a run are concatenated into that one stream rather than written
as separate patch files.

## Machine-readable output

`--format` swaps the text report for a document a program can read. In every
format:

- **stdout carries the document and nothing else**, so it can be redirected
  to a file or piped to `jq` as it is. Everything a human wants to watch —
  warnings, notes (the detected interpreter, the `--fix` reminder to re-run
  your tests), files that could not be processed, `fixed: <path>` lines and
  the summary — still goes to stderr. JSON and SARIF also carry the run's
  warnings, notes and unprocessable files inside the document; the stderr
  copy is for the person reading the CI log. (The `--fix` reminder is not one
  of the run's notes: it is on stderr only.) That holds even for a
  third-party package that writes to stdout while the probe imports it, by
  `print` or straight to file descriptor 1 (`os.write`, unbuffered C output,
  a subprocess). The exception is C stdio output an extension module buffers
  — a `printf` from C while stdout is a pipe — which is flushed when the
  process exits, after the document; if a dependency prints from C on import,
  pass `--python <interpreter>` — one other than cleanporter's own — so the
  probe runs in a subprocess. See
  [Side effects](library.md#side-effects).
- **The exit code is exactly the text report's** — `0`, `1` or `2`, under the
  same rules, `--strict` included. A run that never starts — a malformed
  `[tool.cleanporter]` table, a usage error — exits `2` with its message on
  stderr and **no document** on stdout; check the exit code before parsing.
- **The findings are the ones the text report prints.** `CP004` is listed only
  under `--show-skipped`, though it is counted either way. A code `--select`
  or `--ignore` left out, or a finding a baseline accepted, is in no format
  and no count.
- **Severity is the effect on the exit code:** `CP001`, `CP003` and `CP005` are errors;
  `CP002` is a warning, or an error under `--strict`; `CP004` is a note.
- **Paths**: JSON spells them as the text report does (as found from the path
  you gave). SARIF and GitHub use them relative to the current directory, with
  `/` separators — run cleanporter from the repository root in CI. (SARIF
  falls back to an absolute `file:` URI for a file outside it.)
- **Columns**: JSON's `column` is 0-based, as in the text report's
  `PATH:LINE:COLUMN`. SARIF and GitHub count columns from 1, and get that
  number plus one. Columns count Unicode code points (SARIF's
  `"columnKind": "unicodeCodePoints"`).
- **Filenames that are not valid UTF-8** (possible on Linux) never break a
  report. JSON is ASCII-only and carries such a name as Python holds it, with
  each undecodable byte a lone surrogate escape (`bad\udcff.py` for the byte
  `0xff`) — valid JSON, though a strict UTF-8 consumer may refuse to decode
  it; `os.fsencode` turns it back into the bytes. SARIF percent-encodes the
  bytes (`bad%FF.py`); GitHub gets them backslash-escaped (`bad\xff.py`).

What happens to a patch depends on the format. JSON has a place for one, so
under `--diff` (or `--fix`) each rewrite is in its `patches` list and nothing
else is written to stdout. SARIF and GitHub annotations have none, so `--diff`
with them is a usage error (exit `2`) — also alongside `--fix`, which
otherwise overrides `--diff`, so the rule has no exception. `--fix` with them
writes the files, prints a `fixed: <path>` line to stderr for each, and
reports what is left — `git diff` shows what changed.

The formatters behind `--format` are not part of the
[library API](library.md): the formats are the interface.

### `--format json`

One JSON object, keys in this order:

| Key | Value |
| --- | --- |
| `tool` | `{"name": "cleanporter", "version": "…"}`. |
| `format_version` | `1`. Raised if a key is removed or changes meaning; new keys can appear without it. |
| `mode` | `"check"`, `"diff"` or `"fix"`. |
| `strict` | Whether `--strict` / `treat_unresolved_as_error` was in effect. |
| `exit_code` | The process's exit code. |
| `counts` | `files_checked`, `changed` (files rewritten, or diffed under `--diff`), `violations` (`CP001`), `not_rewritten` (`CP003`), `unresolved` (`CP002`), `skipped_by_config` (`CP004`), `unused_suppressions` (`CP005`) and `errors` (files not processed) — the summary line's numbers, after `--select`, `--ignore` and any baseline. With a baseline applied (`--baseline` or `baseline`), also `baselined` (findings it left out) and `stale_baseline` (its entries that matched no finding); both keys are absent without one. |
| `findings` | One object per finding (below), sorted by path, line, column and code. |
| `errors` | One object per file that could not be read, decoded, parsed or written: `code` (`CP002`), `path`, `line`, `column`, `message`. Not findings; any of them makes the exit code `2`. |
| `warnings`, `notes` | Lists of strings, without the `cleanporter: warning:` / `note:` prefix. |
| `patches` | Under `--diff` and `--fix`, one object per rewritten file: `path`, `written`, `write_error` (why a `--fix` write failed, else `null`), `diff_encoding` and `diff`. The diff is the same unified diff `--diff` prints: text when it is valid UTF-8 (`"diff_encoding": "utf-8"`), otherwise base64 of its bytes (`"base64"`) — it is in each file's own encoding, so a Latin-1 file's patch is not text. |

A finding:

| Key | Value |
| --- | --- |
| `code` | `CP001`–`CP005`. |
| `status` | `violation`, `unresolved`, `skipped`, `skipped-by-config` or `unused-suppression`. |
| `level` | `error`, `warning` or `note`, as above. |
| `path`, `line`, `column` | Where the `from` import starts — for `CP005`, where the comment starts; `column` is 0-based. |
| `parent`, `name` | The `from PARENT import NAME` it is about; empty strings for `CP005`, which is about a comment. |
| `message` | The text report's message, after the code. For `CP001` it suggests the conventional spelling (`helpers.Widget`) and, for a relative import, the replacement import spelled as `--fix` spells it (`from . import helpers`); `--fix` may write a different one — reusing an existing binding of the module, or a free alias when the name is taken — so read the patch, not the message, for what is written. |
| `detail` | The bare reason, for `CP002`–`CP005` (empty for `CP001`). |

```bash
$ cleanporter --format json src/ 2>/dev/null | jq '.findings[0]'
{
  "code": "CP001",
  "status": "violation",
  "level": "error",
  "path": "src/mypkg/consumer.py",
  "line": 3,
  "column": 0,
  "parent": "mypkg.helpers",
  "name": "Widget",
  "message": "imports object 'Widget' from module 'mypkg.helpers'; import the module and use 'helpers.Widget'",
  "detail": ""
}
```

### `--format sarif`

A [SARIF 2.1.0](https://docs.oasis-open.org/sarif/sarif/v2.1.0/sarif-v2.1.0.html)
log with one run. `tool.driver` names cleanporter and its version and carries
one rule per finding code, `CP001`–`CP005`, each with a short and full
description, `help` (text and Markdown) and a `helpUri` pointing at
[the finding codes table](#finding-codes) above. Each result has its
`ruleId`, `level`, message, and a location whose URI is relative to the
`SRCROOT` base (the current directory); `properties` holds `parent` and
`name`. A file that could not be processed is not a
result: it is an error-level `toolExecutionNotifications` entry, and the
invocation's `executionSuccessful` is `false`. Warnings and notes are
notifications too, and `exitCode` is the process's.

To show the findings in GitHub code scanning:

!!! warning "Run it from the repository root"

    Result URIs are relative to the directory cleanporter runs in, and code
    scanning resolves them against the repository root. Run the step from the
    root — no `working-directory:` — and pass paths relative to it, or the
    alerts will point at files that do not exist.

```yaml
- name: cleanporter  # from the repository root: SARIF paths are relative to it
  run: cleanporter --format sarif src/ tests/ > cleanporter.sarif
  continue-on-error: true  # let the upload run; code scanning gates the PR instead
- uses: github/codeql-action/upload-sarif@v3
  if: always()
  with:
    sarif_file: cleanporter.sarif
    category: cleanporter
```

Uploading needs the `security-events: write` permission on the job.

### `--format github`

One [workflow command](https://docs.github.com/actions/reference/workflow-commands-for-github-actions)
per finding, which GitHub Actions turns into an annotation on the line:

```text
::error file=src/mypkg/consumer.py,line=3,col=1,title=CP001::imports object 'Widget' from module 'mypkg.helpers'; import the module and use 'helpers.Widget'
::warning file=src/mypkg/gpu.py,line=5,col=1,title=CP002::could not determine whether 'cupy.ndarray' is a module: 'cupy' is not importable in the target interpreter
```

`error`, `warning` or `notice` follows the severity above (so a `CP005` is an `::error`); an unprocessable
file is an `::error` titled `CP002`. In the message `%`, CR and LF are escaped
as `%25`, `%0D` and `%0A`; in the `file=` and `title=` properties `:` and `,`
are escaped as well (`%3A`, `%2C`), as GitHub's parser requires. As with
SARIF, `file=` is relative to the current directory, so run the step from the
repository root.

GitHub caps how many annotations it shows: at the time of writing, 10 of each
level (`error`, `warning`, `notice`) per step and 50 per job. Past that, the
rest are dropped from the pull request view — though every line is still in
the step's log. On a large backlog, `--format sarif` with code scanning shows
everything.

```yaml
- name: Enforce Google style guide 2.2 (imports)
  run: cleanporter --format github src/ tests/
```

## Workflows

### As a CI gate

Check mode is the gate. Exit `1` on any `CP001`, so no extra scripting is
needed:

```yaml
- name: Enforce Google style guide 2.2 (imports)
  run: cleanporter src/ tests/
```

If you want unresolvable imports to fail the build too — useful once your
dependency set is stable and every import *should* be classifiable in CI —
add `--strict`:

```bash
cleanporter --strict src/ tests/
```

cleanporter finds the project's interpreter by itself — `$UV_PROJECT_ENVIRONMENT`
or the `.venv` beside `pyproject.toml` (the uv workspace root's, for a member),
or else an activated virtual environment — so a copy installed with `pipx` or `uv tool` still classifies
your third-party imports. The project root is the directory of the
`pyproject.toml` found above the first path; with none, it is the first
path's own directory, so `cleanporter src/` looks for `src/.venv`. If your
dependencies live somewhere detection does not look — that layout, a conda
environment, anything else — point the classifier at the interpreter that
actually has them installed, with `--python` or `python =` in
`[tool.cleanporter]`:

```bash
cleanporter --python /opt/envs/proj/bin/python src/
```

### Adopting cleanporter on an existing codebase

A codebase that has never followed the rule can have hundreds of findings,
far more than one change should fix. Record them in a baseline, and the gate
checks only what is *new* from the first day:

```bash
cleanporter --write-baseline cleanporter-baseline.json .   # exit 0; commit the file
```

```toml
[tool.cleanporter]
baseline = "cleanporter-baseline.json"   # relative to this pyproject.toml
```

From then on `cleanporter .` leaves every recorded finding out of the report
and the exit code, and fails on any other one. Pay the backlog down at your
own pace — `cleanporter --fix` still rewrites every violation it can, recorded
or not, so a sweep of one package at a time works as it always has — and
rewrite the file when you do, so it shrinks with the backlog.

Both flags need a `pyproject.toml`: a baseline's paths are relative to the
project root, and without one there is no root that stays the same from
`cleanporter .` to `cleanporter src/`. With none above the first path, they
exit `2`.

**What a recorded finding is.** Its path relative to the project root (with
`/` separators, so one file serves every platform), its code, and the
`parent` and `name` it imports — which already identify the import, whatever
its spelling. There is no line number and nothing of the statement's text.
So a finding stays recorded when:

- the lines above it move;
- its statement is reformatted: whitespace, parentheses, one line exploded
  into several or joined back, a backslash continuation;
- its statement's names are reordered, or another name is added to it (only
  the new name is reported);
- a relative import is spelled absolutely, or the other way round;
- its alias changes (`as h`, or none), or it moves to another scope in the
  same file — from module level into a function, say.

It comes back when the import changes what it imports — another name or
another module — or moves to another file.

`CP001` and `CP003` match each other. Whether the fixer would decline an
import can depend on which files are in the run — a re-export is
load-bearing only when the run includes a file that imports it — so a
baseline written from `.` still holds for a run over `src/`, or a
`--whole-project` [pre-commit hook](pre-commit.md) handed only the changed
files. The code in the file is the one reported when it was written. `CP002`
does not match `CP001`: once the environment can classify an unresolved
import, the violation is reported, and `--write-baseline` says how many
`CP002` it recorded so you know to rewrite the file after fixing the
environment.

Recorded findings are counted, not just listed: two imports of the same name
from the same module in one file are two entries, and a third is reported.
With no line in the key, which of the three is reported is not tracked — it
may be one of the old imports rather than the one just added.

**Stale entries.** A recorded finding that no longer occurs — fixed, changed
or deleted — is *stale*. Fixing something must never fail a run, so the run
passes and says how many entries are stale in a note on stderr
(`cleanporter: note: 3 baseline entries match no finding any more …`);
re-run `--write-baseline` to drop them. Entries for files the run did not
check, or for codes it does not report, are not counted as stale. With
`--format json` the `counts` object carries `baselined` and `stale_baseline`.

**Check runs only.** A baseline is written and applied by *check* runs. Under
`--fix` and `--diff` the same import can be reported differently — a name
nothing reads is a `CP001` in a check and a `CP003` once the fixer has looked
— and the fixer adds file-level `CP003` findings that name no import. So
`--baseline` with `--fix` or `--diff` is a usage error, and a configured
`baseline` is not applied under them: every finding is reported, with a
one-line note on stderr saying so. The `cleanporter-fix` pre-commit hook is
such a run; the `cleanporter` hook applies the baseline.

**The file** is JSON — `{"version": 1, "findings": [...]}`, each entry with
`path`, `code`, `parent` and `name` — sorted and indented, so it diffs well in
review, and written atomically. `CP004` is never recorded (it never fails a
run), a run under `--select` or `--ignore` records only what it reports, and
a run in which a file could not be read or parsed writes nothing and exits
`2`. `--write-baseline` prints no report, so it cannot be combined with
`--format`.

To silence a *kind* of finding rather than a list of them, use `--select` /
`--ignore` (or `select` / `ignore` in the configuration): `--ignore CP002`,
say, while the environment is not yet complete enough to classify every
import.

### As a pre-commit hook

The repository publishes `cleanporter` and `cleanporter-fix` hooks for
[pre-commit](https://pre-commit.com/) and [prek](https://prek.j178.dev/); they
run with `--whole-project`, so a changed file is judged on the whole tree. See
[pre-commit](pre-commit.md).

### Reviewing before applying

`--diff` never writes. Read the patch, then apply it in one step if you like
it:

```bash
cleanporter --diff src/            # read it
cleanporter --diff src/ | git apply
```

Because findings go to stderr in this mode, add `2>/dev/null` if you want the
patch alone on your terminal, or `2>&1 >/dev/null` if you want only the
findings.

### Doing a `--fix` sweep

```bash
git switch -c chore/import-style   # a dedicated branch: the diff can be large
cleanporter --fix src/ tests/      # rewrite what is provably safe
git diff                           # review
uv run pytest                      # re-run the suite -- see the warning below
```

`--fix` prints the diff for every changed file to stdout and a
`fixed: <path>` line to stderr, then a summary:

```text
checked 41 file(s), fixed 6: 3 violation(s), 2 not rewritten, 1 unresolved, 0 skipped by config
```

Anything still reported after the sweep is a `CP001` the fixer never planned
(a semicolon-joined or one-line import), a `CP003` it deliberately declined,
or a `CP002` it could not classify. All three need a human. A `CP004` does
not — that one is your own `skip` rule or inline suppression, and it is only
printed if you ask for it with `--show-skipped`. A `CP005` is a suppression
the sweep made stale: the finding it silenced is gone, so delete the comment.

!!! warning "Re-run your tests"

    cleanporter proves safety by analysing the file it is rewriting, plus
    what the rest of the run says about it. A string in another analysed file
    that spells the rewritten binding's dotted path out whole —
    `monkeypatch.setattr("pkg.cli.helper", ...)`, `mock.patch`, an entry point
    in `pyproject.toml` — keeps that import as it is (a `CP003` naming the
    string). What the run cannot see can still go stale even though the
    rewritten file itself is correct: a dynamic string, a `getattr(module,
    "helper")`, a reference in a file outside the run, or one in a config
    file other than `pyproject.toml` (`setup.cfg`, `tox.ini`, YAML). Whenever
    it writes a file, `--fix` prints a note to stderr saying exactly this.

### Import layout

cleanporter does not re-sort or reflow imports; it inserts or replaces a
statement in place. Run your formatter afterwards if the new import lands
somewhere you would rather it did not:

```bash
cleanporter --fix src/ && ruff check --select I --fix src/
```

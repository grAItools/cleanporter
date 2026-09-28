# Usage

cleanporter is one command. By default it *checks*; passing `--fix` makes it
*rewrite*.

```text
cleanporter [--fix] [--diff] [--python PATH] [--exempt MODULE] [--root PATH]
            [--strict] [--version] [paths ...]
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
| `--python PATH` | Interpreter used to classify stdlib and third-party names. Default: the interpreter running cleanporter. |
| `--exempt MODULE` | An additional module whose members may be imported by name. Repeatable. Adds to, never replaces, the [default exemptions](configuration.md#default-exemptions) and anything in `exempt_modules`. |
| `--root PATH` | An additional first-party import root — a directory that is on `sys.path` for the code being analysed. Repeatable. Adds to whatever the analysed paths themselves imply, and to `source_roots`. A relative value is resolved against the directory holding your `pyproject.toml`, not against the current directory. |
| `--strict` | Also fail (exit `1`) on imports that could not be classified (`CP002`). Equivalent to turning on `treat_unresolved_as_error` for this run. |
| `--show-skipped` | List the imports a [`skip` rule](configuration.md#skip-rules) took out of the run (`CP004`). They are counted in the summary either way; this prints them, which is how you check what a pattern actually swallowed. |
| `--version` | Print the version and exit. |
| `--help` | Print usage and exit. |

Every flag except `--python` and `--version` is additive with the
configuration file rather than overriding it — see
[Configuration](configuration.md#how-cli-flags-layer-on-top-of-config).

## Finding codes

Each reported line has the shape
`PATH:LINE:COLUMN: CODE message`.

| Code | Status | Meaning |
| --- | --- | --- |
| `CP001` | `VIOLATION` | An object is imported by name. This is the rule being enforced, and it is what blocks CI. |
| `CP002` | `UNRESOLVED` | cleanporter could not determine whether the symbol is a module: a third-party parent it cannot import, a name that is both a submodule and a binding in its package's `__init__`, or a first-party name that is neither on disk nor bound in its parent to something cleanporter can follow (a generated `_version.py`, a `_pb2`, an out-of-tree extension, another portion of a namespace package). The message says which evidence was missing. Never rewritten. Only counts toward the failure exit code under `--strict` / `treat_unresolved_as_error`. The same code also marks a whole file that was not processed (`file not processed: …`, when it could not be read, decoded, parsed or written); those lines are not findings and always make the exit code `2`, with or without `--strict` — see [Exit codes](#exit-codes). |
| `CP003` | `SKIPPED` | Structurally a violation, deliberately not rewritten. This is the "declined, because…" note that explains why `--fix` or `--diff` left a file alone. |
| `CP004` | `SKIPPED_BY_CONFIG` | Matched a [`skip` rule](configuration.md#skip-rules), so it was never analysed. Counted in the summary, printed only under `--show-skipped`, and **never** part of the exit code — you asked for it. |

Examples of each:

```text
src/mypkg/consumer.py:3:0: CP001 imports object 'Widget' from module 'mypkg.helpers'; import the module and use 'helpers.Widget'
src/mypkg/gpu.py:5:0: CP002 could not determine whether 'cupy.ndarray' is a module: 'cupy' is not importable in the target interpreter
src/mypkg/api.py:11:0: CP003 file not rewritten: local 'Widget' is rebound in the same scope
src/mypkg/stencils.py:4:0: CP004 'broadcast' from 'gt4py.next' skipped by configuration: skip rule #1 (decorator='field_operator'): DSL bodies are re-parsed by the frontend
```

`CP002` findings are only produced for imports cleanporter actually looked at:
exempt modules and (under `scope = "first-party"`) third-party modules are
skipped before resolution is attempted.

!!! note "`CP003` findings count toward the failure exit code"

    A file the fixer declined still contains a violation, so `CP003` is not
    purely informational: like `CP001`, it makes the run exit `1`.

    Most `CP003` findings are the fixer explaining a decision, so they only
    appear under `--fix` or `--diff`. The exceptions are reasons that belong
    to the import itself, which are reported as `CP003` in every mode: a
    wildcard import (`from x import *`), for which no module import is a
    replacement; an explicit or load-bearing re-export; a replacement that
    cannot be shown to bind the module it names; and `from . import C` where
    the package is top-level, which has no relative replacement (see
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

## Exit codes

| Code | Meaning |
|-----:| --- |
| `0` | Clean — nothing remains to report. |
| `1` | Violations found (or left behind after `--fix`). |
| `2` | Operational error: a file that could not be read, decoded, parsed or (under `--fix`) written, or a malformed `[tool.cleanporter]` table. |

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
  warnings, findings and the summary all go to **stdout**, as usual.
- **`--diff` or `--fix`**: **stdout carries only the patch.** Warnings, parse
  errors, findings, the `fixed: <path>` lines and the summary are all
  redirected to **stderr**. Diff headers are relative to the current working
  directory, so the stream is a valid patch that `git apply` accepts.
  Anything a third-party package prints while it is imported to classify an
  import goes to stderr too (see [How it works](how-it-works.md)).

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

If CI runs in a different environment from the one your code targets, point
the classifier at the interpreter that actually has your dependencies
installed:

```bash
cleanporter --python .venv/bin/python src/
```

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
not — that one is your own `skip` rule, and it is only printed if you ask for
it with `--show-skipped`.

!!! warning "Guards are per file — re-run your tests"

    cleanporter proves safety by analysing the file it is rewriting. A string
    in a *different* file that names the rewritten binding by its dotted path
    — `monkeypatch.setattr("pkg.cli.helper", ...)`, an entry point in
    `pyproject.toml`, an `importlib` lookup — is invisible to that analysis,
    so `--fix` can make such a reference stale even though the rewritten file
    itself is correct. Whenever it writes a file, `--fix` prints a note to
    stderr saying exactly this.

### Import layout

cleanporter does not re-sort or reflow imports; it inserts or replaces a
statement in place. Run your formatter afterwards if the new import lands
somewhere you would rather it did not:

```bash
cleanporter --fix src/ && ruff check --select I --fix src/
```

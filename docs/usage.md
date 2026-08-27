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
| `CP002` | `UNRESOLVED` | cleanporter could not determine whether the symbol is a module. Never rewritten. Only counts toward the failure exit code under `--strict` / `treat_unresolved_as_error`. |
| `CP003` | `SKIPPED` | Structurally a violation, deliberately not rewritten. This is the "declined, because…" note that explains why `--fix` or `--diff` left a file alone. |

Examples of each:

```text
src/mypkg/consumer.py:3:0: CP001 imports object 'Widget' from module 'mypkg.helpers'; import the module and use 'helpers.Widget'
src/mypkg/gpu.py:5:0: CP002 could not determine whether 'cupy.ndarray' is a module: 'cupy' is not importable in the target interpreter
src/mypkg/api.py:11:0: CP003 file not rewritten: local 'Widget' is rebound in the same scope
```

`CP002` findings are only produced for imports cleanporter actually looked at:
exempt modules and (under `scope = "first-party"`) third-party modules are
skipped before resolution is attempted.

!!! note "`CP003` findings count toward the failure exit code"

    A file the fixer declined still contains a violation, so `CP003` is not
    purely informational: like `CP001`, it makes the run exit `1`.

    Most `CP003` findings are the fixer explaining a decision, so they only
    appear under `--fix` or `--diff`. The one exception is a wildcard import
    (`from x import *`), which is reported as `CP003` in every mode — there is
    no module import that reproduces it, so it can never be rewritten.

## Exit codes

| Code | Meaning |
|-----:| --- |
| `0` | Clean — nothing remains to report. |
| `1` | Violations found (or left behind after `--fix`). |
| `2` | Operational error: a file that could not be parsed or decoded, or a malformed `[tool.cleanporter]` table. |

In short: 0 = clean, 1 = violations, 2 = operational error.

Exit `2` takes precedence: if any input file failed to parse, the run reports
`2` regardless of what else it found. A path on the command line that does not
exist is a *warning*, not an error — it is reported and skipped.

## Where output goes

This matters if you intend to pipe anything.

- **Plain check mode** (no `--fix`, no `--diff`): there is no patch, so
  warnings, findings and the summary all go to **stdout**, as usual.
- **`--diff` or `--fix`**: **stdout carries only the patch.** Warnings, parse
  errors, findings, the `fixed: <path>` lines and the summary are all
  redirected to **stderr**. Diff headers are relative to the current working
  directory, so the stream is a valid patch that `git apply` accepts.

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
checked 41 file(s), fixed 6: 3 violation(s), 2 not rewritten, 1 unresolved
```

Anything still reported after the sweep is a `CP001` the fixer never planned
(a semicolon-joined or one-line import), a `CP003` it deliberately declined,
or a `CP002` it could not classify. All three need a human.

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

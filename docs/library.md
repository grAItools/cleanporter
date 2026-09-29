# Library API

Everything the `cleanporter` command does is available as a library call, for
an editor integration, a pre-commit wrapper or a test that wants a real run
without shelling out. The command line is a thin shell over it: it loads the
configuration, calls `cleanporter.run`, prints what comes back and picks the
exit code.

!!! note "Pre-1.0"

    While cleanporter is on `0.x`, the library API can change in a minor
    release, exactly like the command line — see the
    [changelog](https://github.com/grAItools/cleanporter/blob/main/CHANGELOG.md).

## A whole run: `run`

```python
import pathlib

import cleanporter
from cleanporter import config, engine

cfg = config.load_config(pathlib.Path("src"))  # reads [tool.cleanporter]
result = cleanporter.run([pathlib.Path("src")], cfg, cleanporter.Mode.DIFF)

for finding in result.findings:
    print(finding.format())
for patch in result.patches:
    print(patch.path, len(patch.diff), "bytes of patch")
raise SystemExit(result.exit_code(strict=cfg.treat_unresolved_as_error))
```

Each finding is a `cleanporter.model.Finding`: `format()` is the text
report's line, and `message` the part after the code. The command's
`--format json|sarif|github` renderers are deliberately private — the
[formats](usage.md#machine-readable-output) are the interface, not the
functions — so a program that wants them runs the command, or builds its own
report from a `RunResult`.

`run`, `Mode`, `RunResult`, `build` and `Project` are exported from
`cleanporter` itself. `FilePatch` and `Listener` are not: import them from
`cleanporter.engine` (`from cleanporter import engine`, then
`engine.FilePatch`, `engine.Listener`).

`run(paths, config, mode=Mode.CHECK, *, listener=None, whole_project=False)`
takes the paths to process, a `Config` used exactly as given (`load_config`
reads `[tool.cleanporter]`; apply any overrides of your own with
`dataclasses.replace`), and a `Mode`. With `whole_project=True` it is
`--whole-project`: the tree under `Config.root` is read for evidence, and
only the files under `paths` are fixed, reported and counted — `findings`,
`patches`, `errors` and `files_checked` cover those alone, and a file
elsewhere that cannot be parsed is a warning. `run` itself enforces that
every path, and every file a listed directory expands to, lies inside
`Config.root`, both as written and resolved: one that
does not is an `errors` entry, so `exit_code()` is `2`, and the whole run is
refused — nothing is analysed or written, and `files_checked` is `0`. It does
not check that `Config.root` came from a `pyproject.toml`, or that the paths
share one (the command does both); a `Config` built by hand has the cwd as its
root.

| `Mode` | What happens |
| --- | --- |
| `Mode.CHECK` | Report only, like `cleanporter`. |
| `Mode.DIFF` | Also compute every rewrite as a patch, and write nothing, like `cleanporter --diff`. |
| `Mode.FIX` | Compute every rewrite, write it to disk atomically, re-parse and report what is now there, like `cleanporter --fix`. |

It returns a `RunResult`:

| Field | Meaning |
| --- | --- |
| `mode` | The `Mode` the run was made in. |
| `files_checked` | How many files were read and parsed. |
| `findings` | Every finding (`CP001`–`CP005`), sorted by path, line, column and code — the order the command prints them in. |
| `patches` | One `FilePatch` per rewritten file: its `path`, the unified `diff` as bytes (in the file's own encoding and line endings, ready for `git apply`), the `before` and `after` contents, whether it was `written`, and — under `Mode.FIX`, when the write failed — the `write_error` finding. A failed write keeps its patch, which still applies to the untouched file. |
| `errors` | A finding per file that could not be read, decoded, parsed or written: the files that failed to load, then the failed writes. The command exits `2` when there are any. |
| `write_errors` | Just the failed writes among `errors` (the `write_error` of each such patch), so they can be told apart from files that never loaded. |
| `warnings` | Every warning, in the order it arose. |
| `notes` | Every note, in the order it arose — today, which interpreter was detected for the probe and why (see [Side effects](#side-effects)). Informational: never counted by `exit_code`. |
| `violations`, `skipped`, `unresolved`, `skipped_by_config`, `unused_suppressions` | Counts of `CP001`, `CP003`, `CP002`, `CP004` and `CP005` in `findings`. |
| `changed`, `wrote` | How many files were rewritten (written under `Mode.FIX`, diffed under `Mode.DIFF`; a failed write does not count), and whether anything was written to disk. |

`exit_code(strict=False)` is the command's exit code for the result, and the
command uses exactly this: `2` when `errors` is non-empty, else `1` when
`violations + skipped + unused_suppressions` (plus `unresolved` when `strict`, as under `--strict`
or `treat_unresolved_as_error`) is non-zero, else `0`. `CP004` never counts.

A literal dotted path in another analysed file or in `pyproject.toml` keeps
the import it names, but `--fix` cannot see every reference from elsewhere — a
dynamic string, a lookup through a module object, a file outside the run,
config other than `pyproject.toml` (see
[Known limitations](safety.md#known-limitations)); after a `Mode.FIX` run
that `wrote`, re-run the target's tests, exactly as after the command.

### Streaming: `Listener`

A run over a large tree takes a while, and the command prints each patch the
moment it exists. Pass a `Listener` subclass as `listener` to do the same: its
`warning(message)`, `note(message)`, `error(finding)` and `patch(patch)` methods are called as
each happens, in the order the command prints them; each does nothing unless
overridden. Every one of them also ends up in the `RunResult`, so a caller that
only wants the result passes nothing. A write that fails is reported to
`error` only — the command prints no patch for a file it did not change — and
its patch is in `patches` with its `write_error`.

```python
class Progress(engine.Listener):
    def patch(self, patch):
        print("rewrote", patch.path)
```

## The run's inputs: `build` and `Project`

`build(paths, config)` takes the paths through every stage a run needs before
it looks at any one import, and returns a frozen `Project`:

1. discover the Python files and build the first-party module map;
2. read, decode and parse every file (one that cannot be is an `errors` entry);
3. settle the module map's import roots and anchor each file's relative imports;
4. collect what every file *uses*, across files;
5. build the `Resolver` with that evidence and with the interpreter chosen
   from `Config.python` (detected, unless one is named), and classify every
   import the run will ask about in one batch.

A `Project` holds the `config`, the parsed `records`, that `resolver`, and the
`errors`, `warnings` and `notes` from building it. `analyze_record(record, resolver,
config)` and `fix_record(record, resolver, config)` check or fix one of its
records; `run` is those two, file by file, plus writing, re-analysing and
counting.

A `Resolver` built directly —
`Resolver(module_map, evidence=resolver.NO_EVIDENCE)`, where the evidence
argument is required so the choice is explicit — works, but knows nothing
about what other files use or name by dotted path: it never declines a rewrite
for removing a re-export another file needs, because it has no evidence that one
does. `build` is how to get a resolver that has that evidence, since it
collects it before constructing the resolver.

## Side effects

A run can import the code it analyses. Stdlib and third-party names are
classified by an interpreter probe, which imports each *parent* package
(never the imported name itself, and never first-party code — see
[How it works](how-it-works.md)):

- **Which interpreter.** `Config.python` is `None` by default, which — like
  `"auto"` — *detects* the project's interpreter, exactly as the command does:
  `$UV_PROJECT_ENVIRONMENT` or `.venv` at `Config.root` (at the uv workspace
  root instead, for a workspace member), else `$VIRTUAL_ENV`, the first that is an executable interpreter (see
  [Configuration](configuration.md#the-probe-interpreter)). One that is not the
  calling process's own environment is probed in a subprocess, and the
  result's `notes` say which it was and why. `"self"` is the calling
  interpreter, in process; any other string names an interpreter.
  Up to 0.4, `None` meant the calling interpreter: pass `python="self"` to
  keep that.
- **In process when it is the caller's own.** With `python="self"`, when
  detection finds nothing, or when what it finds (or what is named) is the
  running interpreter, the probe runs in the calling process. The target's
  packages are imported into it, run their import-time code there, and stay in
  `sys.modules` after `run` returns.
- **stdout is redirected, process-wide, down to the file descriptor.** While
  the probe imports, `sys.stdout` is pointed at `sys.stderr`
  (`contextlib.redirect_stdout`) *and* file descriptor 1 at descriptor 2
  (`os.dup2`, restored afterwards however the probe exits), so a package that
  prints on import — or writes to descriptor 1 directly: `os.write(1, …)`,
  unbuffered C output, a subprocess — cannot land in a patch or a `--format`
  document. Both are global to the process, so this is **not thread-safe**:
  anything another thread writes to stdout meanwhile, through Python or the
  descriptor, goes to stderr. `sys.stdout` is flushed before and after. With
  no `sys.stdout` or `sys.stderr`, or a descriptor that cannot be duplicated
  (closed, say), only the Python-level redirection applies.
- **Not C stdio's buffer.** An extension that `printf`s on import while
  stdout is a pipe or file keeps that output in the C library's buffer, which
  is flushed at process exit — after the descriptor is restored, so onto
  stdout, after whatever the caller wrote there. When a dependency prints
  from C on import, probe out of process: set `python` to an interpreter
  other than the calling one.
- **To isolate a run**, set `python` in the `Config` to a *different*
  interpreter (for example
  `dataclasses.replace(cfg, python="/path/to/venv/bin/python")`, the library
  equivalent of `--python`), or let detection find the project's, when that
  is not the calling environment. The probe then runs in a subprocess, imports nothing into the caller and redirects
  nothing. Naming the interpreter that is already running keeps the probe
  in-process.

Under `Mode.FIX` a run also writes files, each atomically.

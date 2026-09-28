# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

> **Pre-1.0 stability notice.** cleanporter is on `0.x`. The command-line surface
> and the `[tool.cleanporter]` configuration schema may change between minor
> versions. While on `0.x`, **a minor bump is the breaking-change signal** —
> `0.2.0` → `0.3.0` may require you to adjust flags, config keys or expectations,
> whereas patch releases (`0.3.0` → `0.3.1`) will not. Pin the minor version if
> you depend on the current surface.

## [Unreleased]

Breaking under the pre-1.0 policy above:

- With no `--python` and no `python` key, the probe now runs under the
  project's own interpreter when one is detected, not cleanporter's; for the
  library, `Config.python = None` (the default) now means "detect" rather than
  "the calling interpreter". `python = "self"` / `--python self` /
  `Config(python="self")` restores the old behaviour. `RunResult` and
  `Project` gain a trailing `notes` field, and `engine.Listener` a `note`
  method.

### Added

- **cleanporter finds the project's interpreter by itself.** Installed with
  `pipx` or `uv tool`, cleanporter runs in an environment without the target
  project's dependencies, so every third-party import used to come back
  `CP002` unless you knew about `--python`. Now, when no interpreter is named,
  the probe uses the first executable interpreter of `$VIRTUAL_ENV`, then
  `$UV_PROJECT_ENVIRONMENT` (relative to the project root), then the `.venv`
  beside the `pyproject.toml` in use (or the first path's directory when there
  is none); a candidate that is missing or not executable is passed over
  silently, and nothing else — no `uv` subprocess, no `PATH` search — is
  consulted. The pick is probed out of process, as if passed with `--python`,
  unless it is cleanporter's own `sys.executable`, and a one-line
  `cleanporter: note:` on stderr names it and says why (in `RunResult.notes`,
  and `Listener.note`, for library callers). Detection only chooses which
  environment answers: one lacking a package, or unable to run, leaves the
  import `CP002`, never an optimistic verdict. `python`/`--python` accept
  `auto` (detect; the default) and `self` (cleanporter's own, in process)
  besides a path or command, so `--python auto` restores detection over a
  configured interpreter. Documented under
  [The probe interpreter](https://graitools.github.io/cleanporter/configuration/#the-probe-interpreter).

## [0.4.0] - 2026-09-28

Breaking under the pre-1.0 policy above:

- `cleanporter.build` returns a `Project` rather than a 4-tuple; `analyze.build`
  and `Resolver.note_uses` are removed, and `Resolver` takes a required
  keyword-only `evidence=`.
- `python = ""` and `--python ""` are errors (exit `2`), as is a drive-relative
  `python` such as `C:python.exe`; a relative `python` path is read against the
  `pyproject.toml` directory rather than the current directory.
- Some first-party `CP001` findings are now `CP002` (never rewritten, counted
  only under `--strict`), so a run may change exit code in either direction.
- `from . import X` in a top-level package is now `CP003` in every mode and kept
  as written; `--fix` writes relative replacements for relative imports; under
  `scope = "first-party"`, `--fix`/`--diff` no longer rewrite stdlib and
  third-party imports.
- A file that could not be processed now reads `CP002 file not processed:
  <reason>`, not `could not determine whether '?.?' …`.

### Added

- **A library API for a whole run: `cleanporter.run(paths, config, mode)`.**
  It does everything the command does except print and pick an exit code —
  builds the project, diffs or fixes per `Mode` (`CHECK`, `DIFF`, `FIX`), writes
  under `FIX`, re-parses and re-analyses — and returns a `RunResult`: the sorted
  findings, a `FilePatch` per rewritten file (the patch bytes, before/after
  contents, whether it was written — and, when a write failed, the error, so
  the rewrite is not lost), file-level errors with the failed writes also in
  `write_errors`, warnings, counts, and `exit_code(strict=...)`, the command's
  own 0/1/2 rule. An optional `engine.Listener` hears about each warning, error
  and patch as it happens, which is how the command still streams patches. The
  page also documents the side effects of the in-process probe (imports left
  in `sys.modules`, a process-wide stdout redirect) and how `python` avoids
  them. Documented on the
  new [Library API](https://graitools.github.io/cleanporter/library/) page;
  `Mode`, `Project`, `RunResult` and `run` join the names `cleanporter`
  exports.

- **A weekly gt4py check** (`corpus/gt4py_check.py`, `.github/workflows/gt4py.yml`,
  Mondays at 02:23 UTC and on dispatch). It checks out gt4py's `main`, writes
  the `[tool.cleanporter.skip]` rules a gt4py user is expected to write, runs
  `--fix` over the checkout and re-runs gt4py's own unit tests, failing only on
  a test that passed before and fails after — or on a test that stopped being
  collected, which a comparison of failures alone would read as an improvement.

  The corpus is pinned so that a red run means *this* repository changed; this
  asks the opposite question, against code that moves. gt4py earns it because
  every unsafe-rewrite class in issue #2 came from there, and because those bugs
  produce code that imports and parses — the fixer's own re-parse backstop
  passes them, and only running the suite says otherwise.

- **`corpus/run.py --update`**, which repins `packages.txt` to the latest
  release of every package it names and stops there. Resolution is against the
  floor of `requires-python` rather than the interpreter you happen to run it
  on, so a bump cannot pick a version of a named package that the oldest
  supported Python cannot install — their dependencies are settled by the
  install that follows, which is what running the check afterwards is for.
  Comments, environment markers and file order are preserved, so a bump reads
  as a column of version numbers changing, and a line that is not a simple
  `==` pin is reported rather than quietly skipped. It does not go on to run
  the check: a bump belongs in its own commit, or a changed corpus report has
  two possible causes.

- **`[tool.cleanporter.skip]`: regions you declare off-limits.** A list of rule
  tables matching on `file`, `function`, `method`, `class`, `symbol`,
  `decorator` (AND within a table, OR across the list), each with an optional
  `reason` echoed in the report. Anything a rule covers is neither reported nor
  rewritten, and — the part that matters — any binding whose name appears
  inside a skipped region is *pinned*, so a module-level import stays intact
  when its only use is inside one.

  This exists because some bindings are load-bearing for a consumer no analysis
  of the file can see. A body under `@gtx.field_operator` is re-parsed by
  GT4Py's own frontend, which rejects a module-qualified call outright; a
  `conftest.py` namespace *is* pytest's fixture registry. In both cases the
  resolver is right, the rewrite is legal Python, and the result does not run.

  A rule never changes *how* a name is rewritten and never causes a name to be
  rewritten that would not have been; every line inside a skipped region is
  byte-identical in the output. Nothing in a rule reaches the resolver, and a
  skipped name joins the same *keep* list an explicit re-export is already on,
  so all-or-nothing per file is unchanged. Note that keeping a name also takes
  its whole-file blocker with it, so a file previously declined outright may
  now have its *other* imports fixed — more of the file changes, not less.
  Unlike `exclude`, a skipped file is still read and still contributes
  re-export evidence.

  A rule can also match code the fixer is about to *write*
  (`{ decorator = 'gtx\.field_operator' }` against a file that spells it
  bare). cleanporter recomputes the regions on its own output and declines the
  file rather than apply a rewrite its own configuration would forbid.

- **`CP004` and `--show-skipped`.** A skipped import is reported as `CP004`,
  counted in the summary line, printed only under `--show-skipped`, and never
  part of the exit code — it is your own configuration reporting back, not a
  problem found in your code.

- **[pyrefly](https://pyrefly.org/) as a fourth type checker**, and a
  **blocking** one from the start. It runs as a git hook like the rest, reads
  `[tool.pyrefly]` in `pyproject.toml`, and covers `src/cleanporter` *and*
  `tests/` (excluding `tests/fixtures/`).

- **A corpus check** (`corpus/run.py`, `corpus/packages.txt`, and a daily
  `Corpus` workflow). It runs `--fix` over a pinned set of real third-party
  packages, then *imports and executes* the result, comparing every failure
  against the same probe run on the pristine copy. The fixes below credited to
  the corpus check were found by it and none by `tests/` — they do not fail a
  parse, so the fixer's own re-parse backstop passed them. See
  `corpus/README.md`.

### Changed

- **`cleanporter.build` returns a `Project`, not a 4-tuple, and the resolver
  gets its cross-file evidence at construction.** `build` (now in the new
  `project` module) takes a run's paths through fixed stages — files, parsed
  records, the settled module map, what every file uses, and then a resolver
  built *with* that evidence and warmed — and returns a frozen `Project` with
  `config`, `records`, `resolver`, `errors` and `warnings`. `Resolver` takes a
  **required** keyword-only `evidence` (a `resolver.Evidence`), so what
  `is_load_bearing` answers no longer depends on whether someone remembered to
  call a setter; a resolver built outside a run passes
  `resolver.NO_EVIDENCE` explicitly, and then never calls a re-export
  load-bearing, as before. `cli.run` is now a thin shell over `engine.run`;
  its output, streams and exit codes are byte-for-byte unchanged.

- **`--fix` writes a relative import's replacement relative.** `from .sub.mod
  import C` used to become `from pkg.sub import mod`; it now becomes `from .sub
  import mod`, and `from . import C` in `pkg/sub/mod.py` becomes `from .. import
  sub`. Absolute imports are rewritten as before, and `CP003` messages about a
  replacement quote it as it would be written. Two consequences. `from .
  import C` where the package is top-level — `from . import __version__` in
  `pkg/cli.py` is the common one — has no relative replacement, and the
  absolute one (`import pkg`) would depend on the import root where the
  original did not, so that import is now a `CP003`, in every mode, and kept
  as written; the rest of its file is still fixed. It used to be rewritten.
  And a relative and an absolute import of the same module in one file no
  longer share a module binding: each gets its own, one of them aliased
  (`mod_2`), because the two are the same module only if the inferred import
  root is right. Two relative imports of one module still share one.

- **Paths from different projects in one run now draw a warning.** A run loads
  the configuration found from its *first* path, and a later path whose nearest
  `pyproject.toml` is a different file — another project, or a nested one — was
  analysed under it without a word. cleanporter now warns, naming the
  configuration in use and each path whose own it is ignoring. Which
  configuration wins is unchanged; run once per project to apply each one's.

- **`python = ""` and `--python ""` are errors** (exit `2`). Both used to be
  accepted and silently ignored; omit the key or the flag to use the current
  interpreter.

- **`python` in `[tool.cleanporter]` expands a leading `~` or `~user`**, as the
  shell already did for `--python`; an unknown user is an error. Environment
  variables are still not expanded. A Windows drive-relative value such as
  `C:python.exe` — relative to that drive's current directory, which no project
  root can stand in for — is now an error, and a drive-qualified absolute path
  (`C:\venv\Scripts\python.exe`, `\\server\share\python.exe`) is used as
  written on every platform.

- **A check is about 3.5–4× faster.** Over libcst's own source (297 files) a
  plain check took 110–137 s and now takes 33–37 s, with byte-identical
  output; `--diff` over the same tree went from 329–360 s to 262–283 s, the
  rest being the scope analysis the fixer's guards need. The analysis used to
  walk every file's libcst tree seven times — once per question it asked of
  it, and one of them twice — and resolve libcst's position metadata, another
  full pass, to learn where a handful of imports start. It now walks each
  tree once, collecting every fact it needs in that pass, and reads import
  positions off a parse with Python's own `ast`. A file that parse cannot
  vouch for still gets libcst's metadata, so lines and columns are exactly the
  same. That covers `\r` line endings, a grammar the running Python rejects,
  and code libcst does not reproduce exactly, such as a form feed (`\f`) or a
  backslash continuation in a statement's leading whitespace. Parsing is now
  most of what remains.

- **Under `scope = "first-party"`, other imports are no longer classified at
  all.** They were already neither reported nor rewritten, but every one was
  still sent to the interpreter probe — an import of each third-party package
  — for an answer nothing read. They are now left out of the batch. The one
  third-party name still resolved is one a first-party module re-exports
  (`from os import path` in your package's `__init__.py`): an import of it
  from your package is first-party, and whether it names a module depends on
  what `os.path` is.

- **Some first-party `CP001` findings are now `CP002`, and some are no longer
  findings at all.** Consequences of the fix below, and intended ones. A
  first-party `from P import NAME` whose `P.NAME` is not on disk used to be a
  violation whenever it was anything but a directory or `.py`; it is now
  decided by what `P`'s source binds `NAME` to, followed to its origin.
  Imports that were reported, and under `--fix` rewritten, on the strength of
  absence alone — a generated `_version`, a `_pb2`, an out-of-tree extension,
  another portion of a namespace package, a name only a PEP 562 `__getattr__`
  could supply — are now `CP002`, never rewritten, and count toward the exit
  code only under `--strict`. So is a name the parent could also get from a
  third-party star import, even one it defines itself: statement order is
  deliberately not read. The other way round, a module re-exported under a
  name that is not on disk — `from . import _version as version`, `from .sub
  import helpers`, `from os import path` in a package `__init__` — is now
  recognised as a module, where it used to be a `CP001` whose rewrite still
  ran but whose report was wrong. A run that was exiting `1` on these may now
  exit `0`; one under `--strict` may now fail on the new `CP002`s.

- **The first-party scan skips artifact directories at an import root.**
  Building the module map walked every directory under a root except
  `__pycache__` and dot-directories. At a root it now also skips `build`,
  `dist`, `node_modules` and `site-packages`, the list discovery uses, so a
  `build/lib/` copy of the package is no longer a second first-party tree and
  a `build/` or `dist/` no longer makes `build` a first-party name (`from
  build import ProjectBuilder` goes to the interpreter probe, as it should).
  Below a root those names are scanned as before: pip's
  `pip/_internal/operations/build/` is a real package. `exclude` is
  deliberately not applied to the scan — it picks the files to analyse, not
  the modules that exist. The scan itself is unchanged in cost; the
  first-party test is now a set lookup instead of a union rebuilt per call.

- **A file that was not processed now says so.** A parse error was reported
  as `CP002 could not determine whether '?.?' is a module: parse error: ...`;
  it, and the new read, decode and write errors, now read
  `CP002 file not processed: <reason>`. Anything matching the old wording
  needs updating. The exit code is unchanged: always `2`.

- **zuban is now a gate, not a note.** It was an optional third opinion in its
  own dependency group, with a `manual`-stage hook and a CI job engineered to
  never fail. It is now in `dev`, its hook runs on every commit, and it gates
  through the same lint job as everything else; the separate informational CI
  job is gone. It had reported zero on this tree for some time, so promoting it
  cost nothing.

- **Type checking is now split by generation, and `pyright` no longer covers
  `tests/`.** The older pair (`mypy --strict`, `pyright`) checks
  `src/cleanporter`; the newer pair (`zuban`, `pyrefly`) checks
  `src/cleanporter` and `tests/`, minus `tests/fixtures/`. The dividing line is
  what each does with an unannotated function: `--strict` over the test suite
  is 250-odd `no-untyped-def` reports demanding `-> None` on every test, which
  is why mypy never covered it. zuban carries a `[tool.zuban]` section of its
  own — without one it would fall back to `[tool.mypy]` and inherit mypy's
  scope — with the "annotate every definition" family switched off for
  `tests.*` and the rest of strict mode intact. Net effect: `tests/` is checked
  by two checkers instead of one, and by two that see it in more detail.

- **The last `# type: ignore` in the tree is gone**, and with it the only place
  the type checkers were not actually checking. `config._parse_table` collected
  its results into a `dict[str, object]` and splatted it into `Config(**kwargs)`
  — which types every argument as `object`, so the call had to be silenced, and
  the silence covered any genuine mistake in what the parser assigned (pyrefly
  counted eight suppressed errors on that one line). Fields are now passed by
  name, with the defaults read back off `Config` so an absent key and a changed
  default cannot diverge, and keys are still validated in declaration order so
  a table with several bad keys reports the error it always did (checked
  exhaustively against the old implementation over every combination of the
  seven keys, valid and invalid: no difference in outcome, value or message).
  What changed is that a wrong assignment in the config parser is now a type
  error, which for a parser whose job is rejecting wrongly typed input is where
  it belongs.

- **Two tests now patch `ast.parse` directly** rather than reaching for it
  through `cleanporter.rewrite`'s import of it. It is the same module object,
  so the tests do the same thing; spelled the old way it was an implicit
  re-export, which strict checking over `tests/` reports.

- **Checking and fixing now make one decision per import.** Whether an imported
  name is compliant, a `CP001`, unresolved, declined or taken by a `skip` rule
  was worked out twice — once in `analyze.analyze_record` for the report and
  again in `rewrite._Fixer._partition` for the rewrite — and the two copies
  were kept in step by hand. Both now ask `analyze.Decider.decide`, so the
  fixer rewrites only names checking reports as `CP001` (never-read names
  become `CP003` under `--fix`); a guard hit still declines the whole file.
  That holds by construction rather than by review. Findings, reasons and
  their order are unchanged apart from the fix below. A test runs both modes
  over a tree that reaches every finding the decision can produce — each
  `CP003` reason, `CP002` from the resolver and from an unanchorable relative
  import, `CP004` from a rule covering the line and from a pin — under every
  configuration shape that changes it, and asserts they agree import by
  import.

- **`rewrite.py` is split into the fixer and what it plans from.** It had grown
  to some 1,600 lines mixing independent concerns. `TYPE_CHECKING` detection
  moved to `_type_checking.py`, finding and renaming lazy annotation strings
  and `__all__` names to `_annotations.py`, and the small tree walks they share
  to `_nodes.py`, each with its reasoning; `rewrite.py` keeps the plan, the
  transformer and `fix_record`. A pure move: `--diff` output and exit codes are
  byte-identical before and after over the test fixtures, this repository,
  `pygments`, `packaging` and `_pytest`.

- **The source is now clean under every type checker.** `mypy --strict`,
  `pyright` and `zuban` each reported the same nine errors, all of them
  narrowing failures rather than genuine unsoundness: a runtime-built
  `isinstance` tuple (`_TRY_TYPES`, kept for an `ast.TryStar` that has existed
  since 3.11 and so is unconditional at this package's 3.12 floor), two libcst
  unions whose members are siblings rather than subtypes, and one signature
  written wider than its only call sites. All nine are fixed without a
  `cast` or an `Any` -- which is what made the error budget below removable.

- **Lint and type checking now have one definition.**
  `.pre-commit-config.yaml` is it. mypy and pyright used to run through
  `tests/test_typecheck_baseline.py`, a pytest wrapper that counted their
  errors and compared the total against a pinned budget; that only existed
  because the raw tools always exited non-zero. With the budget at zero the
  wrapper bought nothing, so it is deleted and both run as plain local hooks
  next to `zuban`. CI's lint job is now `uv run prek run --all-files` rather
  than its own spelling of the same commands, so it cannot drift from the git
  hooks. Scope moved into `pyproject.toml` for every checker (`[tool.mypy]
  files`, `[tool.pyright] include`), so no invocation needs a path argument.
  `pyright` briefly covered `tests/` too, which turned up two narrowing
  failures in `tests/test_analyze.py`; where each checker's scope settled is
  the split by generation, above.

- **The two ruff pins are now asserted to agree.** Ruff is installed twice and
  unavoidably: `uv run ruff check` uses `uv.lock`'s copy, while the git hook --
  and so CI, which runs the hooks -- uses the one built from `rev:` in
  `.pre-commit-config.yaml`. Nothing made them match, so a `uv lock --upgrade`
  could silently leave the documented local command and the check that gates a
  pull request running different linters. `tests/test_toolchain_pins.py` fails
  when they diverge.

- **Commits on `main` no longer carry a spurious failed check.** The zuban job
  carried `continue-on-error: true`, which does not do what it looks like: the
  *workflow run* concludes `success`, but the job keeps a check run of its own
  and that still concludes `failure`. Commit lists and commit pages render
  check runs, not workflow runs, so every commit wore a red X beside a green
  CI. Worth knowing before reaching for that setting again: the only thing
  that decides a job's check run is whether its steps exit non-zero. The job
  was first rewritten to exit 0 on every path, and has since been folded into
  the lint job, where zuban gates for real (above).

- **The string-mention guard now distinguishes a reference from prose.** It used
  to block a file whenever a rewritten name appeared as a whole word in any
  non-docstring string literal. That is the single largest source of declined
  files by a wide margin — across a 974-file third-party corpus it accounted
  for 3,140 of 3,195 `CP003` findings — and the overwhelming majority of those
  matches were prose no rename could reach (`"expected Type, got int"`,
  `"--include=PATTERN"`, `"@pytest.yield_fixture is deprecated"`).

    A word match is now only reported when the string could *be* a reference:
    its content is parsed, and the name has to turn up as a `Name`, an
    attribute leaf, a keyword-argument name, an `import` alias, or inside a
    nested forward reference — or the content has to read as a dotted/colon
    path (`"mypkg.cli:main"`). Prose neither parses nor reads as a path, so it
    is cleared. A doctest (`>>>` anywhere) and a bytes literal still block, as
    does anything the parse cannot classify.

    Nothing that was provably unsafe becomes fixable: `getattr` arguments,
    `monkeypatch.setattr` dotted paths and eagerly evaluated string
    annotations (`"list[Widget]"`, `"Widget | None"`) all still block, and so
    does a written-out `exec` payload — content is parsed both as written and
    `textwrap.dedent`-ed, so an indented block is still recognised as code.
    `__all__` is stronger than that: it is a name list *by declaration*, so
    every string in one blocks however it is spelled, including
    `__all__ = "Widget helper".split()`, which no content inspection can read
    as code. One level of indirection is followed, so `__all__ = _EXPORTS`
    covers whatever built `_EXPORTS`. cleanporter's own `__init__.py` still declines on its `__all__`,
    so the dogfooding story in the README is unchanged. The boundary is
    pinned by a table of 43 named cases in `tests/test_guards.py`; add
    counterexamples there rather than relaxing the rule.

    Two shapes are given up deliberately, and are listed under *Known
    limitations* in the fixer-safety docs: a regex literal that matches the
    name at runtime, and an `eval`/`exec` payload assembled rather than
    written out.

- **Strings in an annotation slot or an `__all__` list are treated as code
  even when they do not parse.** `find_string_mentions` takes a new
  `strict_ids` argument for string nodes the caller has already proven to be
  code by context. This is what separates prose (cleared) from a *malformed
  type* such as `"Widget["`, or a name list such as `"Widget helper"`, which
  block — a distinction nothing about the content alone can draw.

### Removed

- **`analyze.build`** — use `project.build` (still exported as
  `cleanporter.build`), which returns a `Project` rather than a
  `(records, resolver, errors, warnings)` tuple.
- **`Resolver.note_uses`** — pass the evidence to the constructor instead:
  `Resolver(module_map, python, evidence=...)`.

### Fixed

- **A relative import in a namespace package is no longer rewritten to the
  standard library.** In `analytics/io/__init__.py`, with `analytics/` a PEP
  420 namespace directory that no other file imports, `from .readers import
  read` was rewritten to `from io import readers`. Nothing inside `analytics`
  can show that `analytics/` is not an import root, so the file was qualified
  as `io`, and the fixer wrote that name out: the rewritten package looked for
  `readers` in the standard library's `io` and failed to import. The
  replacement is now `from . import readers`, which names no package and is
  right wherever the root really is.

- **`--python` naming another virtual environment now probes *that*
  environment.** The probe ran in process whenever the target interpreter,
  symlinks resolved, was the same file as cleanporter's own — and every venv
  built from one base Python is a symlink to the same file. So
  `--python other/.venv/bin/python` was answered from cleanporter's `sys.path`,
  not the target's: a package installed only in the target came back `CP002`
  "not importable in the target interpreter", and one installed only beside
  cleanporter was classified as if the target had it — a guess, which is what
  the resolver exists not to make. The probe now runs in process only when no
  interpreter is named or the one named is, by its unresolved absolute path,
  cleanporter's own `sys.executable`; anything else, a bare command name
  such as `python3` included, runs in a subprocess,
  which can cost a process start and never an answer.

- **A relative `python` in `[tool.cleanporter]` is read against the
  `pyproject.toml` directory, not the current directory.** It was the one path
  in the configuration that was not: `python = ".venv/bin/python"` worked from
  the project root and named a file that does not exist from every
  subdirectory, so a run started from `src/` lost its target interpreter and
  every third-party import came back `CP002`. `exclude` and `source_roots`
  already worked this way. A value with no path separator (`"python3"`) is
  still a command looked up on `PATH`, and the `--python` flag, like any path on
  the command line, is still read against the current directory.

- **A third-party package that prints on import no longer breaks the probe.**
  Classifying `from P import S` imports `P`, and a `P/__init__.py` that prints
  a banner wrote it wherever stdout was. Out of process (`--python`) that was
  the channel carrying the probe's JSON reply, so one banner made the reply
  unparseable and the *whole batch* came back undetermined: every third-party
  import in the run became `CP002`, blamed on packages that imported fine
  ("not importable in the target interpreter"). In process it landed on
  cleanporter's own stdout, ahead of a `--diff` patch. The probe now sends what
  packages print to stderr, and frames its reply so that output written below
  Python (an extension module's `printf`, straight on file descriptor 1) cannot
  corrupt it either.

- **A failed out-of-process probe now says why.** A crash, a timeout or a
  missing reply still reports the whole batch undetermined — the transport
  never guesses — but a warning now names the interpreter, how it failed, and
  the tail of its stderr, instead of discarding all of it.

- **A first-party submodule missing from the checkout is no longer called an
  object.** The filesystem layer said "object" for anything that was neither a
  directory nor a `.py` under the tree — a guess by absence, against the
  resolver's own contract. With `pkg/__init__.py` doing `try: from pkg import
  _version` / `except ImportError: _version = None` and no `_version.py`
  checked in (setuptools_scm writes it at build time), `from pkg import
  _version` elsewhere was reported `CP001` and rewritten to `import pkg` plus
  `pkg._version.version`, which raises `AttributeError` whenever the package
  had not happened to import it first. Protobuf `_pb2` modules, Cython modules
  whose `.pyx` is all the tree holds, and sibling portions of a PEP 420
  namespace package took the same path.

  The verdict now comes from evidence, never from absence. With `P.NAME` not
  on disk, every binding of `NAME` in every file claiming `P` is followed to
  where it comes from: a `def`, `class` or assignment is an object; `import X
  as NAME` is a module; `from M import X as NAME` is whatever `M.X` is — a
  submodule on disk, a first-party `M` read the same way, recursively, or,
  for a third-party `M`, the interpreter probe's
  answer for `M.X`, batched into the same single probe round trip as the rest
  of the run. First-party star imports are followed too, through a literal
  `__all__` or the public names, and a name `__all__` lists without binding
  is treated as the submodule the star import would try to import. `NAME` is
  an object only if every binding is, and a module only if every binding is;
  anything else — bindings that disagree, an origin with no source, a
  third-party star import, a runtime-built `__all__`, a module-level
  `__getattr__` with nothing binding the name, a parent with no source — is
  `CP002`, and the message names the missing evidence, e.g. `'pkg._version'
  is not on disk under this run's import roots, and 'pkg' binds '_version' by
  importing its own submodule of that name`. The replacement-import check
  reads the same verdict, so a sibling portion it cannot see is described as
  unseen rather than as "no submodule".

  Cycles are where this could have guessed, so they get their own rule. Star
  imports that loop back add nothing new and are followed; any other step
  that would read "nothing there" off a lookup still in progress — a `from`
  import, an `__all__` name, a `__getattr__` fallback, several files that
  must agree — answers "a circular import, so what it binds depends on import
  order" instead. That keeps the verdict independent of the order questions
  are asked in: a draft of this change called `pkg.m.ver` an object when
  `app.py` was checked alone and undetermined when `app2.py` was checked
  beside it. Lookups are memoised per `(module, name)` once complete, so a
  diamond of star imports twenty layers deep is walked once per module rather
  than once per path; a chain more than 64 links deep, or a cyclic knot that
  would take more than 20,000 steps, ends in `CP002` rather than a
  `RecursionError` or a hang.

- **A submodule shadowed through a star import is no longer called reachable.**
  `pkg/__init__.py` doing `from ._impl import *`, with `_impl` defining a
  function `helpers` and `pkg/helpers.py` beside it, makes `from pkg import
  helpers` bind the function. The ambiguity check read only the `__init__`'s
  own statements, so it called `pkg.helpers` a plain module, and `--fix`
  turned `from pkg.helpers import f` into `from pkg import helpers` plus
  `helpers.f()` — code that imports and parses, then raises `AttributeError`.
  This predates this release's other resolver changes; a fuzz that *runs*
  every import it classifies found it. Star imports are now followed for this
  check as they are everywhere else: one that brings the name in makes the
  submodule ambiguous (`CP002`, and the replacement import is declined with
  `CP003`), and one from a module this run cannot read leaves it undetermined.

- **A package `__init__.py` in a declared non-UTF-8 encoding is read, not
  skipped.** The parses behind all of the above, and behind the ambiguity check
  and the re-export guard, decoded source as UTF-8, so a PEP 263 `# -*- coding:
  latin-1 -*-` file looked like it bound nothing: a submodule it shadowed was
  called plainly reachable, and the re-export guard stood down for names it
  re-exported. Sources are now parsed from bytes, which honours coding cookies
  and a UTF-8 BOM as the interpreter does, and a file that genuinely cannot be
  read or parsed now proves nothing rather than "binds nothing": a submodule
  it might shadow and a name it might bind are `CP002`, and the re-export
  guard assumes the name is re-exported.

- **`--fix` keeps a file's line endings, encoding and BOM.** Files were read
  with universal-newline translation and written back as UTF-8, so a one-line
  fix to a CRLF file rewrote every line ending in it, and the `--diff` patch,
  computed on the translated text, did not apply to the file on disk. Files
  are now read as bytes, decoded the way Python decodes them (a BOM or a
  PEP 263 coding declaration, else UTF-8) without newline translation, and
  written back in the same encoding, BOM and line endings, so every line the
  fix did not change is byte-identical. The patch is emitted as those same
  bytes, applies to CRLF, BOM-prefixed and Latin-1 files alike, and marks a
  missing final newline. A file in a legacy codec that does not round-trip its
  own bytes (`cp932`), a rewrite its declared encoding cannot hold, or a file
  libCST does not reproduce byte for byte (a lone `\r` at the end) is declined
  with `CP003` rather than written lossily.

- **One undecodable file no longer aborts the run.** A valid Latin-1 source
  file with a PEP 263 cookie, or any file that was not UTF-8, raised
  `UnicodeDecodeError` out of the whole run: exit 2, a message naming no file,
  and nothing else checked. Coding declarations are now honoured, and a file
  that genuinely cannot be read or decoded is reported like a parse error —
  `PATH:LINE:0: CP002 file not processed: <reason>`, pointing at the offending
  line — while every other file is checked and fixed as usual. The exit code
  is still `2`. A file `--fix` fails to write is reported the same way,
  rather than aborting the rest of the run.

- **`--fix` writes atomically.** A rewrite goes to a temporary file in the
  same directory and is renamed over the original, keeping its permission
  bits and, where permitted, its owner and group, and the directory is
  fsynced, so a crash or a full disk mid-write can no longer truncate a source
  file. A read-only file is still refused, and a symlink is written through
  rather than replaced. Extended attributes, ACLs and hard links are not
  preserved.

- **`scope = "first-party"` now applies to `--fix` and `--diff` too.** Checking
  passed over a stdlib or third-party import under that scope, but the fixer
  never consulted it: a file containing `from os.path import join` reported
  nothing, exited `0`, and was still rewritten to `from os import path` /
  `path.join(...)` by `--fix`. An import outside the configured scope is now
  neither reported nor rewritten. This was a consequence of the duplicated
  decision described under *Changed*.

  One knock-on effect under that scope: a file `--fix` used to decline because
  a guard hit a stdlib or third-party name it was about to rewrite (that name
  in an `__all__` string, say) no longer wants to rewrite that name, so the
  guard no longer fires and the file's first-party imports may now be
  rewritten.

- **The replacement import is now checked to bind the module it names.** The
  fix for `from P.S import obj` is `from P import S` plus `S.obj` at every use
  site, and that statement binds `getattr(P, "S")` — so a `P/__init__.py` that
  binds `S` to something else (the lazy re-export idiom, `from .S import S`)
  handed the rewrite an object and every rewritten call raised
  `AttributeError`. Source that imports, parses and does not run. Found on
  gt4py, whose `iterator/transforms/concat_where/__init__.py` re-exports
  `transform_to_as_fieldop` under its own submodule's name — and present in
  this project's own corpus the whole time, where `kombu/pidbox.py` was
  rewritten to `from kombu.utils import uuid as uuid_2` and its
  `uuid_2.uuid()` raises. The corpus check imports that module; it never
  calls that line. Emitting `import P.S as alias` instead would not have
  helped: since 3.7 that statement resolves through `getattr(P, "S")` too,
  and binds the same object.

  The check is the one the resolver already had for the import it *reads* — a
  name that is both a submodule on disk and a top-level binding in the
  package's `__init__` is ambiguous, never guessed — now also asked of the
  import the fixer is about to *write*. That subsumes the self-import case
  (`from pkg import S` inside `pkg/__init__.py`), which no longer needs a rule
  of its own. Anything short of a firm "yes, a module" keeps that one import
  as written and reports `CP003`; the rest of the file is still fixed.

  Two consequences beyond the reported bug. The interpreter probe answers this
  shape too: a submodule spec no longer settles it when the parent's own
  `__dict__` holds a non-module under that name, so such an import is `CP002`
  instead of silently compliant. (Eagerly bound, which is what the idiom does
  — a shadow supplied lazily by a module-level `__getattr__` is not detected,
  because asking for it would import the leaf. `docs/safety.md` lists it.)
  And a `P.S` the run cannot see *at all* is not rewritten: a run pointed at one
  distribution of a namespace package cannot see a sibling's modules, and since
  a first-party object now needs a binding in its parent's source (see *A
  first-party submodule missing from the checkout*, above), the import naming
  one is `CP002`. That costs a fix which would have been correct; the finding
  names the missing evidence, and pointing cleanporter at the whole tree, or
  declaring `source_roots`, resolves it.

- **A package importing its own submodule absolutely is no longer read as a
  shadowing binding.** `from . import signals` inside `pkg/__init__.py` was
  already understood to bind the submodule itself; `from pkg import signals`
  — the same statement, spelled absolutely, and how django's
  `db/models/__init__.py` writes it — was not, so `pkg.signals` was reported
  ambiguous (`CP002`) and every consumer's fix was declined along with it.
  The discount is for the statement, not for the name: a competing binding
  elsewhere in the file still makes the pair ambiguous, which the relative
  spelling now honours too (a reassignment used to be subtracted out from
  under itself).

- **An import nothing in the file reads is no longer rewritten.** `from p
  import Thing` with no read of `Thing` has no use site to qualify, so the
  rewrite removed a violation while changing no call — and dropped `Thing`
  from the module's namespace, which is not always private. A test module's
  `from .fixtures import backend_like` exists *only* so pytest can find the
  name by a test's parameter, and the consumer being a function signature
  rather than an import makes it invisible to the re-export guard. Found by
  running gt4py's and icon4py's own suites against a rewritten copy, where it
  accounted for every remaining failure once the DSL bodies were skipped.

  "Read" is libCST's scope analysis, not a text match: a parameter named
  `backend_like`, and every use of it in the body, belong to the parameter's
  binding and are not reads of the import. The import is kept and reported
  `CP003`; a project that keeps such imports deliberately can declare them
  with a `skip` rule or `exempt_names`.

  Keeping a name also removes it from the set the whole-file guards are keyed
  on, so a file that was declined outright — because `__all__`, a `global`
  declaration or an `exec` payload named that one symbol — may now have its
  other imports rewritten. `--fix` therefore touches slightly *more* files
  than before, not fewer. The corpus check confirms the rewrite still changes
  no observable behaviour.

- **`--fix`'s second pass no longer forgets which module a file is.**
  `cli._reparse` rebuilt the post-fix record without its `qualname`, so a
  re-export the fixer had correctly declined to touch was reported as an
  ordinary `CP001` rather than the `CP003` it is.

- **A load-bearing re-export is no longer rewritten.** If `pkg/tool.py` binds
  `dump` by importing it, `pkg.tool.dump` exists only because of how `tool.py`
  spells that import — which the same run may rewrite. Fixing `tool.py` and
  rewriting another file's `from pkg.tool import dump` into `tool.dump` are
  each correct alone and broken together, and the second one silently points
  at what the first deleted.

    The *re-exporting* side is now what gets protected: `tool.py`'s own import
    is reported `CP003` and kept whenever some analysed file *uses*
    `pkg.tool.dump` — spelled `from pkg.tool import dump`, or as a
    `tool.dump` attribute read through an import binding, or reached by
    `from pkg.tool import *`. Counting only the first was not enough: `import
    M` plus `M.name` is the shape this tool rewrites everything *into*, so a
    second `--fix` run would have deleted the attribute the first run had just
    protected. The consumer is still rewritten normally, because the attribute
    it qualifies through is now guaranteed to survive. A re-export nobody uses
    is still fixed, and a name bound both ways (imported under `try`, defined
    in `except`) survives a rewrite anyway and is unaffected.

    The evidence is the set of files under analysis, which is the same
    boundary every other guard has — a consumer outside the run remains the
    documented cross-file limitation. Third-party modules are never rewritten,
    so their re-exports do not move and are not considered.

- **A module and a package that share a name no longer hide each other's
  re-exports.** `pkg.py` sitting beside `pkg/` — what an older single-file
  release looks like when it is left in place next to a newer packaged one —
  made the first-party map key `pkg` to whichever file it scanned last, which
  is the flat module. The guard above then asked `pkg.py` whether `pkg`
  re-exports `helper`, got "no", and let `--fix` delete the re-export out of
  `pkg/__init__.py` while rewriting a consumer to read it — an
  `AttributeError` at import, from code that compiles. Python resolves
  `import pkg` to the package and never looks at `pkg.py`, but which file wins
  is a `sys.path` question in general, so the map now keeps *every* file that
  claims a dotted name and the re-export guard answers for all of them: any
  claimant that re-exports the name protects it. The cost is a fix declined
  where the losing file was the one re-exporting; the alternative cost was
  working code. Found by the corpus check, where `click_plugins.py` (2.0dev)
  beside `click_plugins/` (1.1.1.2) broke `celery.bin.celery`.

- **A new binding in `pkg/__init__.py` no longer takes a submodule's name.**
  There a module-level name *is* the attribute `pkg.<name>`, so rewriting
  `from kombu.serialization import loads` to `from kombu import serialization`
  inside `celery/security/__init__.py` put kombu's module in the slot owned by
  `celery.security.serialization`. Two things then go wrong, and neither
  raises at the point of the mistake: the next `from celery.security import
  serialization` read that attribute instead of importing the submodule — the
  submodule fallback only runs when the attribute is *absent* — so it bound
  `kombu.serialization` under a name meant for celery's; and once anything
  imports the real submodule, the attribute is replaced and this file's own
  `serialization.loads` starts resolving against the wrong module. Code that
  imports, runs, and is wrong.

    The alias allocator now treats a sibling submodule's name as taken, so it
  picks `serialization_2`. Nothing is declined for this — only the chosen name
  changes — and binding a submodule under its *own* name is excluded, since
  the global and the attribute would then hold the same object. Binding *reuse*
  follows the same rule: an import the author already wrote under a sibling
  submodule's name is no more durable than one the fixer would allocate there,
  so references are no longer qualified through it.

    When the shadowing name is the author's rather than one the fixer would
  introduce, no alias can help: that binding stays, so `from pkg import S`
  would keep reading it. That import alone is now reported `CP003` and kept
  verbatim, while the rest of the file is still fixed. Reachability is decided
  by the rule already behind `CP002` — a name that is both a submodule on disk
  and a top-level binding in the package's `__init__` is ambiguous and never
  guessed — rather than by a second rule that could drift from it.

    Found by the corpus check, which is also where the earlier fix stopped
  masking it: `celery.security` went from failing with a swallowed
  `ModuleNotFoundError` to raising `ImproperlyConfigured` out of
  `pkgutil.walk_packages`. The same hazard for a `from pkg import S` emitted
  into a file that is *not* `pkg` is a known gap, recorded in `docs/safety.md`.

- **A plain `import x` inside `if TYPE_CHECKING:` is no longer treated as a
  runtime binding.** Such a block is `GlobalScope` to libCST exactly like the
  module body, so the fixer harvested the import as an already-available
  module, emitted no runtime import at all, and qualified references through
  a name that does not exist at runtime. `from unittest import TestCase`
  alongside a `TYPE_CHECKING`-gated `import unittest` became
  `unittest.TestCase` with nothing importing `unittest` — a `NameError` on
  the first call. `ImportFrom` nodes were already excluded; plain `Import`
  nodes were not. Found by running `_pytest`'s own test suite against a
  rewritten copy of it. An aliased guard
  (`from typing import TYPE_CHECKING as TC` then `if TC:`) is now recognised
  too.

- **`from P import S as S` is no longer rewritten.** Aliasing a name to itself
  is a no-op at runtime and is only ever written to declare that the name is
  part of the module's public surface — PEP 484's *redundant alias*, which
  mypy's `no_implicit_reexport` and ruff's `F401` both honour. Rewriting it
  deleted that public name, breaking every *other* file importing it,
  including files the tool was never pointed at. It is now reported as
  `CP003` in every mode and kept exactly as written, while the rest of the
  file is still rewritten. An ordinary alias (`import Thing as T`) declares
  nothing and is still rewritten.

    This hole predates the string-guard change under *Changed*, but was masked
    by it: rewriting `_pytest` with the narrowed guard broke the package at
    import time on `from .exceptions import UsageError as UsageError`, which
    the old guard had been shielding by accident, via unrelated prose mentions
    of `UsageError` elsewhere in the same file.

## [0.3.0] - 2026-08-27

The publication release: MIT-licensed, documented, and gated by a full tooling
stack. The Python floor moves to 3.12, which is why this is a minor bump.

### Removed

- **Python 3.10 and 3.11 support.** The supported floor is now Python 3.12.
- The `tomli` dependency. With 3.12 as the floor, the standard library's
  `tomllib` suffices, and `config.py` no longer needs the version fork. **libcst
  is now the only runtime dependency.**

### Added

- MIT `LICENSE` file and complete project metadata: homepage, documentation,
  repository, issues and changelog URLs, `grAItools` attribution, keywords, and
  trove classifiers for Python 3.12, 3.13 and 3.14.
- [pyright](https://github.com/microsoft/pyright) as a second **blocking** type
  checker alongside `mypy --strict`, with its accepted-error count pinned to a
  budget in the test suite.
- [zuban](https://zubanls.com/) as an **optional, non-blocking** third type
  checker, in its own `zuban` dependency group, run as an informational
  cross-check rather than a merge gate.
- A [zensical](https://zensical.org/) documentation site under `docs/`, with
  `zensical.toml` and a `docs` dependency group.
- `CONTRIBUTING.md` for human contributors and `AGENTS.md` (with `CLAUDE.md` as a
  symlink to it) for coding agents.
- Continuous integration on GitHub Actions: lint, formatting and both blocking
  type checkers, plus the test suite across Python 3.12, 3.13 and 3.14.

### Fixed

- `cleanporter --version` reported a stale version. `__version__` was a literal
  in `__init__.py` that had to be bumped in lockstep with `pyproject.toml` and
  had already fallen behind; it is now derived from installed package metadata,
  so the two cannot drift apart again.

### Changed

- **cleanporter now complies with the rule it enforces.** `cleanporter --fix`
  was applied to its own `src/` and `tests/`, removing 128 violations. The
  public-API re-exports in `__init__.py` remain and are reported: the fixer
  declines that file because the names appear in `__all__` as string literals,
  and the finding is left visible rather than silenced with an `exclude`.
- **Ruff is now the sole linter and formatter**, with the full rule set enabled
  (`select = ["ALL"]`) and every exception carrying its justification.
  `ruff format` is enforced at a 100-column line length. There is deliberately no
  black, isort or flake8 in this project.

## [0.2.0] - 2026-08-26

The release in which cleanporter became a fixer rather than only a checker: the
`modimports` prototype was merged in, the resolver gained its layered
never-guess design, and the rewriter gained its all-or-nothing safety model.

### Added

- An `argparse` command-line interface with `0` / `1` / `2` exit codes, replacing
  the Typer-based one.
- Configuration from `[tool.cleanporter]` in the nearest `pyproject.toml`:
  `exclude`, `scope`, `source_roots`, `treat_unresolved_as_error`,
  `exempt_modules`, `exempt_names` and `python`. CLI flags layer on top of config
  values rather than replacing them.
- `scope = "first-party"`, limiting reporting to the project's own modules.
- Path expansion with exclude globs and always-skipped directories
  (dot-directories, `__pycache__`, `node_modules`, `build`, `dist`,
  `site-packages`); a path named explicitly on the command line bypasses every
  filter.
- The all-or-nothing rewrite gate, with guards that block a whole file and
  explain why via `CP003`: string-literal mentions of a renamed binding,
  module-level rebinding, `global` / `nonlocal` declarations, `match` capture
  patterns, `del` of the local name, and comments that a rewrite could not carry
  across (including comments inside a parenthesized import).
- Rewriting of imports that are not at module scope, each scope getting its own
  binding independently of any module-level import of the same module.
- Rewriting of `if TYPE_CHECKING:` imports when `from __future__ import
  annotations` is active, including the lazy annotation strings that mention the
  renamed name.
- Verification that the rewritten source re-parses before it is returned; on
  failure the original content is kept and an internal-error finding is emitted.
- An interpreter probe bridge, batching every `(parent, name)` pair for a run
  into one round trip. It runs in-process by default and out-of-process when
  `--python` names a different interpreter, which keeps cleanporter's own
  dependencies out of the target venv and contains native-library crashes in a
  subprocess.

### Changed

- Ambiguity is reported, never guessed: a name that is both a submodule on disk
  and a top-level binding in the parent's `__init__.py` is `CP002`.
- First-party C-extension submodules (`.so` / `.pyd`) are classified as modules.
- Import roots are ranked rather than picked arbitrarily: a root too shallow for a
  file's own relative imports is discarded, a directory some other file imports by
  an absolute name is demoted from root to package, a declared root beats an
  inferred one, and otherwise the most specific root wins.
- Prose docstrings are exempt from the string-literal guard; docstrings containing
  a `>>>` doctest marker are not.
- Comments on rewritten import lines are preserved, and a rewrite that would
  discard one blocks the file instead of dropping it silently.
- When a patch is produced (`--diff` or `--fix`), **stdout carries only the
  patch**; warnings, findings and the summary go to stderr, so
  `cleanporter --diff src/ | git apply` works.
- `--fix` prints a note to stderr, whenever it writes, that per-file guards cannot
  see dotted references from other files — re-run your test suite.
- Each file's import units and node positions are computed once per run rather
  than once per pass.

### Fixed

- Relative imports that cannot be anchored (they climb above the first-party
  root) are reported as `CP002` instead of being dropped.
- I/O errors in `main` are handled rather than escaping as tracebacks.
- A PEP 420 namespace directory no longer poses as an import root, and a
  namespace package that holds a regular subpackage is not treated as one either.
- Bindings from `for` targets and `match` patterns, `level == 1` self-imports,
  aliased imports and bare annotations are handled correctly when collecting a
  module's top-level bindings.
- A memoized module token that turns out to be shadowed further down the file is
  re-allocated fresh, and a new import binding shadowed by a nested-scope local is
  avoided.
- Lazy annotation strings are parsed rather than pattern-matched, the re-wrapped
  annotation is verified to round-trip, and any unrenderable nested content aborts
  the annotation rewrite.
- An existing binding that is rebound or deleted is never reused for a rewrite.

[Unreleased]: https://github.com/grAItools/cleanporter/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/grAItools/cleanporter/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/grAItools/cleanporter/releases/tag/v0.3.0

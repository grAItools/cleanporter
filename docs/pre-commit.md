# pre-commit

The repository publishes two hooks for [pre-commit](https://pre-commit.com/)
and [prek](https://prek.j178.dev/), in its
[`.pre-commit-hooks.yaml`](https://github.com/grAItools/cleanporter/blob/main/.pre-commit-hooks.yaml):

| Hook id | Runs | Fails the commit when |
| --- | --- | --- |
| `cleanporter` | `cleanporter --whole-project FILES` | a changed file has a `CP001` or `CP003` (or a `CP002` under `--strict`), or cannot be read or parsed. |
| `cleanporter-fix` | `cleanporter --whole-project --fix FILES` | it rewrote a file (the rewrite is left in your working tree to review and stage), or something it could not rewrite remains. |

Add one of them to your project's `.pre-commit-config.yaml`:

```yaml
repos:
  - repo: https://github.com/grAItools/cleanporter
    rev: vX.Y.Z  # a release tag
    hooks:
      - id: cleanporter
      # or, to rewrite what is provably safe:
      # - id: cleanporter-fix
```

`rev` is a release tag. The hooks first ship in the release after `0.4.0`;
`pre-commit autoupdate` (`prek autoupdate`) moves `rev` to the latest one.

Both hooks run on `*.py` files only (`types: [python]` plus `files: \.py$`:
an extensionless script with a python shebang is left out, because the
project walk only collects `*.py` and would never check it), configure
themselves from `[tool.cleanporter]` in your `pyproject.toml` like any other
run, and take extra flags through `args` — `args: [--strict]`, say. The
hooks' own flags are part of their `entry`, not their `args`, so setting
`args` adds to them rather than replacing `--whole-project`. pre-commit lets
you override `entry` on a remote hook too, so `entry: cleanporter` drops the
flag without a `repo: local` hook.

## Why the whole project is read

pre-commit hands a hook only the files that changed. A run over those alone
judges them on a *partial* tree, and some of what cleanporter decides is
cross-file:

- **Uses in other files.** Before rewriting a module's own `from P import
  name`, the fixer checks whether another file imports `name` *from that
  module* — a re-export — since the rewrite deletes the attribute
  (the load-bearing guard, see [Safety](safety.md)). A file the run does not
  read is not evidence, so a re-export used only by an unchanged file looks
  unused: check mode reports `CP001` instead of `CP003`, and `--fix` rewrites
  it and breaks the unchanged file at import time.
- **Which packages are first-party.** Import roots are
  [inferred from the files a run is given](how-it-works.md). Given only
  `tests/test_app.py`, a package under `src/` is not first-party at all, so
  its imports go to the interpreter probe, which answers from whatever is
  installed under that name — nothing (`CP002`), or an older, non-editable
  copy of your own package. The same inference is what tells a PEP 420
  namespace directory from an import root; without the files that import it,
  a file under one can be given the wrong module name.

`--whole-project` closes both. The run reads the whole project — the
directory of the `pyproject.toml` above the first listed file that has one,
walked as `cleanporter .` run from that directory would walk it — so every
file's imports are evidence and every package is on the map, and then fixes,
reports and counts only the files it was given. A rewrite is judged on the
evidence of a full run over the `pyproject.toml` root, plus any listed
outside files. The exit code is decided by the
given files alone: a violation in a file you did not touch does not block
your commit, and a file elsewhere that cannot be parsed is a warning (its
imports are then missing from the evidence, which the warning says).

Consequences worth knowing:

- **A `pyproject.toml` is required, and only one.** The project is the
  `pyproject.toml` the listed files sit under; a symlink is placed by where
  it sits, not by its target. When no listed file has one there is no
  telling where the project starts, and when they sit under *different*
  ones there is no single project to judge them on: either way the run
  exits `2` rather than pick one, whatever order pre-commit lists files in.
  A file under no `pyproject.toml` at all is analysed alongside. Override
  the hook's `entry` (`entry: cleanporter`) if your project has none.
- **`exclude` is honoured for the files pre-commit passes.** A changed file
  that `exclude` (or a skipped directory such as `build/`) leaves out of the
  walk is not reported, as it would not be by `cleanporter .` run from the
  project root. Without the flag, a file named on the command line is always
  checked.
- **Paths are compared as the filesystem spells them.** A listed file is
  matched to the walked tree by its resolved path, and on a case-insensitive
  filesystem (macOS by default) a path spelled in a different case from the
  one on disk is not matched, and so not reported. pre-commit passes the
  paths the repository records, which match.
- **Every commit reads the whole tree.** That is why both hooks are
  `require_serial`: split into parallel batches, each process would read it
  again. Unstaged changes are stashed by pre-commit while hooks run, so the
  tree read is what you are committing, plus any untracked files.
- **`pyproject.toml` not at the repository root? Add a `files:` pattern.**
  pre-commit passes every changed Python file in the repository. A file
  under no `pyproject.toml` (a `scripts/` beside the project) is analysed and
  reported under this project's configuration, with a warning; a file of
  another project stops the run (below). Scope the hook to the project,
  `files: ^myproject/.*\.py$`.

## Several projects in one repository

A commit that touches files of two projects — a monorepo, or a nested
`examples/` or `benchmarks/` directory with its own `pyproject.toml` inside
your project — makes a `--whole-project` run exit `2` with the projects
named: one run judges one project. Split the hook so each entry sees one
project's files:

```yaml
repos:
  - repo: https://github.com/grAItools/cleanporter
    rev: vX.Y.Z
    hooks:
      # The outer project, without the nested one.
      - id: cleanporter
        exclude: ^examples/ex/
      # The nested project, on its own (drop this entry to not check it).
      - id: cleanporter
        name: cleanporter (examples/ex)
        files: ^examples/ex/.*\.py$
```

The `files:` given here replaces the hook's own `\.py$`, so keep the
suffix in the pattern.

## Evidence stops at the nearest `pyproject.toml`

The whole project is one `pyproject.toml`'s directory, and nothing outside it
is read. In a repository of nested projects or a uv workspace, a consumer in
*another* project is invisible, as it is to any run. Say
`libs/foo/pyproject.toml` and `apps/web/pyproject.toml`, with
`libs/foo/src/foo/__init__.py` re-exporting `helper` (`from foo.core import
helper`) and only `apps/web/app.py` importing it from there (`from foo import
helper`). A commit touching `libs/foo` runs on `libs/foo`'s project;
`apps/web` is not evidence, so the re-export looks unused, and
`cleanporter-fix` rewrites it and breaks `apps/web` at import time.
`--whole-project` judges a changed file on a full run over its own project,
not over the repository. Where projects import each other, prefer the
`cleanporter` check hook, and run your whole test suite after any fix.

## Exit codes and `cleanporter-fix`

The exit codes are the [usual ones](usage.md#exit-codes). `--fix` re-checks
each file after writing it, so `cleanporter-fix` exits `0` when it rewrote
everything the changed files needed and `1` when something remains
(`CP001` it could not reach, `CP003` it declined). pre-commit fails a hook
that modified files whatever it exits with, so a commit with fixable
violations fails once, with the fixes in your working tree; review them,
stage them and commit again. `--fix` cannot see a dotted reference from
another file (a `monkeypatch.setattr("pkg.mod.name", ...)` string), so run
your tests before that second commit.

## Which interpreter the probe uses

Stdlib and third-party names are classified by importing them in an
interpreter (see [the probe interpreter](configuration.md#the-probe-interpreter)).
pre-commit runs the hook inside its own virtual environment and sets
`$VIRTUAL_ENV` to it, and that environment holds cleanporter and libcst but
none of your dependencies. Detection prefers the project's own environment
to `$VIRTUAL_ENV`, so:

- **With a `.venv` at the project root** (or `$UV_PROJECT_ENVIRONMENT`, or a
  uv workspace root's `.venv`), detection finds it and third-party imports
  are classified there. Nothing to configure; the note on stderr names the
  environment and the hook environment it passed over.
- **Otherwise** the hook's own environment is the one left, and every
  third-party import is `CP002` — reported, never rewritten, and not a
  failure unless `--strict`. To classify them, either name the interpreter
  that has them, with `python =` in `[tool.cleanporter]` or `args: [--python,
  /path/to/python]`, or install them into the hook's environment with
  `additional_dependencies`:

  ```yaml
      hooks:
        - id: cleanporter
          additional_dependencies: [numpy, requests]
  ```

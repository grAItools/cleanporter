# Contributing to cleanporter

Thanks for taking the time. cleanporter is a small, deliberately conservative
tool: it enforces [Google Python Style Guide §2.2](https://google.github.io/styleguide/pyguide.html#s2.2-imports)
("import modules, not the names inside them") and can rewrite the violations it
finds. Because it *edits other people's source code*, the bar for a change is
"provably correct", not "works on my tree". Most of the guidance below exists to
protect that property.

## Getting set up

Everything runs through [uv](https://docs.astral.sh/uv/). `uv.lock` is committed,
so a sync gives you exactly the environment CI uses.

```bash
git clone https://github.com/grAItools/cleanporter
cd cleanporter
uv sync                 # dev group: pytest, ruff, the four type checkers, prek
uv run prek install     # install the local git hooks
```

Python **3.12 or newer** is required. The test suite is verified green on 3.12,
3.13 and 3.14.

[prek](https://github.com/j178/prek) is a drop-in replacement for `pre-commit`
written in Rust; it reads the same `.pre-commit-config.yaml`. If you already have
`pre-commit` installed and prefer it, it will work too — prek is what the dev
group pins because it is faster.

## Command reference

| Task | Command |
| --- | --- |
| Run the tests | `uv run pytest` |
| Run one test file | `uv run pytest tests/test_rewrite.py` |
| Lint | `uv run ruff check` |
| Lint with autofix | `uv run ruff check --fix` |
| Format | `uv run ruff format` |
| Check formatting only | `uv run ruff format --check` |
| Type check `src/` | `uv run mypy --strict` |
| Type check `src/` | `uv run pyright` |
| Type check `src/` + `tests/` | `uv run zuban mypy` |
| Type check `src/` + `tests/` | `uv run pyrefly check` |
| Every blocking hook | `uv run prek run --all-files` |
| Preview the docs | `uv run --group docs zensical serve` |
| Build the docs | `uv run --group docs zensical build` |
| Run the tool itself | `uv run cleanporter --help` |

Three notes on running these:

- **All four gate.** `mypy`, `pyright`, `zuban` and `pyrefly` are in the `dev`
  group, run as git hooks, and run in CI through those same hooks. There is no
  informational tier any more; see [Type checking](#type-checking) below.
- **`pyright` is slow the first time.** Its PyPI wrapper downloads a Node runtime
  on first invocation. Subsequent runs are fast; if the first one seems to hang,
  it is fetching Node.
- **`--all-files` is not optional.** `prek run` without it judges only *staged*
  files, and with nothing staged every hook reports `(no files to check)
  Skipped` — which reads just like a pass.

## Code style

- **Ruff is the only linter and formatter in this project.** `[tool.ruff.lint]`
  sets `select = ["ALL"]` and then subtracts a short, individually justified
  ignore list; `ruff format` runs at a 100-column line length. There is
  deliberately no black, no isort, no flake8 — please do not add one, and please
  do not add an ignore without a comment explaining why the rule does not apply
  here.
- Code follows the [Google Python Style Guide](https://google.github.io/styleguide/pyguide.html).
- Docstrings use the **Google convention**, enforced by ruff's pydocstyle rules.
  The *shape* is enforced; a docstring is not demanded on every public object
  (`D100`–`D107` are off). Write one where it earns its place — particularly for
  anything in the safety model, where the reasoning matters more than the code.
- Complexity limits in `[tool.ruff.lint.mccabe]` and `[tool.ruff.lint.pylint]`
  are ratchets set to the current worst case in the tree. Existing code passes;
  new growth has to justify itself. Do not raise them casually.

## Type checking

Four type checkers run, and **all four are gates**: `mypy --strict`, `pyright`,
`zuban` and `pyrefly`. Each reports **zero** errors on this tree, so any
diagnostic you see is something your change introduced.

That was not always true. There used to be nine accepted errors, all blamed on
libcst's partially-typed surface — union shapes like `Name | Tuple | List` that
a strict checker cannot narrow. On inspection every one of them was a narrowing
failure that the code could state properly: a runtime-built `isinstance` tuple,
two unions whose members are siblings rather than subtypes, and one signature
written wider than its only call sites. None needed a `cast`. Treat "this is
just libcst" as a hypothesis to test, not an explanation.

If your change adds an error, the fix is to type your code correctly — not a
`# type: ignore`, a `cast`, an `Any`, or a loosened setting. If you genuinely
believe you have hit an unavoidable libcst shape, say so explicitly in the PR
description and expect to be asked to prove it.

There are no type suppressions in the tree at all — no `# type: ignore`, no
`# pyright: ignore` — so "zero errors" means the checkers looked at everything
and had nothing to say, rather than that somebody told them not to look. The
last one lived on `Config(**kwargs)` in `config.py`, where splatting a
`dict[str, object]` typed every argument as `object`; it hid eight real
diagnostics and, with them, any mistake the config parser might have made.

Run `uv run mypy --strict`, `uv run pyright`, `uv run zuban mypy` and
`uv run pyrefly check` directly, or `uv run prek run --all-files` for those plus
ruff. None of them take a path: scope comes from `pyproject.toml`
(`[tool.mypy] files`, `[tool.pyright] include`, `[tool.zuban] files`,
`[tool.pyrefly] project-includes`), so every invocation checks the same thing.

### Who checks what

| Checker | Scope | Configured by |
| --- | --- | --- |
| `mypy --strict` | `src/cleanporter` | `[tool.mypy]` |
| `pyright` | `src/cleanporter` | `[tool.pyright]` |
| `zuban` | `src/cleanporter`, `tests` | `[tool.zuban]` |
| `pyrefly` | `src/cleanporter`, `tests` | `[tool.pyrefly]` |

The split is by generation, and the reason is what a checker does with an
unannotated function. `mypy --strict` over the test suite is 250-odd
`no-untyped-def` reports demanding `-> None` on every test — annotations that
would carry no information, because pytest calls those functions and nothing
else does. So the older pair stays on `src/`, and the newer pair, which is fast
enough to cover both trees, takes `tests/` as well: zuban with the
"annotate every definition" family switched off for `tests.*` in
`[[tool.zuban.overrides]]` (the rest of strict mode still applies, and the test
bodies are still checked), pyrefly with its own defaults, which do not demand
signatures.

`tests/fixtures/` is excluded from both of the wide-scope checkers, for the same
reason ruff excludes it: fixtures are input data for the tool — arbitrary user
code it must handle — not project code, and one written to exercise a weird
shape must not be able to fail the lint job.

Three traps worth knowing, all of them the kind that stays green:

- **zuban reads `[tool.mypy]` when `[tool.zuban]` is absent.** That section is
  not a nicety; it is what lets zuban's scope differ from mypy's. Remove it and
  zuban quietly narrows to `src/`.
- **pyrefly drops include patterns that sit under a hidden directory.** If your
  checkout lives somewhere with a dot-component in its path (`~/.worktrees/…`,
  for instance), `pyrefly check` will skip `tests`, say so in a single
  `WARN Skipping include pattern …` line, and then exit 0. A normal clone and
  CI are unaffected; if you work out of such a directory, read that line.
- **A typo in a checker's own config section is green almost everywhere.** An
  unrecognised key in `[tool.mypy]`, `[tool.pyright]` or `[tool.pyrefly]` gets
  a notice — `Unrecognized option`, `Config contains unrecognized setting`,
  `WARN … Extra keys found in config` — and then exit 0, so the hook passes.
  zuban is the only one of the four that refuses to run on a config it does not
  understand. After editing any of those sections, read the output rather than
  the exit code.

## Documentation and the anti-drift tests

User documentation lives in `docs/` and is built with
[zensical](https://zensical.org/) (configured in `zensical.toml`):

```bash
uv run --group docs zensical serve    # live preview on localhost:8000
uv run --group docs zensical build    # produce the static site
```

Note that `--group` adds to the default groups rather than replacing them, so
this keeps `dev`. It is `docs` that is not a default: a later plain `uv sync`
reconciles the environment back to the defaults and uninstalls zensical, so
re-run `uv sync --group docs` when you next want the docs tooling.

The test suite **asserts that the documentation matches the code**. Tests in
`tests/` walk the real argument parser and the real config-key set and fail if:

- a CLI flag exists that the documentation does not mention;
- a `[tool.cleanporter]` config key exists that the documentation does not
  mention;
- a finding code (`CP001`, `CP002`, `CP003`) is undocumented.

This is deliberate, not an accident of over-testing. **Adding a flag or a config
key without documenting it will fail the suite**, and that is the intended
behaviour: the documentation is the tool's contract with its users, and drift in
it is a real defect. Update `docs/` in the same commit as the code change.

Some of those tests also pin specific claims that a review found to be false at
some point (for example, that comment preservation is not over-claimed). If one
of them fails, read the test's docstring before changing the prose — it is
usually recording a correctness lesson, not a wording preference.

## Working on the tool itself

Two design invariants govern almost every review comment you are likely to get:

1. **The resolver never guesses.** Deciding whether `from a.b import C` imports a
   module or an object cannot always be done from source text. Anything
   cleanporter cannot *prove* is reported as unresolved (`CP002`) and left alone.
   A heuristic that is right 95% of the time is not an improvement — the other 5%
   emits broken code into somebody's repository.
2. **The fixer is all-or-nothing per file.** Before it touches anything, it must
   prove every rename in that file safe. If any guard fires, the whole file is
   left byte-identical and the reason is reported as `CP003`. Partial rewrites are
   not an acceptable middle ground.

If you are adding a guard, add the test that shows the unguarded rewrite
producing wrong code first.

### Re-run your test suite after `cleanporter --fix`

This one bites, and it is worth internalising before you use the tool on
anything — including on this repository.

**cleanporter's safety guards are per file.** They analyse the file being
rewritten and nothing else. So a string in a *different* file that names a
rewritten binding by its dotted path is invisible to them and can go stale:

```python
# tests/test_thing.py -- cleanporter cannot see this while rewriting pkg/cli.py
monkeypatch.setattr("pkg.cli.helper", fake)
```

The same applies to entry points in `pyproject.toml`, `importlib` lookups,
plugin registries, Django-style string references — anything that names a binding
as text from outside the file. After running `cleanporter --fix` over any
codebase, **re-run that codebase's test suite.** `--fix` prints a note to stderr
saying exactly this whenever it writes a file.

This is not hypothetical: it was found by running cleanporter over its own
source, where a test patched `cleanporter.cli.fix_record` — a name the compliant
rewrite no longer bound there.

## Commits

This project uses [Conventional Commits](https://www.conventionalcommits.org/).
Look at `git log --oneline` for the house style; the prefixes in use are:

```
feat:     a new capability
fix:      a bug fix
docs:     documentation only
test:     tests only
perf:     a performance change with no behaviour change
refactor: a restructuring with no behaviour change
chore:    tooling, packaging, housekeeping
```

Write the subject in the imperative and describe the *effect*, not the mechanics:
`fix: stop a PEP 420 namespace directory posing as an import root` rather than
`fix: change firstparty.py`. The body is the right place for the reasoning — this
codebase's history is used as documentation for why a guard exists.

While the project is on 0.x, a `feat:` that changes the CLI surface or the config
schema warrants a minor version bump; see `CHANGELOG.md`.

## Pull request checklist

Before you open a PR:

- [ ] `uv run pytest` passes.
- [ ] `uv run ruff check` is clean.
- [ ] `uv run ruff format --check` is clean (or you ran `uv run ruff format`).
- [ ] `uv run prek run --all-files` is clean — that is ruff, ruff format, mypy,
      pyright, zuban and pyrefly, the same six checks CI runs.
- [ ] New or changed CLI flags, config keys and finding codes are documented in
      `docs/` (the anti-drift tests will tell you if they are not).
- [ ] New behaviour has a test; a bug fix has a test that fails without the fix.
- [ ] Anything the fixer newly declines to rewrite emits a `CP003` explaining why.
- [ ] Commits follow Conventional Commits.
- [ ] `CHANGELOG.md`'s `## [Unreleased]` section mentions user-visible changes.

CI runs `uv run prek run --all-files` once — the same hooks you run locally,
which is the point: CI does not re-spell the commands, so it cannot drift from
`.pre-commit-config.yaml` in its options. The test suite runs separately on
Python 3.12, 3.13 and 3.14.

zuban used to have a CI job of its own that reported disagreements as a warning
annotation and always exited 0. It is gone: zuban is a hook like the others
now, so it gates through the lint job.

## Reporting bugs

The most useful bug report for a codemod is a minimal input file, the command you
ran, what cleanporter produced, and what you expected. If `--fix` produced code
that does not run, that is a high-severity bug — say so, and include the
traceback from the *rewritten* code.

## License

By contributing you agree that your contributions are licensed under the MIT
License, as in `LICENSE`.

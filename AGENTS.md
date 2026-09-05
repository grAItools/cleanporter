# AGENTS.md

Guidance for coding agents here. Humans want `CONTRIBUTING.md`; this is the
short version plus what an agent will otherwise get wrong.

## What this is

cleanporter enforces Google Python Style Guide §2.2 (import *modules*, not the
names inside them): it reports `from P import S` where `S` is provably an object
(`CP001`) and, with `--fix`, rewrites the import and every use site
format-preservingly via libCST. Two design contracts are the product; a change
that weakens either is a regression no matter how well it tests:

1. **The resolver never guesses.** Module or object is decided in layers
   (first-party filesystem → interpreter probe, in-process unless `--python`
   names another interpreter → undetermined). What it cannot *prove* is `CP002`,
   never rewritten. No heuristics, no "CapWords means class", no fallback that
   picks an answer.
2. **The fixer is all-or-nothing per file.** Every rename must be proven safe
   before anything is touched; one guard hit leaves the file byte-identical and
   emits `CP003` with the reason. No partial-rewrite or "fix what we can" mode.

Findings: `CP001` violation, `CP002` unresolved, `CP003` skipped-on-purpose,
`CP004` taken out by a `[tool.cleanporter.skip]` rule. Exit codes: `0` clean,
`1` violations, `2` operational error. `cli.run` folds `CP003` into the failure
count -- a declined violation, not a note -- while `CP002` counts only under
`--strict`/`treat_unresolved_as_error` and `CP004` never counts at all: it is
the author's own configuration reporting back, printed only under
`--show-skipped`.

The code is `src/cleanporter/`; every module opens with a docstring giving its
role and reasoning, at length in `guards.py`, `firstparty.py`, `resolver.py`,
`rewrite.py` and `_probe.py`. Read it before changing the module.

## Commands

Everything runs through `uv`. `uv.lock` is committed; Python floor is 3.12.

```bash
uv sync                                   # dev group
uv run prek install                       # local git hooks (.pre-commit-config.yaml)
uv run pytest                             # tests
uv run ruff check                         # lint (the only linter)
uv run ruff format                        # format (the only formatter, 100 cols)
uv run mypy --strict                      # type check, src/
uv run pyright                            # type check, src/ (fetches Node on first run)
uv run zuban mypy                         # type check, src/ + tests/
uv run pyrefly check                      # type check, src/ + tests/
uv run prek run --all-files               # every blocking hook: the above six
uv run --group docs zensical build        # docs build (`serve` for a live preview)
uv run corpus/run.py                      # rewrite real packages, run them; --skip-install
                                          #   reuses an installed corpus (~30-45 min)
```

No type checker takes a path: each reads its scope from `pyproject.toml`, so
every invocation checks the same thing. `--all-files` is not optional either:
`prek run` otherwise judges only *staged* files, and with nothing staged every
hook reports "(no files to check) Skipped", which reads exactly like a pass.

**And `--all-files` means every file *git knows about*.** A brand-new module
you have not `git add`-ed is untracked, so every hook silently skips it while
still printing six green lines. A change that adds files is therefore not
checked by the thing that reports it is checked -- `git add` first, or run
`uv run ruff format --check .` and `uv run ruff check` directly, which do walk
the working tree. Both `skip.py` and `test_skip.py` reached "all six hooks
pass" while unformatted this way.

## Hard rules

- **All four type checkers stay clean.** `mypy --strict` and `pyright` cover
  `src/cleanporter`; `zuban` and `pyrefly` also cover `tests/` (minus
  `tests/fixtures/`: input data for the tool, not project code). All four gate,
  as git hooks and in CI, so any diagnostic is a regression, not debt. **Never**
  land code behind a `# type: ignore`, a `cast`, an `Any` or a loosened setting:
  there is no budget and the tree carries no type suppressions at all. Both
  precedents went the same way -- the nine errors a budget once covered were
  narrowing failures, and the last `# type: ignore` (`config.py`'s
  `Config(**kwargs)`) hid eight more.
- **`.pre-commit-config.yaml` is the single definition of lint and type
  checking.** CI's lint job is `uv run prek run --all-files`, so add a check by
  adding a hook, never a CI step. Scope comes from `pyproject.toml`
  (`[tool.mypy] files`, `[tool.pyright] include`, `[tool.zuban] files`,
  `[tool.pyrefly] project-includes`), not from hook arguments. `[tool.zuban]`
  exists precisely so zuban's scope can differ from mypy's -- delete it and
  zuban silently falls back to `[tool.mypy]` and stops checking `tests/`.
- **Run the corpus check before changing the resolver, a guard or the fixer.**
  `uv run corpus/run.py` (see `corpus/README.md`) rewrites a pinned set of real
  third-party packages, then *imports and runs* them. Every fixer safety bug so
  far surfaced there and not in `tests/`: they do not fail a parse, so the
  fixer's own re-parse backstop passes them. CI runs it daily, not per-PR, so
  nothing else will catch these for you.
- **The two ruff pins must agree.** `uv run ruff check` uses `uv.lock`'s copy;
  the git hook, and so CI, uses the one built from `rev:` in
  `.pre-commit-config.yaml`. `tests/test_toolchain_pins.py` asserts they match.
  After a `uv lock --upgrade` that bumps ruff, bump `rev:` in the same commit.
- **Docs anti-drift tests are real gates.** Tests in `tests/` walk the live
  argument parser and config-key set and fail on an undocumented CLI flag,
  `[tool.cleanporter]` key or finding code. Adding one means updating `docs/` in
  the same change.
- **`_probe.py` stays stdlib-only.** It runs inside an arbitrary target
  interpreter (`--python`) that will not have libcst. Its only imports are
  `importlib`, `importlib.util`, `json`, `sys`, `types` -- nothing from this
  package, no dependencies.
- **Ruff is the sole linter and formatter**, `select = ["ALL"]` with a justified
  ignore list. Never introduce black, isort, flake8 or autopep8; never add a
  bare `# noqa` or a new ruff ignore without a comment giving the reason.
- **The package complies with its own rule.** `src/` and `tests/` are both
  clean under `cleanporter`; keep them that way -- write `from cleanporter
  import model` and `model.Status`, not `from cleanporter.model import Status`.
  The sole exception is the public-API re-export block in `__init__.py`,
  which is deliberately *not* silenced with an `exclude` or a `skip` rule. Do
  not add one. It is held by the never-read guard -- nothing in `__init__.py`
  reads those names, so rewriting the imports would only delete the public
  surface -- and behind that by the string-mention guard, since the same names
  appear in `__all__` as string literals. `cleanporter .` reports them as
  `CP001` and exits 1; under `--fix` they become `CP003` and the file is left
  byte-identical.
- **`libcst` is the only runtime dependency.** Tooling belongs in a dependency
  group.
- **Conventional Commits** (`feat:`, `fix:`, `docs:`, `test:`, `perf:`,
  `refactor:`, `chore:`). Subject in the imperative, describing the effect.
- Code follows the Google Python Style Guide; docstrings the Google convention
  (shape enforced, not required on every object).

## Gotchas

- **Re-run the target's test suite after any `cleanporter --fix`.** Guards are
  per file, so a string in *another* file naming a rewritten binding by its
  dotted path (`monkeypatch.setattr("pkg.cli.helper", ...)`, an entry point, an
  `importlib` lookup) is invisible and can go stale. It bit this repo's own
  suite once; `--fix` prints a note to stderr saying so.
- **Never lower a guard, widen an exemption, or make the resolver optimistic to
  make a test pass.** Add the guard's counterexample test instead. Guards are
  settled product decisions argued in their docstrings (prose docstrings exempt,
  doctests not; f-strings rewrite, nested plain strings block) -- read the
  docstring before re-litigating one.
- The mccabe/pylint complexity limits in `pyproject.toml` are ratchets set to
  the current worst case (`rewrite.py::_plan_line`). Do not raise them to land
  code.
- **pyrefly silently skips includes under a hidden parent directory.** In a
  checkout whose path has a dot-component (an agent worktree under `~/.paseo/`
  or `.claude/worktrees/`), `pyrefly check` drops `tests` from
  `project-includes`, warns once with `WARN Skipping include pattern ...`, and
  still exits 0 -- passing having checked half of what it claims. Read that line
  if it appears; a normal clone, CI and zuban are unaffected.

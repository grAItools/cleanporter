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
uv sync                 # dev group: pytest, ruff, mypy, pyright, prek
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
| Type check against the budget | `uv run pytest -m typecheck` |
| Type check, raw output | `uv run mypy --strict src/cleanporter` |
| Type check, raw output | `uv run pyright` |
| Type check (optional) | `uv sync --group zuban && uv run --group zuban zuban mypy --strict src/cleanporter` |
| All local hooks | `uv run prek run --all-files` |
| Preview the docs | `uv run --group docs zensical serve` |
| Build the docs | `uv run --group docs zensical build` |
| Run the tool itself | `uv run cleanporter --help` |

Two notes on the type checkers:

- **`pyright` is slow the first time.** Its PyPI wrapper downloads a Node runtime
  on first invocation. Subsequent runs are fast; if the first one seems to hang,
  it is fetching Node.
- **`zuban` is optional and non-blocking.** It is a third type checker kept in its
  own dependency group as an informational cross-check — a second opinion on
  whether an accepted error is genuinely unavoidable. It is not in `dev` and never
  gates a merge. Note that `uv sync --group zuban` makes the environment match
  *exactly* that set of groups, so it will drop the `docs` group; re-run
  `uv sync --group docs` when you want the docs tooling back.

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

## The type-check budget

`mypy --strict` and `pyright` both report a small number of errors on this tree.
They are accepted, and they all originate in the same place: libcst's
partially-typed surface, where union shapes (`Name | Tuple | List`,
`BaseExpression`, the typed-visitor base classes) cannot be narrowed by a strict
checker. They are not sloppiness in cleanporter's own logic.

A test in `tests/` pins those counts. **The pinned number is a ceiling, not a
floor:**

> The budget only ever goes down. If you fix one of the accepted errors, lower the
> number in the same commit. Never raise it to accommodate new code.

If your change adds an error, the fix is to type your code correctly — not to
edit the budget upward. If you genuinely believe you have hit a new unavoidable
libcst shape, say so explicitly in the PR description and expect to be asked to
prove it.

Run `uv run pytest -m typecheck` to check yourself against it. Note that
invoking `mypy` or `pyright` directly always exits non-zero here, because both
report the accepted errors — that is expected, and it is why CI and the git
hooks run the budget test rather than the raw tools.

The source of truth for the current numbers is
`tests/test_typecheck_baseline.py`. This document deliberately does not repeat
them, because a number written here would go stale the first time somebody
tightens a signature.

## Documentation and the anti-drift tests

User documentation lives in `docs/` and is built with
[zensical](https://zensical.org/) (configured in `zensical.toml`):

```bash
uv run --group docs zensical serve    # live preview on localhost:8000
uv run --group docs zensical build    # produce the static site
```

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
- [ ] `uv run pytest -m typecheck` passes — and if you fixed an accepted
      error, you lowered the budget in the same commit.
- [ ] `uv run prek run --all-files` is clean.
- [ ] New or changed CLI flags, config keys and finding codes are documented in
      `docs/` (the anti-drift tests will tell you if they are not).
- [ ] New behaviour has a test; a bug fix has a test that fails without the fix.
- [ ] Anything the fixer newly declines to rewrite emits a `CP003` explaining why.
- [ ] Commits follow Conventional Commits.
- [ ] `CHANGELOG.md`'s `## [Unreleased]` section mentions user-visible changes.

CI runs lint, formatting and both blocking type checkers once, and the test suite
on Python 3.12, 3.13 and 3.14. The zuban job is informational and is allowed to
fail.

## Reporting bugs

The most useful bug report for a codemod is a minimal input file, the command you
ran, what cleanporter produced, and what you expected. If `--fix` produced code
that does not run, that is a high-severity bug — say so, and include the
traceback from the *rewritten* code.

## License

By contributing you agree that your contributions are licensed under the MIT
License, as in `LICENSE`.

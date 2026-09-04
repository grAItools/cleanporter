# AGENTS.md

Guidance for coding agents working in this repository. Humans want
`CONTRIBUTING.md`; this file is the short version plus the things an agent will
otherwise get wrong.

## What this is

cleanporter enforces Google Python Style Guide §2.2 (import *modules*, not the
names inside them). It reports `from P import S` where `S` is provably an object
(`CP001`) and, with `--fix`, rewrites the import and every use site
format-preservingly via libCST. Two design contracts are the product; a change
that weakens either is a regression no matter how well it tests:

1. **The resolver never guesses.** Whether `P.S` is a module or an object is
   resolved in layers (first-party filesystem → interpreter probe, in-process
   unless `--python` names another interpreter → undetermined). Anything it
   cannot *prove* is reported as `CP002` and never rewritten. Do not add
   heuristics, "probably a class because it is CapWords", or any fallback that
   picks an answer.
2. **The fixer is all-or-nothing per file.** Every rename in a file must be
   proven safe before anything is touched. One guard hit leaves the file
   byte-identical and emits `CP003` with the reason. Do not add partial-rewrite
   modes or "fix what we can" paths.

Findings: `CP001` violation, `CP002` unresolved, `CP003` skipped-on-purpose.
Exit codes: `0` clean, `1` violations, `2` operational error. Note that
`cli.run` folds `CP003` into the failure count, so a skipped file exits `1`
too -- it is a declined violation, not a note. `CP002` only counts under
`--strict`/`treat_unresolved_as_error`.

## Module map (`src/cleanporter/`)

| File | Role |
| --- | --- |
| `cli.py` | argparse parser, config/flag layering, run loop, exit codes; patch to stdout, findings and warnings to stderr. |
| `config.py` | Loads/validates `[tool.cleanporter]` from the nearest `pyproject.toml`; `Config`, `ConfigError`, default exemptions, `_KNOWN_KEYS`. |
| `discover.py` | Expands path arguments into files: exclude globs, always-skip dirs; an explicitly named path bypasses every filter. |
| `analyze.py` | `FileRecord` (tree with cached units/positions), `iter_units`, `analyze_record`, `build` — drives parsing and `Finding` generation. |
| `firstparty.py` | `ModuleMap`: filesystem-only import-root inference, root ranking/demotion, dotted-name enumeration. No imports, no side effects. |
| `resolver.py` | Layered `is_module()`: first-party map → `_probe` (in-process or subprocess) → `None`. Caches per `(parent, name)`; batches probe pairs. |
| `_probe.py` | Stdlib-only classifier executed inside the *target* interpreter. Imports only the parent package, never the leaf, never objects. |
| `guards.py` | Whole-file safety predicates returning `(line, reason)` hits: string mentions, match capture patterns, `global`/`nonlocal`. |
| `rewrite.py` | The fixer: plan builder + libcst transformer. Scope-aware reference qualification, binding reuse, alias allocation, lazy-annotation rewriting. |
| `_imports.py` | Small `ImportFrom` helpers: `dotted`, `relative_level`, `resolve_parent`, `imported_names`, `is_star`. |
| `_bindings.py` | `top_level_bindings()` via `ast` — what a package `__init__` binds at top level, to detect the submodule/re-export ambiguity. |
| `model.py` | `Kind`, `Status`, `Finding` (with `code` → `CP00x` and `format()`). |

## Commands

Everything runs through `uv`. `uv.lock` is committed; Python floor is 3.12.

```bash
uv sync                                   # dev group
uv run prek install                       # local git hooks (.pre-commit-config.yaml)
uv run pytest                             # tests
uv run ruff check                         # lint (the only linter)
uv run ruff format                        # format (the only formatter, 100 cols)
uv run mypy --strict                      # type check (scope from pyproject.toml)
uv run pyright                            # type check (fetches Node on first run)
uv run prek run --all-files               # every blocking hook: the above four
uv run --group docs zensical serve        # docs preview
uv run --group docs zensical build        # docs build

uv run corpus/run.py                      # rewrite real packages, check they still work
uv run corpus/run.py --skip-install       # ... reusing an installed corpus (~30-45 min)

# Optional, non-blocking third opinion. Note this sync drops the docs group.
# `--all-files` is required: without it prek judges only *staged* files and
# reports "(no files to check) Skipped", which reads exactly like a pass.
uv sync --group zuban && uv run --group zuban prek run --all-files --stage manual zuban
```

## Hard rules

- **The type checkers must stay clean.** `mypy --strict` (over
  `src/cleanporter`) and `pyright` (over `src/cleanporter` and `tests/`, minus
  the fixtures) are the gates: they run as git hooks and in CI, and any
  diagnostic is a regression, not debt. `zuban` reports zero too and should be
  kept that way, but it is a cross-check rather than a gate -- its hook is
  manual-stage and its CI job cannot fail -- so nothing will stop you breaking
  it. Type the new code correctly; **never** land it behind a `# type:
  ignore`, a `cast`, an `Any`, or a loosened setting. There is no budget any
  more: the nine errors one used to cover were narrowing failures and all nine
  were fixable.
- **`.pre-commit-config.yaml` is the single definition of lint and type
  checking.** CI does not re-spell those commands: its lint job is
  `uv run prek run --all-files`. Add a check by adding a hook, not by adding a
  CI step. Scope for all three type checkers comes from `pyproject.toml`
  (`[tool.mypy] files`, `[tool.pyright] include`), not from hook arguments.
- **Run the corpus check before changing the resolver, a guard or the fixer.**
  `uv run corpus/run.py` (see `corpus/README.md`) rewrites a pinned set of real
  third-party packages and then *imports and runs* them. Every safety bug found
  in the fixer so far was found there and not by `tests/` -- they do not fail a
  parse, so the fixer's own re-parse backstop passes them. It is daily in CI,
  not per-PR, so nothing else will catch these for you.
- **The two ruff pins must agree.** Ruff is installed twice and unavoidably:
  `uv run ruff check` uses `uv.lock`'s copy, the git hook (and so CI, which
  runs the hooks) uses the one built from `rev:` in `.pre-commit-config.yaml`.
  `tests/test_toolchain_pins.py` asserts they match. After a
  `uv lock --upgrade` that bumps ruff, bump `rev:` in the same commit.
- **Docs anti-drift tests are real gates.** Tests in `tests/` walk the live
  argument parser and config-key set and fail if a CLI flag, a
  `[tool.cleanporter]` key, or a finding code is missing from the documentation.
  Adding a flag or key means updating `docs/` in the same change.
- **`_probe.py` must stay stdlib-only.** It is executed inside an arbitrary
  target interpreter (`--python`) that will not have libcst installed. Its only
  imports are `importlib`, `importlib.util`, `json`, `sys`, `types`. Do not
  import anything from the rest of the package into it, and do not add a
  dependency to it.
- **Ruff is the sole linter and formatter.** `select = ["ALL"]` with a justified
  ignore list, plus `ruff format`. Never introduce black, isort, flake8, or
  autopep8; never add a bare `# noqa` or a new ruff ignore without a comment
  giving the reason.
- **Conventional Commits** (`feat:`, `fix:`, `docs:`, `test:`, `perf:`,
  `refactor:`, `chore:`). Subject in the imperative, describing the effect.
- **The package complies with its own rule.** `src/` and `tests/` are clean
  under `cleanporter`; keep them that way -- write `from cleanporter import
  model` and `model.Status`, not `from cleanporter.model import Status`. The
  sole exception is the public-API re-export block in `__init__.py`, which the
  string-mention guard blocks and which is deliberately *not* silenced with an
  `exclude`. Do not add one.
- **`libcst` is the only runtime dependency.** Keep it that way; tooling belongs
  in a dependency group.
- Docstrings follow the Google convention (shape enforced; not required on every
  object). Code follows the Google Python Style Guide.

## Gotchas

- **After any `cleanporter --fix` run, re-run the target's test suite.** Guards
  are per file, so a string in *another* file naming a rewritten binding by its
  dotted path (`monkeypatch.setattr("pkg.cli.helper", ...)`, an entry point, an
  `importlib` lookup) is invisible and can go stale. This bit this repo's own
  test suite once. `--fix` prints a note to stderr saying so.
- The mccabe/pylint complexity limits in `pyproject.toml` are ratchets set to the
  current worst case (`rewrite.py::_plan_line`). Do not raise them to land code.
- Guards are settled product decisions with reasoning in their docstrings (prose
  docstrings exempt, doctests not; f-strings rewrite, nested plain strings
  block). Read the docstring before re-litigating one.
- Never lower a guard, widen an exemption, or make the resolver optimistic to
  make a test pass. Add the guard's counterexample test instead.

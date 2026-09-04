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

### Added

- **[pyrefly](https://pyrefly.org/) as a fourth type checker**, and a
  **blocking** one from the start. It runs as a git hook like the rest, reads
  `[tool.pyrefly]` in `pyproject.toml`, and covers `src/cleanporter` *and*
  `tests/` (excluding `tests/fixtures/`).

- **A corpus check** (`corpus/run.py`, `corpus/packages.txt`, and a daily
  `Corpus` workflow). It runs `--fix` over a pinned set of real third-party
  packages, then *imports and executes* the result, comparing every failure
  against the same probe run on the pristine copy. All three fixes below were
  found by it and none by `tests/` — they do not fail a parse, so the fixer's
  own re-parse backstop passed them. See `corpus/README.md`.

### Changed

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

### Fixed

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

    This hole predates the string-guard change below, but was masked by it:
    rewriting `_pytest` with the narrowed guard broke the package at import
    time on `from .exceptions import UsageError as UsageError`, which the old
    guard had been shielding by accident, via unrelated prose mentions of
    `UsageError` elsewhere in the same file.

### Changed

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
  hooks. Scope moved into `pyproject.toml` for all three checkers
  (`[tool.mypy] files`, `[tool.pyright] include`), so no invocation needs a
  path argument. `pyright` now also covers `tests/` (excluding
  `tests/fixtures/`, which is input data rather than project code), which
  turned up two narrowing failures in `tests/test_analyze.py`; `mypy --strict`
  still checks `src/cleanporter` only.

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

[Unreleased]: https://github.com/grAItools/cleanporter/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/grAItools/cleanporter/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/grAItools/cleanporter/releases/tag/v0.2.0

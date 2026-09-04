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

- **A corpus check** (`corpus/run.py`, `corpus/packages.txt`, and a weekly
  `Corpus` workflow). It runs `--fix` over a pinned set of real third-party
  packages, then *imports and executes* the result, comparing every failure
  against the same probe run on the pristine copy. All three fixes below were
  found by it and none by `tests/` — they do not fail a parse, so the fixer's
  own re-parse backstop passed them. See `corpus/README.md`.

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

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

Nothing yet.

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

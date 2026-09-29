<h1>
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/wordmark-dark.svg">
    <img src="docs/assets/wordmark-light.svg" alt="cleanporter" width="330">
  </picture>
</h1>

[![CI](https://github.com/grAItools/cleanporter/actions/workflows/ci.yml/badge.svg)](https://github.com/grAItools/cleanporter/actions/workflows/ci.yml)
[![Docs](https://github.com/grAItools/cleanporter/actions/workflows/docs.yml/badge.svg)](https://graitools.github.io/cleanporter/)
[![Python](https://img.shields.io/badge/python-3.12%20%7C%203.13%20%7C%203.14-blue)](https://github.com/grAItools/cleanporter)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

**cleanporter** enforces section [2.2 (Imports)](https://google.github.io/styleguide/pyguide.html#s2.2-imports)
of the Google Python Style Guide — and fixes what it finds.

> Use `import` statements for packages and modules only, not for individual
> types, classes, or functions.

```python
from collections import OrderedDict     # CP001 - object import
import collections                       # ok -> collections.OrderedDict

from os.path import join                 # CP001 - object import
from os import path                      # ok -> path.join
```

> [!NOTE]
> cleanporter is pre-1.0. The CLI surface and the `[tool.cleanporter]` schema
> may change between minor versions; see [CHANGELOG.md](CHANGELOG.md).

## What makes it different

Lint-only tools (`flake8-import-restrictions`'s IMR241, the
`pylint_google_style_guide_imports_enforcing` plugin) can tell you the import
is wrong. cleanporter also **rewrites** it — the import and every use site,
format-preserving, via [libCST](https://github.com/Instagram/LibCST). Ruff has
an open issue to add this rule natively
([#5841](https://github.com/astral-sh/ruff/issues/5841)); until that lands, and
as a checker only, cleanporter covers both halves.

The hard part is not the rewrite, it is knowing whether `from a.b import C`
imports a *module* or an *object*. That cannot be decided from source text
alone — `C` might be a C-extension submodule, a lazily created module, a
namespace package, or a re-exported class. A wrong guess is a nuisance for a
checker but **emits broken code** for a fixer. So cleanporter resolves in
layers — from the filesystem, then by asking a Python interpreter directly
(your project's own, found from its `.venv` or `$VIRTUAL_ENV`, out of process
when it is not the one running cleanporter; `--python` names another) — and
**never guesses**: anything it cannot prove is reported and left alone.

## Installation

cleanporter is not on PyPI yet. Install it from the repository:

```bash
uv tool install git+https://github.com/grAItools/cleanporter
# or, from a clone:
uv tool install .          # or: pip install .
cleanporter --help
```

Requires Python >= 3.12. The only runtime dependency is
[libcst](https://github.com/Instagram/LibCST).

## Quickstart

```bash
cleanporter src/                 # check; exit code 1 if violations exist
cleanporter --diff src/          # preview the rewrite as a unified diff
cleanporter --fix src/           # rewrite what is provably safe
```

```python
# before                                # after (--fix)
from mypkg.helpers import Widget        from mypkg import helpers
w = Widget()                            w = helpers.Widget()
```

Because stdout carries only the patch, `cleanporter --diff src/ | git apply`
works as-is.

For CI, `--format json`, `--format sarif` (GitHub code scanning) and
`--format github` (pull-request annotations) put a machine-readable report on
stdout instead; see
[Machine-readable output](https://graitools.github.io/cleanporter/usage/#machine-readable-output).

Adopting it on a codebase with a backlog? `cleanporter --write-baseline
cleanporter-baseline.json .` records today's findings, and `baseline =
"cleanporter-baseline.json"` under `[tool.cleanporter]` (or `--baseline`)
then fails only on new ones; `--select` / `--ignore` narrow the report to
chosen codes. See
[Adopting cleanporter on an existing codebase](https://graitools.github.io/cleanporter/usage/#adopting-cleanporter-on-an-existing-codebase).

Some code is off-limits for reasons no analysis can discover — a body a
framework re-parses with its own frontend, a `conftest.py` whose namespace
*is* pytest's fixture registry. Declare those with
[`skip` rules](https://graitools.github.io/cleanporter/configuration/#skip-rules):

```toml
[[tool.cleanporter.skip]]
decorator = 'field_operator|scan_operator|program'
reason = "GT4Py re-parses these bodies; a module-qualified call is a DSLError"
```

## pre-commit

```yaml
repos:
  - repo: https://github.com/grAItools/cleanporter
    rev: vX.Y.Z  # a release tag
    hooks:
      - id: cleanporter        # or cleanporter-fix, to rewrite
```

The hooks run with `--whole-project`: a changed file is judged on the evidence
of a full run over its `pyproject.toml` root, and only the changed files are
reported. See
[pre-commit](https://graitools.github.io/cleanporter/pre-commit/).

## Exit codes

| Code | Meaning |
|-----:|---------|
| 0 | no violations remain |
| 1 | violations found (or left after `--fix`) |
| 2 | operational error: a file that could not be read, decoded, parsed or written (reported with its path; every other file is still processed), or bad config |

## Finding codes

| Code | Status | Meaning |
| --- | --- | --- |
| `CP001` | `VIOLATION` | object imported by name — blocks CI |
| `CP002` | `UNRESOLVED` | could not determine whether the symbol is a module; never rewritten. Also marks a file that was not processed (could not be read, decoded, parsed or written), which always exits `2`, with or without `--strict` |
| `CP003` | `SKIPPED` | structurally a violation that cannot be rewritten safely; like `CP001`, it fails the run. A wildcard import, an explicit or load-bearing re-export, a re-export another file names by its dotted path (`monkeypatch.setattr("pkg.mod.name", ...)`, an entry point), an unprovable replacement and a top-level `from . import X` (unless its import root is declared) are reported in every mode; the rest are the fixer explaining a decision, so they appear under `--fix`/`--diff` |
| `CP004` | `SKIPPED_BY_CONFIG` | matched a [`skip` rule](https://graitools.github.io/cleanporter/configuration/#skip-rules), or an [inline suppression](https://graitools.github.io/cleanporter/configuration/#inline-suppressions) (`# cleanporter: ignore[CP001]`) named its code (the import is analysed, and the finding is replaced) — never rewritten, counted in the summary, printed only under `--show-skipped`, and never part of the exit code |
| `CP005` | `UNUSED_SUPPRESSION` | an inline suppression that suppressed nothing — the finding it named is gone, or the comment is on the wrong line; like `CP001`, it fails the run. Not itself suppressible |

## Dogfooding

cleanporter is run over its own source, and `cleanporter .` exits 0: `src/`
and `tests/` are compliant.

That includes the package's public API, re-exported from
`cleanporter/__init__.py` with `from .engine import Mode, RunResult, run` and
the like. It is not silenced with an `exclude` or a `skip` rule: a
module-level import in a package's `__init__.py` is the package's public
surface, the conventional exception to §2.2, so cleanporter treats it as
compliant everywhere — never reported, never rewritten (see
[A package's `__init__.py`](https://graitools.github.io/cleanporter/configuration/#a-packages-__init__py)).

## Documentation

Full documentation lives at **<https://graitools.github.io/cleanporter/>**:

- [Usage](https://graitools.github.io/cleanporter/usage/) — every flag, the
  finding and exit codes, and CI/`git apply`/sweep workflows.
- [pre-commit](https://graitools.github.io/cleanporter/pre-commit/) — the
  `cleanporter` and `cleanporter-fix` hooks, and why they read the whole tree.
- [Library API](https://graitools.github.io/cleanporter/library/) — a whole
  run from Python: `cleanporter.run`, `RunResult` and `Project`.
- [Configuration](https://graitools.github.io/cleanporter/configuration/) — the
  `[tool.cleanporter]` reference and how CLI flags layer over it.
- [How it works](https://graitools.github.io/cleanporter/how-it-works/) — the
  layered resolver and import-root inference.
- [Fixer safety](https://graitools.github.io/cleanporter/safety/) — the
  all-or-nothing safety model and the known limitations.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, the command reference, and the
project's conventions. `AGENTS.md` carries the same ground rules in the form
coding agents read.

## License

MIT — see [LICENSE](LICENSE).

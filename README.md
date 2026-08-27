<img src="docs/assets/logo-mark.svg" width="80" align="right" alt="">

# cleanporter

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
(out of process when `--python` names a different one) — and **never
guesses**: anything it cannot prove is reported and left alone.

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

## Exit codes

| Code | Meaning |
|-----:|---------|
| 0 | no violations remain |
| 1 | violations found (or left after `--fix`) |
| 2 | operational error (syntax error/undecodable input, bad config) |

## Finding codes

| Code | Status | Meaning |
| --- | --- | --- |
| `CP001` | `VIOLATION` | object imported by name — blocks CI |
| `CP002` | `UNRESOLVED` | could not determine whether the symbol is a module; never rewritten |
| `CP003` | `SKIPPED` | structurally a violation that cannot be rewritten safely — reported in every mode and, like `CP001`, fails the run |

## Dogfooding

cleanporter is run over its own source. `src/` and `tests/` are compliant,
with one deliberate exception: the package's public API is re-exported from
`cleanporter/__init__.py`, so `cleanporter .` reports those re-exports as
`CP001` and exits 1.

That is not an oversight, and it is not silenced with an `exclude`. The
findings are true — the package really does import objects by name there —
and the fixer declines to rewrite the file on its own terms: the names appear
in `__all__` as string literals, so rewriting the imports would leave those
strings naming attributes the module no longer binds. It is a fair
demonstration of both the rule and the guard that keeps the fixer honest.

## Documentation

Full documentation lives at **<https://graitools.github.io/cleanporter/>**:

- [Usage](https://graitools.github.io/cleanporter/usage/) — every flag, the
  finding and exit codes, and CI/`git apply`/sweep workflows.
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

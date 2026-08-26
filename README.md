# cleanporter

**cleanporter** enforces section [2.2 (Imports)](https://google.github.io/styleguide/pyguide.html#s2.2-imports)
of the Google Python Style Guide:

> Use `import` statements for packages and modules only, not for individual
> types, classes, or functions.

It ships two modes:

- **check** (default) reports every `from P import S` where `S` is provably a
  plain object rather than a module/subpackage (`CP001`);
- **fix** (`--fix`) rewrites such imports *conservatively*: the offending alias
  becomes `import P`, and every reference to the old local name is rewritten
  to `P.S`. Anything it cannot prove safe leaves the file untouched.

## Installation

```bash
uv tool install .          # or: pip install .
cleanporter --help
```

Requires Python ≥ 3.12. Depends on [libcst](https://github.com/Instagram/LibCST)
for format-preserving rewrites.

## Quickstart

```bash
cleanporter src/                 # check; exit code 1 if violations exist
cleanporter --fix src/           # rewrite what is provably safe
cleanporter --fix src/mypkg/consumer.py pkg/other.py
```

Example transformation:

```python
# before                                # after (--fix)
from mypkg.helpers import Widget        from mypkg import helpers
w = Widget()                            w = helpers.Widget()
```

The fixer imports the enclosing *package* and qualifies uses through the
module name. When the symbol lives in a package `__init__` (so no submodule
name exists to bind), it falls back to importing the full module path and
using qualified references through that binding — both shapes stay compliant
with §2.2.

Formatting, comments, and blank lines are preserved exactly (libcst round-trips
the source); the only whitespace change is the inserted import statement itself.

## Exit codes

| Code | Meaning                                        |
|-----:|------------------------------------------------|
| 0    | no violations remain                           |
| 1    | violations found (or left after `--fix`)       |
| 2    | operational error (syntax error in input file, bad config) |

## Finding codes

- **CP001** – non-module from-import (violation). Blocks CI.
- **CP002** – could not determine whether the symbol is a module (warning;
  with `treat_unresolved_as_error = true` it also fails the run).
- **CP003** – informational: why `--fix` skipped rewriting a file.

## Configuration

Config is read from `[tool.cleanporter]` in the nearest `pyproject.toml`
(searched upward from the first path argument).

```toml
[tool.cleanporter]
exclude = ["tests/", "src/generated_*.py"]
scope = "all"                    # "all" (default) or "first-party"
autofix_third_party = false      # --fix rewrites only your own modules
runtime_fallback = true          # allow importing parent modules to classify
treat_unresolved_as_error = false
source_roots = []                # [] = auto-discover ("src", repo root…)
```

| Key | Default | Description |
|-----|---------|-------------|
| `exclude` | `[]` | Glob patterns matched against project-relative POSIX paths; literal names match directories too. `.git`, `__pycache__`, venvs, etc. are always skipped. |
| `scope` | `"all"` | Report only first-party violations (`"first-party"`) or everything including stdlib/third-party (`"all"`). |
| `autofix_third_party` | `false` | Permit `--fix` to rewrite imports whose target module lives outside the source roots. |
| `runtime_fallback` | `true` | When static analysis cannot decide, import the parent module and inspect the attribute. Disable for never-execute environments. |
| `treat_unresolved_as_error` | `false` | Count CP002 findings toward the failure exit code. |
| `source_roots` | `[]` | Explicit first-party roots. Empty means auto-discovery of `src/` layouts plus packages/modules at the repo root. |

## How classification works

For each `from P import S` (relative forms anchored against the containing
package):

1. **Static layout** — walk the first-party roots and installed site-packages:
   submodule hit if `S.py`, `S/__init__.py`, a namespace directory, or an
   extension module `S.*.so` exists under `P`.
2. **Static bindings** — parse `P/__init__.py` (or `P.py`) and collect
   top-level name bindings.
   - submodule + binding → ambiguous (lazy-re-export idiom), reported CP002;
   - submodule only → **module**, allowed;
   - binding only → object, CP001;
   - nothing bound → possibly dynamic (`__getattr__`, C extensions).
3. **Runtime fallback** (if enabled) — `importlib.import_module(P)` then
   inspect the attribute; an `AttributeError` falls back to importing
   `P.S` as a submodule (exactly matching Python's own semantics). This is
   how stdlib cases such as `from collections import OrderedDict` (object,
   violation) vs `from os import path` (module, fine) are decided correctly.

Results are cached per `(module, symbol)` and per parsed file.

## Fixer safety model

The fixer rewrites a file **all-or-nothing**: before touching anything it
must prove every rename safe using libcst scope analysis. Each rename applies
only to accesses whose referents resolve *uniquely* to that import binding, so
shadowing inside functions, conditional redefinitions elsewhere, dotted
prefixes, and `as` aliases are handled precisely.

A whole file is skipped (CP003 explains why) when any target has:

- mentions of the local name inside string literals (`__all__`,
  `getattr(..., "Name")`, …) — except lazy string annotations when
  `from __future__ import annotations` is active, which are *renamed along
  with* the code;
- a module-level rebinding of the local name (flow-insensitive scopes make
  such references ambiguous);
- `global`/`nonlocal` declarations naming it;
- placement under `if TYPE_CHECKING:` without future annotations (eagerly
  evaluated annotations would raise `NameError` afterwards);
- a one-liner suite body (`if x: from p import obj`);
- another small statement on the same physical line assigning to the imported
  root name (statement-level reorder hazard).

After rewriting, the result must re-parse; otherwise the original content is
kept untouched and an internal-error note is emitted. In `--fix` mode a
unified diff is printed for every changed file.

## Known limitations

- Wildcard imports (`from x import *`) are ignored by design.
- One-liner-suite imports are flagged but not fixed (see above).
- Type comments (`# type: ...`) are not inspected.
- Imports added by the fixer are placed adjacent to the fixed statement rather
  than re-sorted into import blocks — use isort/Ruff separately for layout.
- Runtime fallback executes project/third-party code at import time (opt out
  via `runtime_fallback = false`).

## Development

```bash
uv sync --dev
uv run pytest
```

Project layout:

```
src/cleanporter/
  cli.py       argparse entry point, exit codes, reporting
  config.py    pyproject.toml ([tool.cleanporter]) loading/validation
  checker.py   ImportFrom scanner + finding generation (shared w/ fixer)
  fixer.py     conservative plan builder + libcst transformer
  resolver.py  hybrid static/runtime module-vs-symbol resolution
tests/         pytest suite (36 tests)
```

## License

MIT

# cleanporter

**cleanporter** enforces section [2.2 (Imports)](https://google.github.io/styleguide/pyguide.html#s2.2-imports)
of the Google Python Style Guide:

> Use `import` statements for packages and modules only, not for individual
> types, classes, or functions.

```python
from collections import OrderedDict      # CP001 - object import
import collections                        # ok -> collections.OrderedDict

from os.path import join                  # CP001 - object import
from os import path                       # ok -> path.join
```

It ships one command with two modes:

- **check** (default) reports every `from P import S` where `S` is provably a
  plain object rather than a module/subpackage (`CP001`);
- **fix** (`--fix`) rewrites such imports *conservatively*: the offending alias
  becomes `import P` (or `from parent import P` when `P` itself is nested),
  and every reference to the old local name is rewritten to `P.S`. Anything it
  cannot prove safe is left untouched and reported instead (`CP003`).

Unlike lint-only tools (`flake8-import-restrictions`'s IMR241, the
`pylint_google_style_guide_imports_enforcing` plugin), cleanporter also
**rewrites** offending imports and every use site, format-preserving, via
[libCST](https://github.com/Instagram/LibCST). Ruff has an open issue to add
this rule natively ([#5841](https://github.com/astral-sh/ruff/issues/5841));
until that lands (as a checker only — Ruff does not fix import-shape
violations), cleanporter covers both check and fix.

## Why a runtime resolver

Deciding whether `from a.b import C` imports a *module* or an *object* cannot
be done reliably from source text alone: `C` might be a C-extension submodule,
a lazily created module, a namespace package, or a re-exported class. A wrong
guess is a nuisance for a checker but *emits broken code* for a fixer.
cleanporter resolves in layers and **never guesses**:

1. **First-party, filesystem** — for modules under the analysis roots,
   classification is decided purely from the source tree: no imports, no side
   effects. This layer also recognizes C-extension submodules (`.so`/`.pyd`),
   PEP 420 namespace packages, and the case where a name is *both* a submodule
   on disk and bound directly in the parent's `__init__.py` (a lazy re-export)
   — that shape cannot be decided statically, so it is reported as ambiguous
   (`CP002`) rather than guessed.
2. **Stdlib / third-party, interpreter probe** — an out-of-process,
   stdlib-only script (`--python`, default: the current interpreter) is asked,
   via `importlib`, whether `parent.name` is a submodule. All names needed for
   a run are batched into a single round trip. Only the *parent* package is
   ever imported; the leaf name and any objects are never imported. Pointing
   `--python` at a different interpreter also keeps cleanporter's own
   dependencies (libCST) out of a target project's venv, and contains any
   native-library crash the probe triggers in a subprocess instead of this
   process.
3. **Undetermined** — when neither layer can decide (e.g. an optional/GPU
   dependency that cannot be imported here), the import is reported (`CP002`)
   and `--fix` leaves it untouched. Never rewritten on a guess.

## Exemptions

`typing`, `typing_extensions`, `collections.abc`, and `__future__` are exempt
by default — the style guide itself blesses importing members of `typing`,
and the others are equally idiomatic (`TYPE_CHECKING`, `Mapping`/`Sequence`,
`from __future__ import annotations`). Extend the allowlist with the
`--exempt` flag or the `exempt_modules` config key; individual names can be
exempted with `exempt_names` regardless of which module they come from.

## Installation

```bash
uv tool install .          # or: pip install .
cleanporter --help
```

Requires Python >= 3.10. Depends on [libcst](https://github.com/Instagram/LibCST)
for format-preserving rewrites (and `tomli` on 3.10, where `tomllib` is not
yet in the standard library).

## Quickstart

```bash
cleanporter src/                 # check; exit code 1 if violations exist
cleanporter --diff src/          # preview the rewrite as a unified diff
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
with §2.2. An existing binding of the same module in the same scope is
reused rather than duplicated, and a colliding module token is aliased
(`helpers_2`) instead of breaking the file.

Formatting, comments, and blank lines are preserved exactly (libcst round-trips
the source); the only structural change is the inserted/replaced import
statement itself.

## Exit codes

| Code | Meaning                                        |
|-----:|------------------------------------------------|
| 0    | no violations remain                           |
| 1    | violations found (or left after `--fix`)       |
| 2    | operational error (syntax error/undecodable input file, bad config) |

## Finding codes

| Code | `Status` | Meaning |
| --- | --- | --- |
| `CP001` | `VIOLATION` | object imported by name — blocks CI. |
| `CP002` | `UNRESOLVED` | could not determine whether the symbol is a module (`--strict`/`treat_unresolved_as_error` also fails the run). |
| `CP003` | `SKIPPED` | structurally a violation, deliberately not rewritten — informational, explains why `--fix`/`--diff` declined a file. |

## Command-line flags

```
cleanporter [--fix] [--diff] [--python PATH] [--exempt MODULE] [--root PATH]
            [--strict] [--version] [paths ...]
```

| Flag | Meaning |
| --- | --- |
| `paths` | Files or directories to process (default: `.`). |
| `--fix` | Rewrite violations in place where provably safe. |
| `--diff` | Show the rewrite as a unified diff without writing (ignored if `--fix` is also given — `--fix` wins and writes). |
| `--python PATH` | Interpreter used to classify stdlib/third-party names. Default: the interpreter running cleanporter. |
| `--exempt MODULE` | Additional module whose members may be imported by name (repeatable). |
| `--root PATH` | Additional first-party import root (repeatable). |
| `--strict` | Also fail (`exit 1`) on imports that could not be classified (`CP002`). |
| `--version` | Print the version and exit. |

## Configuration

Config is read from `[tool.cleanporter]` in the nearest `pyproject.toml`
(searched upward from the first path argument). CLI flags layer on top of
(never replace) config file values: `--exempt` extends `exempt_modules`,
`--root` extends `source_roots`, `--python` overrides `python` only if given,
and `--strict` ORs into `treat_unresolved_as_error`.

```toml
[tool.cleanporter]
exclude = ["tests/", "src/generated_*.py"]
scope = "all"                    # "all" (default) or "first-party"
source_roots = []                # [] = auto-discover from the paths given
treat_unresolved_as_error = false
exempt_modules = ["six.moves"]   # extends, does not replace, the built-in defaults
exempt_names = ["THING"]
python = null                    # interpreter path; null/absent = current
```

| Key | Default | Description |
|-----|---------|-------------|
| `exclude` | `[]` | Glob patterns matched against project-relative POSIX paths; a pattern with no glob metacharacters also matches a directory and everything under it. |
| `scope` | `"all"` | Report only first-party violations (`"first-party"`) or everything including stdlib/third-party (`"all"`). |
| `source_roots` | `[]` | Explicit first-party import roots, relative to the `pyproject.toml` directory. Combined with whatever the analyzed paths themselves imply. |
| `treat_unresolved_as_error` | `false` | Count `CP002` findings toward the failure exit code. |
| `exempt_modules` | `["typing", "typing_extensions", "collections.abc", "__future__"]` | `from MODULE import X` is allowed when `MODULE` (or an ancestor of it) is in this set. Configured values are added to, never replace, the built-in defaults. |
| `exempt_names` | `[]` | Individual bound names that are always allowed, regardless of which module they come from. |
| `python` | `null` (current interpreter) | Interpreter used for the stdlib/third-party probe. |

Always-skipped directories regardless of `exclude`: dot-directories (`.git`,
`.venv`, ...), `__pycache__`, `node_modules`, `build`, `dist`, and
`site-packages`. A path named *explicitly* on the command line bypasses every
filter — pointing cleanporter at an excluded file is taken as deliberate.

## How classification works

For each `from P import S` (relative forms anchored against the containing
package; a relative import that cannot be anchored — e.g. it climbs above the
first-party root — is reported `CP002` rather than guessed at):

1. **First-party filesystem** — is `P` one of the packages/modules under the
   analysis roots? If so, decide from disk alone: a submodule hit is `P/S.py`,
   `P/S/__init__.py`, a namespace-package directory, or an extension module
   `P/S.*.so`/`.pyd`. If `S` is *also* bound as a top-level name in `P`'s
   `__init__.py` (a lazy re-export idiom), the result is ambiguous
   (`CP002`) — never guessed.
2. **Stdlib / third-party interpreter probe** — `importlib.util.find_spec`
   against `P.S` in the target interpreter (only `P` is imported), falling
   back to inspecting the type of the already-loaded attribute `P.S`. This is
   how stdlib cases such as `from collections import OrderedDict` (object,
   violation) vs `from os import path` (module, fine) are decided correctly,
   without ever importing the leaf.
3. **Undetermined** — the parent could not be imported in the target
   interpreter, or the probe otherwise failed. Reported `CP002`, never
   rewritten.

Results are cached per `(module, symbol)` pair; each file's syntax tree is
parsed and walked once per run, not once per finding-generating pass.

## Fixer safety model

The fixer rewrites a file **all-or-nothing**: before touching anything it
must prove every rename safe using libcst scope analysis. Each rename applies
only to accesses whose referents resolve *uniquely* to that import binding, so
shadowing inside functions, conditional redefinitions elsewhere, dotted
prefixes, and `as` aliases are handled precisely. Imports inside function or
class bodies are fixed too, each scope getting its own binding, independent of
any module-level import of the same module.

A whole file is skipped (`CP003` explains why) when any target has:

- a mention of the local name inside a string literal (`__all__`,
  `getattr(..., "Name")`, an f-string, ...) — except a genuine prose
  docstring (module/class/function docstrings containing no `>>>` doctest
  marker are exempt: a stale name in prose is a documentation nit, not broken
  code) and except lazy string annotations under
  `from __future__ import annotations`, which are *rewritten along with* the
  code rather than left as a mention;
- a module-level rebinding of the local name (libcst's scopes are not
  flow-sensitive, so such references are ambiguous between the import and the
  rebinding);
- `global`/`nonlocal` declarations naming it;
- placement under `if TYPE_CHECKING:` without
  `from __future__ import annotations` active (eagerly evaluated annotations
  would raise `NameError` after the import moved); with future annotations
  active, both the import and any lazy string annotation mentioning the name
  are rewritten together;
- a one-liner suite body (`if x: from p import obj`) — reported, not fixed;
- another statement joined onto the same physical line by a semicolon —
  reported, not fixed;
- removing the import line outright would discard a leading or trailing
  comment attached to it (the module is already bound elsewhere and nothing
  else on the line needs to be kept) — rather than silently drop the
  comment, the file is left alone.

After rewriting, the result must re-parse; otherwise the original content is
kept untouched and an internal-error finding is emitted. In `--fix` mode a
unified diff is printed to stdout for every changed file before it is written;
`--diff` prints the same preview without writing anything.

## Known limitations

- Relative imports are rewritten to their **absolute** form
  (`from .sub.mod import C` -> `from pkg.sub import mod` + `mod.C`).
- Imports are not re-sorted; the fixer inserts/replaces a statement in place
  rather than reflowing import blocks — use isort/Ruff separately for layout.
- Wildcard imports (`from x import *`) are reported but never rewritten.
- One-liner suites (`if x: from p import obj`) and statements joined by a
  semicolon onto the same physical line as an import are reported but never
  rewritten.
- A file is blocked outright (not partially fixed) when a rewritten name
  appears in a non-docstring string literal, inside a doctest, or when
  removing an import would discard its comment — see "Fixer safety model".
- Type comments (`# type: ...`) are not inspected.
- `--diff`/the diff portion of `--fix` output is **not** currently
  `git apply`-able: diff headers are built as `f"a/{path}"`/`f"b/{path}"`
  without normalizing `path`, so an absolute path argument produces headers
  like `a//home/you/project/file.py` (a doubled slash `patch`/`git apply`
  will not resolve), and all diffs for a run are concatenated to stdout
  rather than written as separate patch files. This is a known gap, tracked
  for a future fix rather than addressed here.
- `six.moves` is not in the default exemption set, even though the style
  guide mentions it; add it via `--exempt six.moves` or
  `exempt_modules = ["six.moves"]` if you need it.
- Probe results are cached in memory for the run but not persisted to disk;
  a very large third-party surface re-pays the (batched) probe cost on every
  invocation.

## Development

```bash
uv sync --dev
uv run pytest
uv run ruff check src tests
uv run mypy --strict src/cleanporter
```

This project runs `ruff check` (not `ruff format`) as a gate: formatting is
not currently enforced, so `ruff format` may show drift against the existing
source. Do not run `ruff format` on this tree without a dedicated pass — it
would produce a large, unreviewed reformatting diff unrelated to any feature
change.

Project layout:

```
src/cleanporter/
  cli.py         argparse entry point, exit codes, reporting
  config.py      pyproject.toml ([tool.cleanporter]) loading/validation
  discover.py    path expansion, exclusion, always-skip directories
  analyze.py     drives parsing + finding generation, per-file caching
  firstparty.py  filesystem-only first-party module/package enumeration
  resolver.py    layered static/probe module-vs-symbol resolution
  _probe.py      stdlib-only classifier executed inside the target interpreter
  guards.py      whole-file safety predicates (string mentions, global/nonlocal)
  rewrite.py     conservative plan builder + libcst transformer
  _imports.py    small ImportFrom/Import parsing helpers
  _bindings.py   top-level name-binding collection for __init__.py files
  model.py       Finding/Status/Kind shared result types
tests/           pytest suite
```

## License

MIT

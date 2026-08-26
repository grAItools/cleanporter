# modimports

Enforce **Google Python Style Guide §2.2** — *"Use `import` statements for
packages and modules only, not for individual types, classes, or functions."* —
with both a **checker** and an **autofixer**.

```
from collections import OrderedDict      # ✗  object import
import collections                        # ✓  ... OrderedDict -> collections.OrderedDict

from os.path import join                  # ✗  object import
from os import path                       # ✓  ... join -> path.join
```

Unlike the existing lint-only tools (`flake8-import-restrictions` IMR241, the
`pylint_google_style_guide_imports_enforcing` plugin), `modimports` also
**rewrites** offending imports and every use site, and it classifies names by
**asking the real interpreter** instead of guessing statically — so C-extension
submodules, PEP 420 namespace packages, and re-exports are handled correctly.

## Why a runtime resolver

Deciding whether `from a.b import C` imports a *module* or an *object* cannot be
done reliably from source alone (`C` might be a C-extension submodule, a lazily
created module, a namespace package, or a re-exported class). A wrong guess is a
nuisance for a checker but *emits broken code* for a fixer. `modimports` resolves
in layers and **never guesses**:

1. **First-party** (files under the analysis roots): decided from the filesystem
   — no imports, no side effects, correct for namespace packages.
2. **Stdlib / third-party**: `importlib.util.find_spec("parent.name")` /
   attribute inspection in the target interpreter. Only the *parent* package is
   imported (cached); the leaf and any objects are never imported.
3. **Undetermined** (e.g. an optional/GPU dependency that can't be imported on
   this machine): reported by `check`, **skipped** by `fix`.

The probe is pure-stdlib and runs against a target interpreter (`--python`,
default: the current one). Point it at your project's venv and the heavy tool
dependencies (libCST, Typer) never need to be installed next to your scientific
/ GPU stack; a different interpreter is driven in a subprocess, which also
contains native-library crashes.

## Install

```bash
pip install modimports          # or: uv tool install modimports
```

Requires Python ≥ 3.10.

## Usage

```bash
# Report violations (exit 1 if any). Great as a pre-commit / CI gate.
modimports check src/

# Preview the rewrite as a unified diff.
modimports fix src/

# Apply it.
modimports fix src/ --write
```

Useful options (both commands):

| Option | Meaning |
| --- | --- |
| `--python PATH` | Interpreter used to classify stdlib/third-party names. Default: current. |
| `--exempt MODULE` | Additional module whose members may be imported by name (repeatable). |
| `--root PATH` | Extra first-party import root. |
| `--strict` *(check)* | Also exit non-zero on *undetermined* imports. |
| `--write` / `-w` *(fix)* | Apply changes in place instead of printing a diff. |

By default `typing`, `typing_extensions`, `collections.abc`, and `__future__`
are exempt (importing their members is idiomatic and, for `typing`, explicitly
blessed by the guide). Extend with `--exempt`.

### Exit codes & diagnostics

`check` prints `path:line:col: CODE message` and exits non-zero on violations.

| Code | Meaning |
| --- | --- |
| `MI001` | Object imported by name — a violation (auto-fixable). |
| `MI002` | Structurally a violation but **not** auto-fixed (see below). |
| `MI900` | Could not classify (parent not importable in the target interpreter). |

## What gets fixed (and what doesn't)

`from a.b import C[, D]` becomes `from a import b` plus `b.C` / `b.D` at every
**in-scope** use (`from a import C` at top level becomes `import a` + `a.C`).
The fixer:

- reuses an existing module binding when the file already imports that module
  (no duplicate imports);
- keeps compliant names in a mixed statement in place;
- picks a collision-free alias if the module token is already taken;
- qualifies only the *actual* references to the binding — a local variable that
  shadows the name is left alone (scope-aware, via libCST metadata);
- is idempotent.

For safety it **does not** auto-fix (these are reported, not rewritten):

- imports not at module scope (inside functions/classes);
- imports inside an `if TYPE_CHECKING:` block (would risk runtime annotations);
- `from x import *`;
- names it could not classify.

### Known limitations (v1)

- Relative imports are rewritten to their **absolute** module form
  (`from .sub.mod import C` → `from pkg.sub import mod` + `mod.C`).
- The fixer may introduce a duplicate of an already-present import in edge
  cases; run `isort`/`ruff` afterwards to tidy import blocks.
- Ruff's port of this rule (issue #5841) was still open at the time of writing —
  if it has since landed, prefer it for the *check* half and use `modimports`
  for the *fix*.

## Development

```bash
uv sync
uv run pytest
uv run ruff check . && uv run mypy src
```

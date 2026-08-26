# Spec: Merge `cleanporter` and `modimports`

**Date:** 2026-08-26
**Status:** Accepted

## Context

Two independently written tools enforce Google Python Style Guide §2.2
("Use `import` statements for packages and modules only"). Both are the
author's; neither has any git history, published release, or users.

- `cleanporter` (2,639 LOC) — better **fixer safety model**.
- `3rdparty/modimports` (1,341 LOC) — better **classification architecture**,
  **code generation**, and **rule fidelity**.

Measured head-to-head (corpora under `/tmp/bake`, `/tmp/b2`, `/tmp/b4`):

| Case | cleanporter | modimports |
| --- | --- | --- |
| `from typing import Any` | CP001 false positive | exempt (correct) |
| local shadows new binding | emits broken code | aliases `helpers_2` |
| `__all__ = ["THING"]` | blocks (correct) | breaks `__all__` |
| `from os import path` + `from os.path import join` | refuses to fix | reuses `path` |
| trailing `# comment` on import | preserved | dropped |
| first-party `accel.*.so` | module (correct) | breaks it |
| submodule *and* `__init__` binding | reports ambiguous | silently "module" |
| import in a function body | fixes | skips, unreported |
| `sys.exit()` in a package `__init__` | kills the linter | unaffected |
| file containing `elif` | crashes | fine |

Hygiene: mypy `--strict` reports 53 errors on cleanporter, 6 on modimports;
ruff `F,E9,B,SIM` reports 6 and 0.

## Decision

**Take `modimports` as the base; port `cleanporter`'s safety model onto it.**
The merged project keeps the name, CLI, and finding codes of `cleanporter`.

Rationale: the work is asymmetric. Porting modimports *into* cleanporter means
rewriting cleanporter's scanner (~200 of `checker.py`'s 307 lines) and its
plan/executor state machine (~250 of `fixer.py`'s 766) — the two files where
its bugs live. Porting cleanporter *into* modimports is additive: the guards
are standalone predicates, `config.py` transfers nearly verbatim, and the
`ModuleMap` extensions are pure filesystem code. ~450 lines added versus ~500
lines rewritten, and the added lines are far less entangled.

## Requirements

Capabilities the merged tool must have, and where each comes from:

**From modimports (already present — must not regress)**
1. Style-guide exemptions: `typing`, `typing_extensions`, `collections.abc`,
   `__future__`, plus user-supplied.
2. Out-of-process classification against a target interpreter (`--python`),
   batched into one round trip; the leaf is never imported.
3. Filesystem-only first-party classification (no side effects).
4. Existing-module-binding reuse and collision-safe aliasing.
5. Clean multi-line statement replacement (`FlattenSentinel`).
6. Visitor-based traversal (no hand-rolled statement walker).

**From cleanporter (to be ported)**
7. All-or-nothing per-file rewriting with explanatory CP003 findings.
8. Guards: string-literal mentions, `global`/`nonlocal`, module-level
   rebinding of the imported local.
9. Post-rewrite re-parse verification with revert.
10. First-party C-extension submodules (`.so`/`.pyd`) classified as modules.
11. Submodule-and-`__init__`-binding ambiguity reported, never guessed.
12. `[tool.cleanporter]` config in `pyproject.toml`, `exclude` globs,
    always-skip directories.
13. `scope = "first-party"`.
14. argparse CLI, single command with `--fix`, exit codes 0 / 1 / 2.
15. Fixing imports that are not at module scope.
16. Fixing `TYPE_CHECKING`-gated imports when `from __future__ import
    annotations` is active; blocking otherwise.

**New (found during comparison)**
17. Trailing comments on rewritten import lines are preserved.
18. `fix` mode reports violations it declined to fix (modimports drops them).
19. One tree traversal per file, not three.

## Explicitly not doing

- **A static site-packages layer in front of the probe.** Considered and
  rejected: with the probe out-of-process the safety argument is gone, and
  static guessing about third-party layout is *less* accurate than asking the
  interpreter. Profiling shows the probe is not the bottleneck anyway
  (requirement 19 is). If speed later matters, cache probe results to disk.
- **Import sorting.** Both tools punt to isort/ruff; keep punting.
- **Relative imports staying relative.** Both rewrite to absolute. Out of scope.

## Finding codes

| Code | `Status` | Meaning |
| --- | --- | --- |
| CP001 | `VIOLATION` | object imported by name |
| CP002 | `UNRESOLVED` | could not classify |
| CP003 | `SKIPPED` | structurally a violation, deliberately not rewritten |

## Global constraints

- Python >= 3.10 (modimports' floor; cleanporter's was 3.12 — take the lower).
- Runtime dependencies: `libcst>=1.1` only. Typer is dropped in Task 5.
- `src/cleanporter/_probe.py` must remain stdlib-only and import-free of the
  rest of the package; it is executed inside the *target* interpreter.
- The probe must never import the leaf name, only the parent.
- Never guess: an unclassifiable import is reported, never rewritten.

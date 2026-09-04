# The corpus check

`cleanporter --fix` run over a pinned set of real third-party packages, with
the rewritten code then imported and executed to check that nothing moved.

```bash
uv run corpus/run.py                 # install, fix, check  (~30-45 min)
uv run corpus/run.py --keep          # leave both trees for inspection
uv run corpus/run.py --skip-install  # reuse an already-installed corpus
```

Exit `0` when the rewrite changed no observable behaviour, `1` on a
regression, `2` if the harness could not run.

## Why this exists separately from `tests/`

The unit suite checks that the fixer does what it is told on inputs someone
thought of. This checks that it does not break code nobody thought of.

Every safety bug found in the fixer so far was found here, not there:

| Bug | How it showed up |
| --- | --- |
| `from .exceptions import UsageError as UsageError` was rewritten, deleting a public name | `_pytest` stopped importing |
| `import unittest` under `if TYPE_CHECKING:` was reused as a runtime binding | `NameError` in 191 of libCST's test modules |
| Both halves of a re-export chain were rewritten | `libcst.tool.dump` pointed at nothing |

None of those is visible in a diff. None of them fails a parse, and the fixer
re-parses its own output, so its own backstop passed all three. They only
appear when the rewritten code is *imported and run*.

## What it checks

Every check is **differential**: the same probe runs against the pristine copy
and the rewritten one, and only a *new* failure counts. That is what makes it
usable on third-party code, which has its own pre-existing failures — an
optional dependency that is not installed, a test that wants a network, a
platform-specific module. The corpus does not need to be green, only
unchanged.

1. **Every module imports** — each submodule in turn, not just the top-level
   package. This is what catches an attribute a rewrite deleted from under
   some other module.
2. **No new undefined names**, via `ruff --select F821`. Cheap, and it catches
   a reference the fixer failed to qualify.
3. **Bundled test suites still pass**, for packages that ship one inside the
   wheel. The strongest signal by a distance, because it actually executes the
   rewritten code — check 1 cannot see a failure that only happens when a
   function is called.

## The manifest

`packages.txt`, pinned with `==` on purpose. An unpinned corpus makes every run
a moving target, and a regression becomes indistinguishable from an upstream
release. Bump pins deliberately, in their own commit, so a change in corpus
results has exactly one cause.

Packages are chosen for variety of *import style* rather than popularity — one
that only ever writes `import x` exercises nothing here. The set covers heavy
re-export surfaces, `if TYPE_CHECKING:` blocks, deep package nesting,
namespace-ish layouts and plain flat modules.

Adding a package that ships its own tests is worth several that do not; list it
in `BUNDLED_SUITES` in `run.py` to have that suite run.

## In CI

`.github/workflows/corpus.yml`, weekly and on manual dispatch — not on every
push. It installs ~200 MB, rewrites thousands of files and runs libCST's suite
twice, which is the wrong price for feedback on a docs typo. It is also the
only check in this repository that an *upstream* release can break, so keeping
it off the PR path means a red run points at either a real regression or a
deliberate pin bump.

Run it by hand from the Actions tab before merging anything that touches the
resolver, the guards or the fixer.

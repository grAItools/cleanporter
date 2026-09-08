# The corpus check

`cleanporter --fix` run over a pinned set of real third-party packages, with
the rewritten code then imported and executed to check that nothing moved.

```bash
uv run corpus/run.py                 # install, fix, check  (~30-45 min)
uv run corpus/run.py --keep          # leave both trees for inspection
uv run corpus/run.py --skip-install  # reuse an already-installed corpus
uv run corpus/run.py --update        # repin packages.txt to the latest, then stop
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

`--update` repins every package to its latest release and stops there — the
other flags describe the run, so they do not apply. It resolves against the
*floor* of this project's `requires-python` rather than whichever interpreter
you happen to run it on, so a bump cannot pick a version of a *named* package
that the oldest supported Python cannot install. Its dependencies are resolved
by the install that follows, not here, so they can still surprise you; that is
what running the check afterwards is for.

Comments, environment markers and the order of the file are preserved, so the
diff is a column of version numbers and nothing else, and a line that is not a
simple `==` pin is reported rather than quietly skipped. `--update`
deliberately does not go on to run the check: a bump belongs in its own commit,
or a changed report has two possible causes and no way to tell them apart.

## In CI

`.github/workflows/corpus.yml`, daily at 04:17 UTC and on manual dispatch — not
on every push. It installs ~200 MB, rewrites thousands of files and runs
libCST's suite twice, which is the wrong price for feedback on a docs typo. It
is also the only check in this repository that an *upstream* release can break,
so keeping it off the PR path means a red run points at either a real
regression or a deliberate pin bump.

Daily rather than weekly: this is the only check that can catch a fixer bug at
all, and a week-wide window means a regression is several days of commits deep
before anything reports it.

Run it by hand from the Actions tab before merging anything that touches the
resolver, the guards or the fixer.

## The weekly gt4py check

`gt4py_check.py`, run by `.github/workflows/gt4py.yml` on Mondays at 02:23 UTC
and on manual dispatch. Same shape as the corpus — rewrite real code, then run
it, and count only failures that are *new* — pointed at a checkout of
[gt4py](https://github.com/GridTools/gt4py)'s `main` instead of at pinned
wheels.

```bash
uv run corpus/gt4py_check.py /path/to/gt4py          # its .venv must exist
uv run corpus/gt4py_check.py /path/to/gt4py --tests tests/next_tests/unit_tests
```

The two ask opposite questions. The corpus is pinned so that a red run means
*this* repository changed; here the code moves and this repository does not, so
a red run means the fixer has met something new. gt4py earns the separate check
because every unsafe-rewrite class in issue #2 came from it, and because those
bugs produce code that imports and parses — the fixer's own re-parse backstop
passes them, and only running the suite says otherwise.

It writes the `[tool.cleanporter.skip]` rules a gt4py user is expected to write
(a DSL body under `@field_operator` is re-parsed by gt4py's own frontend; a
`conftest.py` namespace *is* pytest's fixture registry) before rewriting
anything.

Three things it refuses to run on, all of them ways a check like this can
report success having proved nothing: an interpreter that imports gt4py from
anywhere but source inside the checkout (an installed copy is not what gets
rewritten), a checkout with local modifications (a previous run's rewrite would
be part of the baseline — re-clone, or `git checkout .`), and a `--fix` that
rewrote nothing under `src/` or the selection (both runs then exercised the
same code). Each exits 2. Without them the run would fail every week for a reason that is not a
defect, which is the fastest way to teach everyone to ignore it. A checkout that
already configures cleanporter is left alone, since its own rules would then be
the ones worth testing.

Weekly rather than daily: this one cannot be bisected against this
repository's history anyway — upstream moved too — so its job is to keep the
answer from ever being more than a week stale.

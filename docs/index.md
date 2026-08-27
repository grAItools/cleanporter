![cleanporter](assets/wordmark-light.svg#only-light){ width=330 }
![cleanporter](assets/wordmark-dark.svg#only-dark){ width=330 }

# cleanporter

**cleanporter** enforces section
[2.2 (Imports)](https://google.github.io/styleguide/pyguide.html#s2.2-imports)
of the Google Python Style Guide:

> Use `import` statements for packages and modules only, not for individual
> types, classes, or functions.

It ships a single command with two modes: a **checker** that reports every
`from PARENT import NAME` where `NAME` is provably a plain object rather than a
module or subpackage, and a **fixer** that rewrites those imports — and every
use site of the name — conservatively and format-preservingly.

## The rule, in code

```python
from collections import OrderedDict  # CP001 - object import
import collections  # ok -> collections.OrderedDict

from os.path import join  # CP001 - object import
from os import path  # ok -> path.join
```

The point of the rule is that a module-qualified reference tells the reader
where a name comes from at the point of use, and it keeps the import block from
becoming a flat namespace of loose identifiers.

## Before and after

Running `cleanporter --fix` on a file turns this:

```python
from mypkg.helpers import Widget

w = Widget()
```

into this:

```python
from mypkg import helpers

w = helpers.Widget()
```

The fixer imports the enclosing *package* and qualifies uses through the module
name. When the symbol is bound in a package's own `__init__.py` (so there is no
submodule name to bind), it imports the full module path instead — `from mypkg
import Widget` becomes `import mypkg` plus `mypkg.Widget` — and both shapes are
compliant with §2.2. An existing binding of the same module in the same scope
is reused rather than duplicated, and a module token that would collide with a
name already in scope is aliased (`helpers_2`) instead of breaking the file.

## What makes it different

Other tools in this space check and stop there:
[`flake8-import-restrictions`](https://pypi.org/project/flake8-import-restrictions/)
has `IMR241`, and there is a
[`pylint_google_style_guide_imports_enforcing`](https://pypi.org/project/pylint-google-style-guide-imports-enforcing/)
plugin. Ruff has an open issue to add the rule natively
([astral-sh/ruff#5841](https://github.com/astral-sh/ruff/issues/5841)); until
that lands — and as a checker only, since Ruff does not fix import-shape
violations — cleanporter covers both halves.

Two things follow from *rewriting* rather than only reporting:

- **It preserves formatting.** Rewrites go through
  [libCST](https://github.com/Instagram/LibCST), a concrete syntax tree, so the
  rest of the file round-trips unchanged; the structural change is the
  inserted or replaced import statement and the qualified references.
- **It refuses to guess.** Deciding whether `from a.b import C` imports a
  module or an object cannot be done reliably from source text alone. A wrong
  guess is a nuisance in a checker and *emits broken code* in a fixer, so
  cleanporter resolves in layers and reports anything it cannot decide instead
  of rewriting it. See [How it works](how-it-works.md).

Everything the fixer cannot prove safe is left exactly as the author wrote it
and reported with a reason. See [Safety and limitations](safety.md).

## Installation

Requires **Python 3.12 or newer**. The only runtime dependency is
[libcst](https://github.com/Instagram/LibCST), used for the format-preserving
rewrites.

cleanporter is a standalone command-line tool, so installing it in isolation
(rather than into your project's virtualenv) is usually what you want — that
also keeps libCST out of the environment you are analysing.

=== "uv"

    ```bash
    uv tool install git+https://github.com/grAItools/cleanporter
    ```

=== "pipx"

    ```bash
    pipx install git+https://github.com/grAItools/cleanporter
    ```

=== "From a checkout"

    ```bash
    uv tool install .          # or: pip install .
    ```

Then:

```bash
cleanporter --help
```

## 60-second quickstart

Check a tree. Nothing is written; the exit code is `1` if anything is
reported.

```bash
cleanporter src/
```

Findings look like this:

```text
src/mypkg/consumer.py:3:0: CP001 imports object 'Widget' from module 'mypkg.helpers'; import the module and use 'helpers.Widget'
```

Preview what the fixer would do, without touching the working tree:

```bash
cleanporter --diff src/
```

Apply it:

```bash
cleanporter --fix src/
```

Or point it at individual files:

```bash
cleanporter --fix src/mypkg/consumer.py pkg/other.py
```

!!! warning "Re-run your tests after a `--fix` sweep"

    cleanporter's safety guards are **per file**. A string in *another* file
    that names a rewritten binding by its dotted path — a
    `monkeypatch.setattr("pkg.cli.helper", ...)`, an entry point, an
    `importlib` lookup — cannot be seen from the file being rewritten, so a
    fix can leave such a reference stale. `--fix` prints a note to stderr
    saying so whenever it writes a file.

## Where to go next

- **[Usage](usage.md)** — every flag, the finding codes, exit codes, and the
  CI / `git apply` / sweep workflows.
- **[Configuration](configuration.md)** — the `[tool.cleanporter]` table, how
  CLI flags layer on top of it, and the default exemptions.
- **[How it works](how-it-works.md)** — the layered resolution model and the
  import-root inference rules.
- **[Safety and limitations](safety.md)** — exactly when the fixer declines to
  touch a file, and what it does not attempt.

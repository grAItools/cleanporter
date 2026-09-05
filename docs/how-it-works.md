# How it works

## Why source text is not enough

The rule cleanporter enforces is a rule about *what a name is*:
`from a.b import C` is fine when `C` is a module or subpackage, and a violation
when `C` is a class, function or constant. Nothing in the statement itself
says which.

Deciding that from source text alone is not merely hard, it is undecidable in
the general case. `C` might be:

- a **C-extension submodule** — `a/b/C.cpython-312-x86_64-linux-gnu.so`, with
  no `.py` file anywhere to grep for;
- a **lazily created module**, installed into `sys.modules` at import time by
  the parent's `__init__`;
- a **PEP 420 namespace package** — a directory with no `__init__.py`, which
  is nonetheless importable;
- a **re-exported class** that a package's `__init__.py` binds under the same
  name as a real submodule on disk, in which case the binding is what you
  actually get.

A heuristic that guesses wrong is an annoyance in a checker: one spurious
warning. In a fixer it is *code that does not run*. So cleanporter is built
around a single commitment: **never guess**. Anything it cannot decide is
reported as `CP002` and left untouched.

## The three layers

For every `from PARENT import NAME` in the analysed files, cleanporter tries
each layer in order and stops at the first that gives an answer.

### 1. First-party, from the filesystem

If `PARENT`'s top-level component is one of the analysis roots, the answer
comes from the source tree alone — nothing is imported, no module-level code
runs, no side effects are possible.

`PARENT.NAME` is a **module** when the tree contains any of:

- `PARENT/NAME.py`
- `PARENT/NAME/__init__.py`
- `PARENT/NAME/` as a PEP 420 namespace package — a directory with no
  `__init__.py` that nonetheless contributes importable submodules or
  subpackages
- an extension module `PARENT/NAME.*.so` or `PARENT/NAME.*.pyd`

If nothing on disk matches, `NAME` is an **object**, and that is a `CP001`
violation.

There is one shape this layer refuses to decide. If `NAME` is *both* a
submodule on disk *and* bound as a top-level name in `PARENT/__init__.py` —
the lazy re-export idiom, where `__init__.py` does `from .NAME import NAME` or
assigns a class of the same name — then the binding wins at import time, and
which one you get cannot be determined without running the code. That is
reported ambiguous (`CP002`), never guessed.

Reading `__init__.py` for those bindings is a parse, not an import:
cleanporter walks the `ast` for the names bound at module level, descending
into `if` / `try` / `for` / `while` / `with` / `match` bodies, and discounts
`from . import NAME` at level 1 without an alias — that binds the submodule
itself, so it is not a shadowing binding. `from PARENT import NAME` written
*inside* `PARENT/__init__.py` is the same statement spelled absolutely and is
discounted with it; django's `db/models/__init__.py` writes it that way, and
reading only the relative form made every consumer of
`django.db.models.signals` unresolvable.

The discount is for the statement, not for the name: a second binding of
`NAME` anywhere else at module level — a reassignment, an import from
somewhere else — is what wins the attribute lookup the import falls back
from, so the pair stays ambiguous.

### 2. Stdlib and third-party, by interpreter probe

Everything else is settled by asking a Python interpreter — the one selected
by `--python`, defaulting to the interpreter running cleanporter.

The question is put to a small, **stdlib-only** classifier module. It imports
only `PARENT`, then asks `importlib.util.find_spec("PARENT.NAME")`: a spec
means `NAME` is a submodule. If there is no spec, it falls back to the type of
the already-loaded attribute `PARENT.NAME` — that is how
`from os import path` (a module bound as an attribute of the non-package
module `os`, so compliant) is separated from `from os import getcwd` (a
function, so a violation).

A spec alone is not the whole answer, though. `from PARENT import NAME` binds
`getattr(PARENT, NAME)` whenever that attribute exists, so a package whose
`__init__` does `from .NAME import NAME` hands out an object even though the
submodule is right there on disk. When the spec is found *and* `PARENT`'s
`__dict__` holds something other than a module under that name, this layer
reports the same ambiguity the filesystem layer does (`CP002`), rather than
calling it a module — the two layers have to agree, because the fixer asks
this question about the import it is *about to write* as well as the one it
read.

It is the package's `__dict__` and not `getattr` on purpose: `getattr` would
run a module-level `__getattr__` (PEP 562), which is the standard hook for
lazy submodules, and that imports the leaf — breaking the property below, for
every name asked about, in the target interpreter. The cost is that a shadow
supplied lazily rather than bound eagerly is not seen; `safety.md` lists it
among the known limits.

Three properties of this layer matter:

- **The leaf is never imported.** Only `PARENT` is. Objects are never imported
  at all, so importing a symbol cannot trigger whatever that symbol's own
  module does on import.
- **The whole run is one round trip.** Every `(PARENT, NAME)` pair needed for
  the run is collected up front and classified in a single batch, with parent
  imports cached across the batch.
- **It can run out of process.** When `--python` points at a *different*
  interpreter, the classifier is executed there as a subprocess, exchanging
  JSON over stdin/stdout. That keeps cleanporter's own dependency (libCST) out
  of the target project's virtualenv, and contains a native-library crash in a
  subprocess rather than taking the whole run down. When `--python` resolves to
  the interpreter cleanporter is already running on — the default case — the
  same stdlib-only code runs in process, since there is nothing to isolate.

The subprocess bridge fails *closed*. A non-zero exit, an interpreter that
cannot be executed, one that hangs past the probe's wall-clock budget, or
output that is not the expected JSON map: every one of those reports the
**entire batch** as undetermined. "Never guess" applies to the transport
exactly as it does to the classification.

### 3. Undetermined

When neither layer can decide — the parent could not be imported here because
it is an optional or GPU dependency, its import raised, the ambiguous
re-export shape above, or a relative import that could not be anchored — the
import is reported `CP002` with the reason, and `--fix` leaves it exactly as
it is.

`CP002` is not a failure by default. Add `--strict` (or
`treat_unresolved_as_error = true`) when you want unresolvable imports to fail
the run.

Results are cached per `(PARENT, NAME)` pair for the duration of a run. Each
file's syntax tree is parsed and walked once, not once per pass. The cache is
in memory only: a very large third-party surface re-pays the (batched) probe
cost on every invocation.

## Relative imports

A relative import has to be turned into an absolute `PARENT` before it can be
classified at all. `from .helpers import Widget` in `mypkg/consumer.py` is
`from mypkg.helpers import Widget`; `from ..util import x` climbs one package
further.

That requires knowing the dotted name of the file doing the importing, which
requires knowing which directory is its import root. If a relative import
climbs above the top-level package — more leading dots than there are package
components to consume — it cannot be anchored, and cleanporter reports `CP002`
rather than picking something plausible.

## Import roots

An *import root* is a directory that would be on `sys.path` for the files
being analysed: `src/` in a src-layout project, the repository root in a flat
one. Getting it wrong produces a dotted name that does not exist at runtime —
and in `--fix` mode that name gets written into the file, which is code that
compiles and then raises `ModuleNotFoundError`.

Roots come from two places:

- **Inferred** from each path you gave, by walking upward while the directory
  above still looks like a package (has an `__init__.py`, or is a namespace
  directory contributing submodules).
- **Declared** by you, via `--root` or `source_roots`.

Roots routinely nest. A `src/` layout that also has `tests/__init__.py` infers
both `src/` and the repository root, and only one of them is really on
`sys.path` for `src/mypkg/consumer.py`. When roots nest, cleanporter says so
as a warning, naming which contains which.

### The ranking rules

When several candidate roots contain the same file, they are ranked in this
order.

**1. The file's own relative-import depth is a floor.** `from ..x import y`
in a file means that file sits at least two packages deep — Python requires
it. Any root that would leave it shallower than that is impossible and is
discarded. This is evidence the directory tree alone does not carry, and it is
what keeps a PEP 420 namespace directory (which has no `__init__.py`, so the
upward walk stops there and infers a root one level too deep) from being
mistaken for a real import root.

**2. A root that another file imports by an absolute name is a package, not a
root.** The canonical PEP 420 layout — an `analytics/` with no `__init__.py`
around a regular `analytics/io/` — defeats rule 1 entirely: the walk infers
`analytics` as a root, and `analytics/io/__init__.py` genuinely can sit one
package deep, so its own relative imports rule nothing out. Nothing *inside*
`analytics` can settle it. A file outside it saying `from analytics.io import
x` can: `analytics` is then a package under some higher root, so it is not a
root itself. Without this rule, `from .readers import read` inside that
`__init__.py` would be rewritten to `from io import readers` — the standard
library. Only *inferred* roots that sit inside another root can be demoted
this way; a declared root never is.

**3. A declared root beats an inferred one.** `--root src` and
`source_roots = ["src"]` are you telling cleanporter the answer, and inferring
past that is never right. The corollary is to declare the directory that is
really on `sys.path`, not one that merely contains it: `--root .` on a src
layout will qualify your package as `src.mypkg`, which is exactly what the
nesting warning is trying to tell you.

**4. Otherwise, the most specific root wins.** The deepest candidate that can
hold the file. That is what keeps `src/mypkg/consumer.py` from being qualified
as `src.mypkg.consumer` when both `src/` and the repository root were
inferred.

If rules 1 and 2 leave nothing at all, the best-ranked candidate is used
anyway — not to guess, but so the import is *reported* as `CP002` rather than
silently vanishing from the run.

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

When nothing on disk matches, the answer comes from what the parent's own
source *binds* `NAME` to — `PARENT/__init__.py` for a package, `PARENT.py` for
a plain module — read with the same rules as the ambiguity check below
(conditional bodies included), and each binding is followed to where it
comes from:

- a `def`, a `class` or an assignment binds an **object**;
- a plain `import X as NAME` binds a **module**;
- `from M import X as NAME` binds whatever `M.X` is, so it is followed
  there. A submodule on disk is a module — `from . import _version as
  version` makes `PARENT.version` a module, and so does `from .sub import
  helpers` when `sub/helpers.py` exists. A first-party `M` is read the same
  way, recursively. A *third-party* `M` is not
  this layer's to answer: `from os import path` in the parent sends
  `os.path` to the interpreter probe (layer 2), in the same single batch as
  everything else, and its answer is the parent's;
- `from M import *` is followed into a first-party `M`, honouring its
  `__all__` when that is a single literal list or tuple of strings, and its
  public names when it has none. A name `__all__` lists but `M` does not bind
  is imported by the star import as the submodule of that name.

A name is an **object** — a `CP001` violation — only when every binding
resolves to an object, and a **module** only when every binding resolves to a
module. A try/except pair that binds an accelerated function or a pure-Python
fallback is an object when both halves are; one that binds a module or
`None` is not decided. If more than one file claims `PARENT` (a stale
`pkg.py` beside `pkg/`), they must all agree.

Missing is not the same as an object. A checkout routinely lacks real
submodules: the `_version.py` setuptools_scm writes at build time, a protobuf
`_pb2`, a Cython module whose `.pyx` is all the tree holds, the sibling
portion of a PEP 420 namespace package that another distribution installs.
Each of those is reported `CP002`, never rewritten, whenever the evidence runs
out:

- `NAME` is neither on disk nor bound in the parent;
- the parent binds it with `from . import NAME` (or `from PARENT import NAME`
  inside its own `__init__`) and the submodule is not on disk, or imports it
  from a first-party module that neither binds it nor has it on disk;
- a binding comes from a first-party module with no source to read — an
  extension module, a namespace package — or from a third-party one the
  probe cannot import;
- the bindings disagree: a module under one, an object under another;
- the parent star-imports from a module this run cannot read (third-party, or
  first-party without source), or from one whose `__all__` is built at run
  time. This holds even when the parent *also* binds `NAME` directly: which
  of the two runs last depends on statement order, which this layer
  deliberately does not reason about;
- nothing binds `NAME` but the parent defines a module-level `__getattr__`
  (PEP 562), which could supply it;
- the parent is a namespace package (no `__init__.py`), an extension module,
  or not on disk at all, so there is no source to show what it binds;
- the answer runs through a **circular import**. `pkg/__init__.py` doing
  `from .m import *` then `ver = None`, while `pkg/m.py` does `from pkg import
  ver`, gives `pkg.m.ver` either `None` or nothing at all depending on which
  module is imported first. Star imports that merely loop back add nothing and
  are followed; but a `from` import, a listed-but-unbound `__all__` name, a
  `__getattr__` fallback or files that must agree, read through a cycle, are
  undetermined — and the verdict is the same whichever file of the run asks
  first;
- the chain of re-exports is more than 64 links deep, or so tangled with
  cycles that following it would take thousands of steps. That ends in a
  finding rather than a crash or a hang. Whether a chain near that limit hits
  it can depend on which other imports the same run looked at first (their
  answers are remembered, and shorten the walk), so such a name can be
  `CP002` in one run and correctly decided in another — but never decided
  wrongly.

The finding names the evidence that was missing, and the first link of the
chain that led there, for example `'pkg._version' is not on disk under this
run's import roots, and 'pkg' binds '_version' by importing its own submodule
of that name`, or `'pkg.helpers' is neither on disk under this run's import
roots nor bound in 'pkg'`. A first-party name that ends up here is *not* handed to the
interpreter probe: that would import first-party code, which is what this
layer exists to avoid. Pointing cleanporter at the whole tree, or declaring
`source_roots`, is what settles a sibling portion it cannot see.

At an import root, the scan that builds this map skips the directories file
discovery skips — dot-directories, `__pycache__`, `build`, `dist`,
`node_modules`, `site-packages` — so a `build/lib/` copy of the package is not
a second first-party tree, and a `build/` or `dist/` at a root does not claim
the name of the PyPI package `build`. *Inside* a package those names are
ordinary: pip has a real `pip/_internal/operations/build/` package, and only
dot-directories and `__pycache__` are skipped there. `exclude` patterns are
deliberately not applied to the scan: they choose which files are analysed,
not which modules exist, and an excluded module is still one other files
import.

There is one shape this layer refuses to decide. If `NAME` is *both* a
submodule on disk *and* bound as a top-level name in `PARENT/__init__.py` —
the lazy re-export idiom, where `__init__.py` does `from .NAME import NAME` or
assigns a class of the same name — then the binding wins at import time, and
which one you get cannot be determined without running the code. That is
reported ambiguous (`CP002`), never guessed. A star import counts as a
binding here too: `from ._impl import *` where `_impl` defines a function
`helpers` shadows `PARENT/helpers.py` exactly as `helpers = ...` would, and a
star import from a module this run cannot read might, so it leaves the
submodule undetermined.

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

Everything else is settled by asking a Python interpreter — the one named by
`--python` (or the `python` key), or else the project's own, detected from
`$UV_PROJECT_ENVIRONMENT` or the `.venv` at the project root (the uv
workspace root, for a workspace member), or else from `$VIRTUAL_ENV`, or else the interpreter running cleanporter (see
[The probe interpreter](configuration.md#the-probe-interpreter)). Detection
only picks *which* environment answers; a pick that lacks a package, or cannot
run, leaves the import `CP002`, so it can never make a verdict more optimistic
than the environment asked can prove.

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
  interpreter, or detection finds one, the classifier is executed there as a subprocess, exchanging
  JSON over stdin/stdout. That keeps cleanporter's own dependency (libCST) out
  of the target project's virtualenv, and contains a native-library crash in a
  subprocess rather than taking the whole run down. When the interpreter is
  cleanporter's own — `--python self`, detection finding nothing, or a named
  or detected interpreter that is the very executable cleanporter is running
  as — the same stdlib-only code runs in process, since there is nothing to
  isolate.

  "The very executable" means the same path, made absolute but with no symlink
  resolved: equal to cleanporter's own `sys.executable`, and containing no `..`
  component. A bare command name (`--python python3`, no path separator) is
  never probed in process: the subprocess finds it on `PATH`, which need not
  lead back to cleanporter's own interpreter even when the current directory
  does. Resolving symlinks would be wrong, because a virtual
  environment's `bin/python` is a symlink to its base Python, and so are every
  other venv's — they would all look like cleanporter's own, and be probed
  against cleanporter's `sys.path` instead of their own. Any other spelling of
  the same interpreter just runs in a subprocess, which costs a process start
  and never changes an answer.

The subprocess bridge fails *closed*. A non-zero exit, an interpreter that
cannot be executed, one that hangs past the probe's wall-clock budget, or
output that carries no framed JSON map (see below): every one of those reports the
**entire batch** as undetermined. "Never guess" applies to the transport
exactly as it does to the classification. It does not fail *silently*,
though: cleanporter prints a warning saying which interpreter failed and how
(its exit status, a timeout, no readable reply), quoting the last few lines
of its stderr, so a batch of `CP002` findings comes with its actual cause.

Importing a parent runs its code, and some packages print on import (a
"Welcome to ..." banner). None of that can reach a patch or corrupt the
probe's reply:

- **In process**, `sys.stdout` points at stderr while the probe runs, so a
  banner is printed on stderr and `--diff`'s stdout stays a clean patch.
- **Out of process**, the probe also points its `sys.stdout` at stderr, and
  its reply is *framed* by markers that are extracted wherever they sit in
  the output. The framing is what copes with output written below Python —
  an extension module's `printf` goes straight to file descriptor 1, and a
  pipe's C-level buffer can flush it after the reply — without the probe
  needing anything beyond the handful of stdlib modules it is allowed.

The in-process swap is of `sys.stdout` only: output an extension module
writes directly to file descriptor 1 during an in-process probe still lands
on cleanporter's stdout; it is a known limit rather than something the
in-process path can close without rewiring the process's own descriptors
under the code it is importing.

### 3. Undetermined

When neither layer can decide — the parent could not be imported here because
it is an optional or GPU dependency, its import raised, the ambiguous
re-export shape above, a first-party name that is neither on disk nor bound in
its parent to something it can follow, or a relative import that could not be
anchored — the
import is reported `CP002` with the reason, and `--fix` leaves it exactly as
it is.

`CP002` is not a failure by default. Add `--strict` (or
`treat_unresolved_as_error = true`) when you want unresolvable imports to fail
the run.

Results are cached per `(PARENT, NAME)` pair for the duration of a run. The
cache is in memory only: a very large third-party surface re-pays the
(batched) probe cost on every invocation.

Each file is parsed once by libcst, and a check walks its syntax tree once: that
single pass collects every import, every `module.attribute` read and every
star import, and everything else the analysis needs is read off what it
collected. Where each import starts is taken from a parse with Python's own
`ast` rather than from libcst's position metadata, which would cost a second
full pass over the tree. That parse is used only where it provably gives
libcst's answer.
A file it cannot vouch for gets the metadata instead: one with `\r` line
endings, one whose grammar the running Python does not accept, or one libcst
does not reproduce exactly. libcst drops a form feed (`\f`) or a backslash
continuation from a statement's leading whitespace, which moves the columns
and lines it reports. Either way, the lines and columns printed are libcst's.
`--fix` and `--diff` add the scope analysis the guards need on top, but only
for a file with at least one `CP001`: a file with none has nothing the fixer
could rewrite or explain, so it costs about what a check does.

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

The absolute name is for classifying only. When `--fix` rewrites a relative
import, the module import it writes is relative too — `from .helpers import
Widget` becomes `from . import helpers` — so it climbs from wherever the file
really is, as the original did, whatever the import root. `from . import C`
imports from the package itself, whose only relative spelling is from its
parent (`from .. import sub` in `pkg/sub/mod.py`). That one does lean on the
root giving the package a parent, but a wrong root makes it fail loudly with
`ImportError` at import time; it cannot bind a different module. Where the
package is top-level there is no relative spelling, and that import is kept
as a `CP003` — see [Known limitations](safety.md#known-limitations).

## Import roots

An *import root* is a directory that would be on `sys.path` for the files
being analysed: `src/` in a src-layout project, the repository root in a flat
one. Getting it wrong produces a dotted name that does not exist at runtime,
or names a different module, and every relative import in the file is
classified under it. `--fix` does not write that name out for a relative
import (see [Relative imports](#relative-imports)); it did once, which
produced code that compiled and then raised `ModuleNotFoundError`, or looked
for the name in the standard library.

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
`__init__.py` is classified as an import from `io.readers`. The fixer still
writes `from . import readers`, which is right either way; before it wrote
relative imports relative, it wrote `from io import readers` — the standard
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

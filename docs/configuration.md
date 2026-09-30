# Configuration

Configuration lives under `[tool.cleanporter]` in your `pyproject.toml`. There
is no separate config file format and no other location.

## Where the config is found

cleanporter takes the **first path argument** of the run, resolves it, and
walks *upward* from there looking for the nearest `pyproject.toml`. The
directory holding that file becomes the *project root*, and every relative
path in the configuration — `exclude` patterns, `source_roots` entries — is
interpreted against it.

If no `pyproject.toml` is found anywhere above the first path argument,
cleanporter runs entirely on defaults, with the project root set to that path
(or its parent, if it is a file).

One run uses one configuration. If you pass several paths and a later one's
nearest `pyproject.toml` is a *different* file — another project, or a nested
one with its own `pyproject.toml` — that path is still analysed, but under the
first path's configuration. cleanporter says so with a warning naming the
configuration in use and each path whose own configuration it is ignoring
(alongside the other warnings: on stdout in check mode, on stderr under
`--diff`, `--fix` or a structured `--format`). Which configuration wins does not change; to apply each
project's own, run cleanporter once per project.

An unknown key inside `[tool.cleanporter]`, a value of the wrong type, or a
`scope` outside the allowed set is a hard error: the run stops and exits `2`
rather than silently ignoring the mistake.

## Full example

```toml
[tool.cleanporter]
exclude = ["tests/", "src/generated_*.py"]
scope = "all"                     # "all" (default) or "first-party"
source_roots = ["src"]            # [] = infer from the paths given
treat_unresolved_as_error = false
exempt_modules = ["six.moves"]    # extends the built-in defaults
exempt_names = ["THING"]
# python = "/path/to/target/venv/bin/python"   # omit (or "auto") = detect; "self" = cleanporter's own
# select = ["CP001", "CP003"]     # omit = every code
ignore = []
# baseline = "cleanporter-baseline.json"   # written by --write-baseline
ruff_aliases = true               # read ruff's flake8-import-conventions aliases as defaults

skip = [
    { decorator = 'field_operator|scan_operator|program', reason = "GT4Py re-parses these bodies" },
    { file = '.*conftest\.py', reason = "pytest collects fixtures from this namespace" },
]

[[tool.cleanporter.alias]]
module = "numpy"
as = "np"
```

## Reference

| Key | Default | Description |
| --- | --- | --- |
| `exclude` | `[]` | Glob patterns (list of strings). Each is matched against the project-relative POSIX path of a candidate file or directory, and also against its absolute POSIX path. A pattern containing no glob metacharacter (`*`, `?`, `[`) additionally matches a directory prefix, so `"tests/"` excludes `tests/` and everything under it. |
| `scope` | `"all"` | `"all"` reports violations everywhere, including stdlib and third-party imports. `"first-party"` reports — and `--fix` rewrites — only imports whose top-level package is one of your own analysis roots. |
| `source_roots` | `[]` | Explicit first-party import roots — directories that are on `sys.path` for the code being analysed — relative to the `pyproject.toml` directory. Combined with, not substituted for, whatever the analysed paths themselves imply. A declared root outranks an inferred one, and, when neither it nor any directory above it up to the project root is a package directory and it does not nest with another root, lets `--fix` rewrite `from . import C` in a top-level package to `import pkg` (see [Known limitations](safety.md#known-limitations)). |
| `treat_unresolved_as_error` | `false` | When `true`, `CP002` (unresolved) findings count toward the failure exit code, so a run that could not classify something exits `1`. |
| `exempt_modules` | `["typing", "typing_extensions", "collections.abc", "__future__"]` | `from MODULE import X` is allowed when `MODULE` — or any ancestor of it — is in this set. Configured values are **added to** the built-in defaults; they never replace them. |
| `exempt_names` | `[]` | Individual bound names that are always allowed, whatever module they came from. Checked before the module is even looked at. |
| `python` | absent (detect the project's interpreter) | The interpreter used for the stdlib/third-party classification probe: a non-empty string. Omit the key, or write `"auto"`, to detect the project's own interpreter; write `"self"` for the interpreter running cleanporter, probed in process — see [The probe interpreter](#the-probe-interpreter). Any other value names an interpreter (an interpreter literally called `auto` or `self` is named with a separator, `"./self"`). A value containing a path separator is a path, and a *relative* one is read against the `pyproject.toml` directory, like every other path here — so `".venv/bin/python"` works from any subdirectory. A value with no separator (`"python3"`) is a command name, looked up on `PATH` as usual. A leading `~` or `~user` is expanded first (an unknown user, or no home directory, is an error). Environment variables are *not* expanded, and symlinks are not resolved. A Windows drive also makes the value a path: `'C:\venv\Scripts\python.exe'` and `'\\server\share\python.exe'` are used as written, while a drive with no root, `'C:python.exe'`, is an error — it is relative to that drive's current directory, which the project root cannot stand in for. Drives are recognised on every platform, so a configuration means the same everywhere — which means a single letter followed by a colon reads as a drive even on Linux and macOS: `'a:b/python'` is drive `a:` with no root, and so an error, not a relative path under a directory called `a:b`. |
| `skip` | `[]` | Regions of your code the tool must not analyse or rewrite, as a list of rule tables. See [`skip` rules](#skip-rules) below. |
| `select` | absent (every code) | The finding codes reported and counted, as a list (`["CP001", "CP003"]`); must name at least one. Reporting only: `--fix` rewrites exactly what it would have without it, and the exit code follows only what is reported (a selected `CP002` still fails the run only under `treat_unresolved_as_error`). An unknown code is an error. Replaced by `--select`. |
| `ignore` | `[]` | Finding codes neither reported nor counted, applied after `select`. Reporting only, like `select`. Replaced by `--ignore`. |
| `alias` | `[]` | Which name a module must be bound under, as an ordered list of rule tables (`[[tool.cleanporter.alias]]`); the first matching rule wins. A binding that breaks its rule is a `CP006`, and every binding `--fix` creates follows the rules. See [`alias` rules](#alias-rules) below. |
| `ruff_aliases` | `true` | Also read `aliases` and `extend-aliases` from `[tool.ruff.lint.flake8-import-conventions]` (or the legacy `[tool.ruff.flake8-import-conventions]`) in the same `pyproject.toml`, as unconditional rules ranked after every `alias` rule. `false` ignores ruff's table entirely, malformed or not. See [Ruff's aliases as defaults](#ruffs-aliases-as-defaults). |
| `baseline` | absent | A baseline file (written by `--write-baseline`) whose findings are left out of the report and the exit code, relative to the `pyproject.toml` directory. The file must exist. Applied by check runs only: under `--fix` or `--diff` it is skipped, with a note. Replaced by `--baseline`. See [Adopting cleanporter on an existing codebase](usage.md#adopting-cleanporter-on-an-existing-codebase). |

!!! tip "`exempt_modules` matches ancestors"

    The default entry `typing` covers `from typing import TYPE_CHECKING`, and
    it also covers anything under it. Adding `"six.moves"` exempts
    `from six.moves import urllib` as well as `from six.moves.urllib.parse
    import urlencode`, because `six.moves` is an ancestor of the latter.

## Default exemptions

Four modules are exempt out of the box:

| Module | Why |
| --- | --- |
| `typing` | The Google style guide explicitly blesses importing members of `typing` — `from typing import TYPE_CHECKING`, `Any`, `cast`. |
| `typing_extensions` | The backport of the same surface; treating it differently from `typing` would be arbitrary. |
| `collections.abc` | `from collections.abc import Mapping, Sequence` is the idiomatic spelling; `collections.abc.Sequence` at every use site is noise. |
| `__future__` | `from __future__ import annotations` is the *only* legal spelling — there is no compliant alternative to rewrite it into. |

These are built in and cannot be switched off through configuration.
`exempt_modules` and `--exempt` only ever add to them.

Note that `six.moves` is **not** exempt by default, even though the style
guide mentions it. Add it explicitly if your codebase needs it.

### A package's `__init__.py`

A module-level `from P import S` in a package's `__init__.py` is never
reported and never rewritten when `S` is an object (a module is compliant
anyway, and a name the resolver cannot classify is still the `CP002` it
would be anywhere). Whatever such an import binds
is an attribute of the package, and a package's attributes are what its users
import: `from ._version_info import VersionInfo` in attrs' `attr/__init__.py`
is what makes `attr.VersionInfo` exist. Rewriting it deletes public API, and
nothing in your run need read that name, list it in `__all__` or spell it in a
string, so no evidence could show the rewrite was unsafe. The re-export is
the conventional exception to §2.2, so it is treated as compliant rather than
as a declined violation: it adds no finding, `CP004` included, and does not
count towards the exit code.

"Module level" means outside every `def` and `class` body, so an import under
a module-level `try`, `if` or `with` counts — it binds a package attribute all
the same. An import inside a function in `__init__.py` binds a local (one
in a class body, a class attribute), and is reported and fixed like any
other; one a `global` statement makes a module global is declined by the
fixer's `global`/`nonlocal` guard, as anywhere. The rule is about the file name, not the
directory: `__main__.py` and every other module in the package are checked as
usual, and a namespace package has no `__init__.py` to exempt. Like the
modules above, it is built in and cannot be switched off.

## The probe interpreter

Stdlib and third-party names are classified by asking a Python interpreter
(see [How it works](how-it-works.md#2-stdlib-and-third-party-by-interpreter-probe)),
and the answer can only be as good as that interpreter's environment: one that
does not have your project's dependencies installed calls every third-party
import unresolvable (`CP002`). cleanporter is usually installed on its own —
`pipx install cleanporter`, `uv tool install cleanporter` — so the interpreter
running it is exactly such a one.

So when neither `--python` nor the `python` key names an interpreter (or either
says `"auto"`), cleanporter looks for the **project's** interpreter. The order
is uv's — the project's own environment first, an activated one only when the
project has none — so a shell that still has some other environment activated
does not decide what your project is checked against:

1. `$UV_PROJECT_ENVIRONMENT` — where uv keeps the project environment when told
   not to use `.venv`; a relative value is read against the environment root
   (below);
2. `.venv` in the environment root;
3. `$VIRTUAL_ENV` — an activated virtual environment.

The *environment root* is where uv would put the project's environment: the
project root, unless the project is a **uv workspace member**, in which case
it is the workspace root — and the member's own directory is not tried, as uv
does not use it. Membership follows uv: the *first* directory above the
project root that holds a `pyproject.toml` decides. If that file has a
`[tool.uv.workspace]` table whose `members` globs match the project root and
whose `exclude` globs do not, it is the workspace root; if it is a plain
project (an `examples/demo` project inside a package), a workspace the project
is not a member of, or a file that cannot be read, there is no workspace, and
nothing further up is looked at. A project root that itself declares
`[tool.uv.workspace]` is its own workspace root, even when an outer
workspace's `members` would match it. Globs are matched as uv matches them,
component by component and literally: `"./pkgs/*"` and `"pkgs/*/"` include
nothing.

The *project root* is the directory of the `pyproject.toml` in use. **With no
`pyproject.toml` above the first path argument, it is that path's directory**
(see [Where the config is found](#where-the-config-is-found)), so detection
looks for a `.venv` there — which, for `cleanporter src/` in a project without
a `pyproject.toml`, is `src/.venv`, not the one beside it. In that layout, or
whenever your environment lives somewhere detection does not look, name it:
`--python .venv/bin/python`, or `python = ".venv/bin/python"`.

The first candidate whose `bin/python` (`Scripts\python.exe` on Windows) is an
executable file wins; a candidate that does not exist, or cannot be run, is
passed over without a word — detection is a search, not a request. Nothing
else is tried: no `uv` subprocess, no `PATH` search, **no conda environment**
(`$CONDA_PREFIX` is not read; name a conda interpreter with `--python`), and
no walk up the directory tree beyond the first `pyproject.toml` above the
project root. If nothing is
found, the interpreter running cleanporter is used, in process, as before.

When the environment found *is* the one running cleanporter — its interpreter
is cleanporter's own `sys.executable`, or its directory is cleanporter's
`sys.prefix` and that is a virtual environment, as when you `uv run
cleanporter` or `uv run python -m cleanporter` from the project's own
environment — the probe runs in process and nothing is printed, unless an
active `$VIRTUAL_ENV` names a *different* directory: then a note says the
project's environment was used rather than it. (`uv run` sets `$VIRTUAL_ENV`
to the project's own, so it stays silent.) Anything else is probed in a
subprocess, exactly as if it had been passed with `--python`, and cleanporter
says which one it picked, and why, in one line on stderr — naming the active
`$VIRTUAL_ENV` too when the project's own environment was preferred over it:

```text
cleanporter: note: classifying stdlib and third-party imports with /work/proj/.venv/bin/python, found from the .venv in the project root /work/proj; set python = 'self' (--python self) to use cleanporter's own interpreter
```

Detection chooses *which* environment is asked; it never supplies an answer
the probe did not prove. An interpreter that lacks a package reports it not
importable (`CP002`), and one that cannot be run at all fails its whole batch
with a warning (`CP002` again), so a wrong pick can only leave imports
unresolved — never produce a `CP001` the environment it asked could not back,
and never a rewrite. `python = "self"` (or `--python self`) turns detection off.

## How CLI flags layer on top of config

Most flags do not replace configured values: they extend or strengthen them. This means a developer can tighten a run locally without
having to restate what the project already declares.

| Flag | Effect on the loaded config |
| --- | --- |
| `--exempt MODULE` | Added to `exempt_modules` (which already contains the built-in defaults). |
| `--root PATH` | Appended to `source_roots`. Relative values resolve against the project root, i.e. the `pyproject.toml` directory. |
| `--strict` | OR-ed into `treat_unresolved_as_error`. `--strict` can turn it on; it can never turn it off. |
| `--select CODES`, `--ignore CODES` | **Override** `select` and `ignore`, each only when given: `--select` replaces the configured `select` while the configured `ignore` still applies, until `--ignore` replaces that too. |
| `--baseline FILE` | **Overrides** the `baseline` key. A relative value is read against the current directory. |
| `--python PATH` | **Overrides** the `python` key, but only when the flag is actually given — so `--python auto` restores detection over a configured interpreter, and `--python self` turns it off. Like any path on the command line, a relative value is read against the current directory, not the project root. An empty value (`--python ""`) is an error (exit `2`). |

There is no flag that removes an exemption, drops a source root, or relaxes
`treat_unresolved_as_error` back to `false`. If you need that, change the
config file.

## What gets scanned

### Always-skipped directories

When cleanporter *walks* a directory you gave it, these are never descended
into, regardless of `exclude`:

- any directory whose name starts with a dot — `.git`, `.venv`, `.tox`,
  `.mypy_cache`, and so on
- `__pycache__`
- `node_modules`
- `build`
- `dist`
- `site-packages`

Within the directories it does walk, cleanporter picks up files with a `.py`
suffix.

The same list applies, *at an import root only*, to the scan that decides
what is first-party (see
[How it works](how-it-works.md#1-first-party-from-the-filesystem)), so a
`build/` or `dist/` at a root is not mistaken for a first-party package. Below
a root those are ordinary package names — pip has a real
`pip/_internal/operations/build/` — and are scanned. `exclude` is *not*
applied to that scan: it chooses which files are analysed, and an excluded
module still exists for the files that import it.

### An explicitly named path bypasses every filter

Naming a file on the command line is taken as deliberate. Such a path skips
the `exclude` patterns and the always-skipped list entirely:

```bash
# `exclude = ["tests/"]` in pyproject.toml -- but this still runs:
cleanporter tests/test_thing.py

# so does this, despite `.venv` being an always-skipped directory name:
cleanporter .venv/lib/python3.12/site-packages/somepkg/mod.py
```

This only applies to paths that name a **file**. A *directory* on the command
line is walked, and the walk applies both filters normally.

## Scope: `"all"` versus `"first-party"`

With the default `scope = "all"`, `from collections import OrderedDict` in
your code is a `CP001` just like `from mypkg.helpers import Widget` is.

With `scope = "first-party"`, only imports whose top-level package is one of
your analysis roots are considered. A stdlib or third-party import is passed
over without being classified: it is not reported, and the interpreter probe
never imports its package to ask about it. This is a useful staging step on a
large legacy codebase: fix your own modules first, then widen to `"all"`.

One third-party name is still classified: one a first-party module re-exports
(`from os import path` in your package's `__init__.py`). An import of it *from
your package* is first-party, and whether it names a module depends on what
`os.path` is.

The scope governs `--fix` and `--diff` exactly as it governs checking: both
modes make one decision per import, so an import that is not reported is never
rewritten.

The first-party test looks at the **top-level component only**. If `mypkg` is
first-party then `mypkg.anything.at.all` is treated as first-party, whether or
not that dotted path exists. This fails safe — a name that does not exist is
still reported when it cannot be resolved; it is just never mistaken for a
third-party import.

## Excluding files

`exclude` patterns are ordinary `fnmatch` globs, matched against the path as
seen from the project root:

```toml
[tool.cleanporter]
exclude = [
  "tests/",                # directory prefix: no metacharacters, so it and
                           # everything under it is excluded
  "src/generated_*.py",    # a glob, matched against the relative path
  "docs/examples/*",
]
```

Three details worth knowing:

- A pattern with no glob metacharacter (`*`, `?`, `[`) matches a *directory
  prefix* as well as an exact path — that is what makes `"tests/"` exclude the
  whole tree. A pattern that does contain one is matched only as a glob.
- The globs are `fnmatch` patterns, not shell or `pathlib` ones, so `*`
  matches `/` too. `"docs/examples/*"` therefore covers nested files, and a
  pattern like `"tests*"` is broader than it looks.
- Each pattern is tried against the project-relative path first and against
  the absolute POSIX path second, so an absolute pattern also works.


## `skip` rules

Some bindings are load-bearing for a consumer that no analysis of the file can
see. A function body under `@gtx.field_operator` is re-parsed by GT4Py's own
frontend, which rejects a module-qualified call outright. A `conftest.py`
namespace *is* pytest's fixture registry. In both cases the resolver is right,
the rewrite is legal Python, and the result does not run — and the only thing
that knows is you. `skip` is where you say so.

```toml
[tool.cleanporter]
skip = [
    { decorator = 'field_operator|scan_operator|program' },
    { file = '.*conftest\.py' },
    { file = 'src/legacy/.*', symbol = 'load_.*' },
]
```

TOML's array-of-tables spelling is identical in effect, and reads better when a
rule carries a `reason`:

```toml
[[tool.cleanporter.skip]]
decorator = 'field_operator|scan_operator|program'
reason = "GT4Py re-parses these bodies; a module-qualified call is a DSLError"
```

Use TOML *literal* strings (single quotes) for patterns. A regex is full of
backslashes and a literal string passes them through untouched, so
`'.*conftest\.py'` means what it looks like — in a double-quoted string you
would have to write `".*conftest\\.py"`.

### Keys

Within one table every key must match **the same definition** (AND). Across the
list, any rule matching is enough (OR).

| Key | Selects | Matched against |
| --- | --- | --- |
| `file` | A whole file when it is the only matcher; otherwise narrows the rest of the rule to that file | The path relative to the project root, POSIX-spelled. A file outside the root has no relative spelling, so it offers its absolute path instead. |
| `function` | A `def`/`async def` that is **not** directly inside a class body | The bare name; the qualified name within the module (`outer.inner`); the `module:qualname` address (`pkg.mod:outer.inner`) |
| `method` | A `def`/`async def` directly inside a class body | as above (`Cache.get`, `pkg.mod:Cache.get`) |
| `class` | A `class` statement | as above |
| `symbol` | Any of the three above | as above |
| `decorator` | Any definition carrying a matching decorator | The decorator as a dotted name with any call stripped (`@gtx.program(backend=...)` → `gtx.program`), and that name's last component (`program`) |
| `reason` | *Not a matcher.* Free text, echoed in the `CP004` finding | — |

Every pattern is matched with **`re.fullmatch`** against each candidate for its
key; the key matches if any candidate does. Two things follow:

- `{ decorator = 'field_operator' }` covers `@field_operator`,
  `@gtx.field_operator` and `@gt4py.next.field_operator` alike, because the
  last component is one of the candidates. Write
  `{ decorator = 'gtx\.field_operator' }` to pin one spelling.
- `{ method = 'Cache\.get' }` needs no second key: the qualified name is
  already a candidate. Nesting goes in the pattern.

The `module:qualname` candidate is the spelling
[`pkgutil.resolve_name`](https://docs.python.org/3/library/pkgutil.html#pkgutil.resolve_name)
takes, and it is the precise way to name one symbol when a filename repeats
across a package tree.

Setting more than one of `function` / `method` / `class` / `symbol` in a single
table is an **error**: they select mutually exclusive node kinds, so the rule
could never match, and a rule that silently never fires is the worst thing this
feature could do. So are an unknown key, a non-string value, an uncompilable
pattern, and an empty table `{}` — which constrains nothing and would therefore
take your entire project.

### What a rule skips

A `file`-only rule takes the whole file. Any other rule takes, per matching
definition, everything from its **first decorator line** through its last line,
nested definitions included.

Two things are then skipped, and the second is what makes the feature work:

1. an import **inside** a skipped region, and
2. any binding whose name appears anywhere inside a skipped region — *pinned*,
   wherever its import statement actually sits.

Pinning is the point. It is what keeps a module-level
`from gt4py.next import broadcast` intact when the only use is inside a field
operator two hundred lines below; without it the import would be rewritten and
the skipped region left holding a name that no longer exists.

The pin test asks only whether the identifier *appears* inside a skipped
region, not whether it resolves to that import — so it pins more than you might
expect, all of it deliberately:

- a local variable, parameter or keyword argument of the same name;
- the leaf of an attribute access — `mod.go()` inside a region pins `go`;
- a name a **string** in the region could be referring to, read the same way
  the [string guard](safety.md#a-reference-to-the-local-name-inside-a-string-literal)
  reads one. This is what keeps a lazy annotation safe: under `from __future__
  import annotations`, `def op(a: "Field")` is the region's only mention of
  `Field`, and it is not an identifier node at all.

Over-pinning can only ever cost you a rewrite, never cause a wrong one. But a
`decorator` rule over a large body does swallow a lot — check what with
`--show-skipped`.

The rest of the file is untouched by all this. A module with fifteen field
operators still gets its plain-Python helpers rewritten.

### `skip` is not `exclude`

They read alike and do different jobs.

| | `exclude` | `skip` |
| --- | --- | --- |
| Syntax | fnmatch globs | regular expressions |
| Stage | **Discovery** — the file is never read | **Analysis** — the file is read and parsed |
| Evidence | Contributes none | Still contributes re-export and package evidence |
| Granularity | Whole files | Whole files, or definitions inside them |
| Reported as | Nothing at all | `CP004` |

That evidence difference is why `skip` is the right tool for *your own code you
do not want touched*. Drop a `conftest.py` at discovery with `exclude` and it
stops counting as an importer of the fixtures it pulls in — which can unblock
an unsafe rewrite in the file those fixtures live in. `skip` keeps reading it
and only suppresses the findings.

Use `exclude` for code that is not yours to fix: vendored trees, generated
files, build output.

## Inline suppressions

A `skip` rule is for a *kind* of code. For one import you have looked at and
decided to keep, put the decision on the import itself:

```python
from gt4py.next import broadcast  # cleanporter: ignore[CP001]
from vendored_thing import handle  # cleanporter: ignore[CP001, CP002]
```

The finding the comment names is reported as `CP004` instead — counted in the
summary, printed only under `--show-skipped`, never part of the exit code —
with a message saying which code was suppressed and by the comment on which
line. `--fix` keeps a suppressed import exactly as it keeps one a `skip` rule
covers: the name is not rewritten, nor is any use of it, and the rest of the
file is fixed as usual. The import is still analysed — a suppression
replaces the finding only once it exists, so the resolver (and the probe) are
asked about it as about any other — but the suppression never feeds back into
the verdict: it cannot turn an unresolved name into an answer, and it changes
nothing about any other import.

### Syntax

The comment is split at each `#`, so a suppression can share a line with
other tools' directives (`# noqa: F401  # cleanporter: ignore[CP001]`). Each
piece is, in order:

1. optional whitespace after the `#`;
2. `cleanporter:` — exactly, lowercase, with no space before the colon;
3. optional whitespace, then `ignore[`;
4. one or more codes, separated by commas, each with optional whitespace
   around it; each code is `CP001`, `CP002`, `CP003` or `CP006`, in capitals;
5. `]`;
6. nothing more, or whitespace followed by any text — a reason, say.

So these are accepted:

```python
# cleanporter: ignore[CP001]
#cleanporter:ignore[CP001,CP002]
# cleanporter: ignore[ CP002 ]  -- the vendored copy is not importable
```

A piece that starts with `cleanporter` and a colon in any other way — in any
case, with any spacing before the colon — is **malformed**. A malformed comment
suppresses nothing: cleanporter prints a warning naming its file and line and
goes on, and the finding it meant to suppress is reported as usual. Malformed
are, for example:

- `# cleanporter: ignore` with no codes — a bare ignore would silence findings
  nobody has seen yet, which is why the rule exists — and `ignore[]`;
- a code that is not one of the four: an unknown one (`CP009`), a lowercase
  one (`cp001`), `CP004` (already your own decision) or `CP005` (the report
  that a suppression did nothing);
- a near miss: `# Cleanporter: ignore[CP001]`, `# cleanporter : ignore[CP001]`,
  `# cleanporter: IGNORE[CP001]`, `# cleanporter: ignore [CP001]`,
  `# cleanporter: ignore[CP001]: reason` (no whitespace before the reason),
  `# cleanporter: noqa`.

One malformed piece voids the whole comment, including a well-formed piece
beside it.

A never-read import is `CP001` to a plain check and `CP003` under `--fix` (the
fixer knows nothing reads it). It is suppressed by `ignore[CP001]`, the code a
check reports, so one comment works in both modes. `ignore[CP003]` on it
suppresses nothing in either mode — a check cannot know the name is never read,
so accepting it under `--fix` alone would make the two modes disagree — and is
reported `CP005`, whose message under `--fix` points to `CP001`.

### Which imports a comment covers

Attachment is by **physical line**. A suppression comment covers

1. every name imported by a `from` or `import` statement that **starts** on
   its line, and
2. every imported name **written** on its line.

```python
from pkg.shapes import Circle, Square  # cleanporter: ignore[CP001]  <- both names

from pkg.shapes import (  # cleanporter: ignore[CP001]              <- every name below
    Circle,
    Square,
)

from pkg.shapes import (
    Circle,  # cleanporter: ignore[CP001]                            <- Circle only
    Square,
)
```

A comment on a line of its own, on the closing `)`, or on anything but an
import covers nothing. A plain `import` statement can only have a `CP006` to
suppress (`import numpy as npy  # cleanporter: ignore[CP006]`). It applies only to findings about an imported
name: a file-level `CP003` (the fixer declining a whole file) cannot be
suppressed.

!!! warning "A per-name comment inside a statement `--fix` rewrites"

    `--fix` never discards a comment. A comment *inside* a parenthesised
    import cannot survive a rewrite of that statement, so when another name
    in it is a `CP001`, the file is declined
    ([`CP003`](safety.md#a-comment-inside-the-import-statement)). Give the
    suppressed name a statement of its own.

!!! warning "`--fix` never moves a suppression onto other imports"

    When `--fix` rewrites some names of a statement and keeps others, the kept
    names are written on one line, which takes the statement's trailing
    comment. A comment that covered only a rewritten name, or nothing at all
    (one on a closing `)`), would then cover the kept names and start
    suppressing them. cleanporter recomputes which imports every suppression
    covers on its own output, and when what any comment covers changes —
    beyond losing the names the rewrite takes away: a kept name, a second
    import of the same name, or the module import just written — it declines
    the whole file (`CP003`: *the rewrite would move this
    suppression comment onto imports it does not cover now*). Give the
    suppressed names a statement of their own, or delete a stale comment.

### Unused suppressions: `CP005`

A code in a suppression that matches no finding on the names the comment
covers is reported as `CP005`, at the comment, and it makes the run exit `1`
like a `CP001`: a stale suppression would otherwise silently swallow the next
finding of that code to land on its line. It appears when the import was fixed
or became compliant, and when the comment is on a line that covers nothing.
Two cases are exempt, because a `skip` rule has already replaced whatever the
comment could have matched: a comment on a line a rule covers (every line of a
file a rule takes whole), and a comment covering an import a rule has already
reported as `CP004`. Nothing else is: a comment on an import a rule pins, but
whose name is compliant or exempt, is still `CP005`.

That includes a package's public surface. A module-level import of an
object in a package's `__init__.py` is compliant (see
[A package's `__init__.py`](#a-packages-__init__py)), so it has no finding
for a comment to suppress, and `# cleanporter: ignore[CP001]` on it is a
`CP005` asking you to delete the comment: the import is kept without it. The
rule covers only a name proven to be an object: a module-level `CP002` (a name
the resolver cannot classify) or a wildcard import's `CP003` there is still a
finding, and a comment naming its code suppresses it as anywhere else. The
public surface is decided before any comment is read, so the comment cannot
change it. A suppression on an import *inside a function* in `__init__.py`
works as it does in any module, since that import is reported and fixed like
any other.

## `alias` rules

A project that writes `import numpy as np` everywhere can say so, and
cleanporter will hold every file to it:

```toml
[[tool.cleanporter.alias]]          # an ordered list: the first matching rule wins
module   = "gt4py.next"             # required: a dotted name or pattern
as       = false                    # required: a name, a {leaf} template, or false
importer = 'gt4py\.next(\..*)?'     # optional: the importing module's dotted name
file     = 'src/.*'                 # optional: the importing file's path
reason   = "the package's own code imports itself by name"

[[tool.cleanporter.alias]]
module = "gt4py.next.*"
as     = "gtx_{leaf}"

[[tool.cleanporter.alias]]
module = "gt4py.next"
as     = "gtx"
```

Two things follow from a rule:

1. **Checking.** A binding of the module under any other name is reported as
   `CP006`, and fails the run like a `CP001`. It is report-only: `--fix` does
   not rename a binding you wrote.
2. **Fixing.** Every binding `--fix` *creates* is named by the rule:
   `from numpy import array` becomes `import numpy as np`, and `from
   gt4py.next.ffront import field_operator` becomes `from gt4py.next import
   ffront as gtx_ffront`. A relative import stays relative (`from . import
   helpers as h`). An existing binding of the module is reused whatever it is
   called — it is already reported, if it breaks the rule.

### Keys

| Key | Required | Meaning |
| --- | --- | --- |
| `module` | yes | The module the rule is about: dotted components, where a component `*` matches exactly one component and `**` one or more. `numpy` matches `numpy` only; `gt4py.next.*` matches `gt4py.next.ffront` but neither `gt4py.next` nor `gt4py.next.ffront.decorator`; `gt4py.**` matches both of those. There is no other wildcard: `num*` is an error. |
| `as` | yes | The name: an identifier (`"np"`); a template in which `{leaf}` stands for the last component of the matched module (`"gtx_{leaf}"`); or `false`, the module's **own name** — `import M` for a top-level module, `from P import L` for a submodule (a binding named after its leaf). Any other `{...}` field, a conversion or format spec, a stray brace, or a result that is not an identifier, is a keyword or is `__debug__`, is an error. A template can still render such a name for one particular module (`"i{leaf}"` for a module `f` gives `if`): every binding of that module is then reported `CP006`, with a message naming the rendered name, and `--fix` declines any file that would need it (`CP003`). |
| `importer` | no | A regex, `re.fullmatch`ed against the importing file's dotted module name (`gt4py.next.ffront.decorator`). A file whose module name cannot be determined never matches it. |
| `file` | no | A regex, `re.fullmatch`ed against the importing file's path relative to the project root, POSIX-spelled (`src/gt4py/next/ffront/decorator.py`) — the same candidate a `skip` rule's `file` is matched against. |
| `reason` | no | Free text, echoed in every `CP006` and `CP003` the rule causes. |

When both `importer` and `file` are given, both must match. There is no
negation key: "everyone but `gt4py.next` itself" is spelled by putting the
`importer`-scoped rule *first*, as above — inside the package the first rule
matches and asks for the own name; everywhere else it does not, and the next
rule applies. An unknown key, a value of the wrong type, a malformed pattern or
template, an uncompilable regex, and a second rule with neither `importer` nor
`file` whose `module` repeats an earlier such rule's exactly (it could never
apply) are all configuration errors (exit `2`).

### What is a binding of a module

| Statement | Binds |
| --- | --- |
| `import M as N` | `N`, to `M` |
| `import M` (no dot) | `M`, to `M` |
| `import a.b.c` | `a`, to `a` — so a rule for `a` judges it — and the chain `a.b.c`: compliant only with a rule whose `as` is `false`; an identifier rule for `a.b.c` asks for `import a.b.c as NAME` |
| `from P import L [as N]` | `N` (or `L`), to `P.L` — **only when the resolver proves `P.L` is a module**. An object, or a name it cannot classify, is `CP001`'s or `CP002`'s business and never a `CP006`. A relative import is matched on the absolute name it resolves to. |

Never judged: `from __future__` and wildcard imports; a module-level import in
a package's `__init__.py` (its [public surface](#a-packages-__init__py), as for
`CP001`; an import inside a function there is judged); and, under
`scope = "first-party"`, any module outside your analysis roots — the scope is
applied as it is to every other finding, so nothing is classified for it.
Under that scope, alias rules therefore apply only to your own modules: a
convention for a third-party module, such as ruff's `numpy = "np"`, is
enforced (and followed by `--fix`) only with `scope = "all"`.

A `skip` rule covering the import's line, or pinning the bound name, turns a
`CP006` into a `CP004`, as it does a `CP001`; so does an inline
`# cleanporter: ignore[CP006]` on the import's line. `--select`, `--ignore` and
baselines treat `CP006` like any other code; a baseline records it by path,
module and bound name.

### When `--fix` cannot follow a rule

Without a rule, a new binding whose name is taken in its scope gets a numeric
suffix (`helpers_2`). With one, a suffixed name would be a `CP006` the fixer
wrote itself, so when the configured name — or, for `as = false`, the leaf — is
taken in the scope (by any name the scope or an enclosing one binds, one a
nested scope using the new binding would shadow, or a builtin or undefined name
read where the binding would be visible — `str` for `as = "str"`), the **whole file** is left
unchanged and reported `CP003`: *configured alias 'np' for numpy is taken in
this scope*. Rename the conflicting name, or change the rule. `--fix` never
introduces a `CP006`.

### Ruff's aliases as defaults

If your `pyproject.toml` configures ruff's `flake8-import-conventions`
(`ICN001`), cleanporter reads its `aliases` and `extend-aliases` from
`[tool.ruff.lint.flake8-import-conventions]` — or, when that table is absent,
from the legacy `[tool.ruff.flake8-import-conventions]` — and appends each
`module = "alias"` entry as an unconditional, exact rule **after** every
`[[tool.cleanporter.alias]]` rule, so your own rules always win. An entry in
`extend-aliases` replaces one for the same module in `aliases`. Its `CP006`
names the ruff key it came from.

Only what is written in that `pyproject.toml` is read: not `ruff.toml` or
`.ruff.toml`, and not ruff's built-in default table (`numpy = "np"`, `pandas =
"pd"`, ...), which changes between ruff versions — an alias cleanporter
enforces is one somebody wrote down. A malformed entry (an alias that is not a
string, or not an identifier) is a configuration error naming the ruff key.
Set `ruff_aliases = false` to ignore ruff's table altogether.

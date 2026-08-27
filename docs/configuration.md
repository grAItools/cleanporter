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
# python = "/path/to/target/venv/bin/python"   # omit = current interpreter
```

## Reference

| Key | Default | Description |
| --- | --- | --- |
| `exclude` | `[]` | Glob patterns (list of strings). Each is matched against the project-relative POSIX path of a candidate file or directory, and also against its absolute POSIX path. A pattern containing no glob metacharacter (`*`, `?`, `[`) additionally matches a directory prefix, so `"tests/"` excludes `tests/` and everything under it. |
| `scope` | `"all"` | `"all"` reports violations everywhere, including stdlib and third-party imports. `"first-party"` reports only imports whose top-level package is one of your own analysis roots. |
| `source_roots` | `[]` | Explicit first-party import roots — directories that are on `sys.path` for the code being analysed — relative to the `pyproject.toml` directory. Combined with, not substituted for, whatever the analysed paths themselves imply. A declared root outranks an inferred one. |
| `treat_unresolved_as_error` | `false` | When `true`, `CP002` (unresolved) findings count toward the failure exit code, so a run that could not classify something exits `1`. |
| `exempt_modules` | `["typing", "typing_extensions", "collections.abc", "__future__"]` | `from MODULE import X` is allowed when `MODULE` — or any ancestor of it — is in this set. Configured values are **added to** the built-in defaults; they never replace them. |
| `exempt_names` | `[]` | Individual bound names that are always allowed, whatever module they came from. Checked before the module is even looked at. |
| `python` | absent (the current interpreter) | Path to the interpreter used for the stdlib/third-party classification probe. Must be a string; omit the key to use the interpreter running cleanporter. |

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

## How CLI flags layer on top of config

Flags do not replace configured values; with two exceptions they extend or
strengthen them. This means a developer can tighten a run locally without
having to restate what the project already declares.

| Flag | Effect on the loaded config |
| --- | --- |
| `--exempt MODULE` | Added to `exempt_modules` (which already contains the built-in defaults). |
| `--root PATH` | Appended to `source_roots`. Relative values resolve against the project root, i.e. the `pyproject.toml` directory. |
| `--strict` | OR-ed into `treat_unresolved_as_error`. `--strict` can turn it on; it can never turn it off. |
| `--python PATH` | **Overrides** the `python` key, but only when the flag is actually given. |

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
your analysis roots are considered; stdlib and third-party imports are passed
over without being resolved at all. This is a useful staging step on a large
legacy codebase: fix your own modules first, then widen to `"all"`.

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

Two details worth knowing:

- A pattern with no glob metacharacter (`*`, `?`, `[`) matches a *directory
  prefix* as well as an exact path — that is what makes `"tests/"` exclude the
  whole tree. A pattern that does contain one is matched only as a glob.
- The globs are `fnmatch` patterns, not shell or `pathlib` ones, so `*`
  matches `/` too. `"docs/examples/*"` therefore covers nested files, and a
  pattern like `"tests*"` is broader than it looks.
- Each pattern is tried against the project-relative path first and against
  the absolute POSIX path second, so an absolute pattern also works.

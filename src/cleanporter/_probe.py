"""Standalone, stdlib-only classifier for ``from PARENT import NAME``.

This module answers a single question: given a ``from PARENT import NAME``
statement, is ``PARENT.NAME`` a *module/subpackage* (compliant with Google
Python Style Guide 2.2) or an *object* (class / function / constant -> a
violation)?

It is deliberately dependency-free so that it can be executed *inside the
target project's own interpreter* -- either in-process (when the tool runs in
the same venv as the code under analysis) or via a subprocess when a different
``--python`` is supplied. Keeping it stdlib-only means the heavy tool
dependency (libCST) never needs to be installed next to a project's
scientific / GPU stack.

Classification contract (``classify``):

* ``True``  -> ``PARENT.NAME`` is a module or subpackage.
* ``False`` -> ``NAME`` is an attribute of module ``PARENT`` (an object).
* ``None``  -> undetermined (``PARENT`` could not be imported in this
  environment, e.g. an optional/GPU dependency is missing). Callers MUST treat
  ``None`` as "do not touch": report it in ``check`` mode, skip it in ``fix``
  mode. We never guess, because a wrong guess in ``fix`` mode produces broken
  code.

The leaf ``NAME`` is *never* imported: only its parent package is, and only to
inspect ``__path__`` / discover submodule specs. Objects are never imported at
all.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import sys
import types

_MISSING = object()


def classify(parent: str, name: str) -> bool | None:
    """Return whether ``parent.name`` names a module. See module docstring."""
    if not parent:
        # A bare ``import name`` is always module-only; this helper is only
        # meaningful for ``from parent import name``.
        return None
    try:
        pkg = importlib.import_module(parent)
    except BaseException:  # noqa: BLE001 - any failure means "unknown"
        # Parent not importable here (missing optional dep, no GPU, import-time
        # error, ...). Undetermined -> caller must skip.
        return None

    # If PARENT is a package, ask the finders whether ``parent.name`` resolves
    # to a spec *without* importing the leaf. A spec means it is a submodule.
    if hasattr(pkg, "__path__"):
        try:
            spec = importlib.util.find_spec(f"{parent}.{name}")
        except BaseException:  # noqa: BLE001 - not a package / broken finder
            spec = None
        if spec is not None:
            return True

    # Fall back to the type of the already-loaded attribute. This is what
    # distinguishes ``from os import path`` (a module bound as an attribute of
    # the non-package module ``os`` -> compliant) from ``from os import getcwd``
    # (a function -> violation), and catches submodules already imported by the
    # parent's ``__init__``.
    obj = getattr(pkg, name, _MISSING)
    if obj is _MISSING:
        # A package with no such submodule/attribute: treat as an object only
        # when PARENT is a package (then the name is genuinely not a module);
        # otherwise it may be dynamically created -> undetermined.
        return False if hasattr(pkg, "__path__") else None
    return isinstance(obj, types.ModuleType)


def classify_many(pairs: list[tuple[str, str]]) -> dict[str, bool | None]:
    r"""Classify many ``(parent, name)`` pairs, caching parent imports.

    Keys in the returned mapping are ``f"{parent}\x00{name}"`` so the result is
    trivially JSON-serialisable for the subprocess bridge.
    """
    out: dict[str, bool | None] = {}
    for parent, name in pairs:
        key = f"{parent}\x00{name}"
        if key not in out:
            out[key] = classify(parent, name)
    return out


def _main() -> int:
    """Entry point for the subprocess bridge: read JSON pairs, write JSON map."""
    raw = sys.stdin.read()
    pairs = [(p, n) for p, n in json.loads(raw)] if raw.strip() else []
    json.dump(classify_many(pairs), sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())

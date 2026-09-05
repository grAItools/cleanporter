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
* ``AMBIGUOUS`` -> ``PARENT.NAME`` is a submodule *and* ``PARENT`` binds that
  name to something that is not a module, so which one ``from PARENT import
  NAME`` reaches depends on import order. The same shape the first-party layer
  calls ``Kind.AMBIGUOUS``; callers treat it as undetermined, and it is spelled
  apart from ``None`` only so the reason given to the user is the true one.
* ``None``  -> undetermined (``PARENT`` could not be imported in this
  environment, e.g. an optional/GPU dependency is missing). Callers MUST treat
  ``None`` as "do not touch": report it in ``check`` mode, skip it in ``fix``
  mode. We never guess, because a wrong guess in ``fix`` mode produces broken
  code.

The leaf ``NAME`` is *never* imported: only its parent package is, and only to
inspect ``__path__`` / discover submodule specs, and to read its ``__dict__``
without running a module-level ``__getattr__``. Objects are never imported at
all.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import sys
import types

_MISSING = object()

#: Answer for "a submodule of that name exists, but ``PARENT`` binds the name
#: to an object". A plain string so the JSON bridge carries it unchanged,
#: and distinct from every other answer so ``is True`` / ``is False`` /
#: ``is None`` tests keep meaning exactly what they meant.
AMBIGUOUS = "ambiguous"


def classify(parent: str, name: str) -> bool | str | None:
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
            # A spec proves the submodule exists; it does not prove that
            # ``from PARENT import NAME`` reaches it. That statement binds
            # ``getattr(PARENT, NAME)`` whenever the attribute is there, so a
            # package whose ``__init__`` does ``from .NAME import NAME``
            # hands out the object instead -- and the fixer would qualify
            # every use site against it. Whether the attribute is the module
            # or the object also depends on what else has been imported, so
            # this is undecidable rather than merely unread: report the
            # collision, never guess it away.
            #
            # ``vars(pkg)``, not ``getattr``: the attribute must be read
            # without running any of the package's code. A module-level
            # ``__getattr__`` (PEP 562) is the standard lazy-submodule hook,
            # and asking it for NAME imports the leaf -- which this module
            # promises never to do, costs an arbitrary amount of work in the
            # target interpreter, and can raise (``lazy_loader.attach``
            # raises ``ImportError`` for a leaf whose optional dependency is
            # absent). A binding that shadows the submodule is written into
            # the package's ``__dict__`` by its ``__init__``, so it is
            # exactly what ``vars`` sees. A lazy ``__getattr__`` that returns
            # a *non-module* for a name that also has a spec is not detected,
            # and is left as a stated limit rather than paid for by importing
            # every leaf.
            shadow = vars(pkg).get(name, _MISSING)
            if shadow is not _MISSING and not isinstance(shadow, types.ModuleType):
                return AMBIGUOUS
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


def classify_many(pairs: list[tuple[str, str]]) -> dict[str, bool | str | None]:
    r"""Classify many ``(parent, name)`` pairs, caching parent imports.

    Keys in the returned mapping are ``f"{parent}\x00{name}"`` so the result is
    trivially JSON-serialisable for the subprocess bridge.

    **Ancestors are classified before their descendants**, which is what the
    sort is for and not merely tidiness. Classifying ``("P.S", "obj")``
    imports ``P.S``, and the import system's last act is
    ``setattr(P, "S", <module>)`` -- overwriting the very binding that tells
    ``("P", "S")`` apart from a plain submodule. Asked in that order, a
    shadowed name answers ``AMBIGUOUS`` on its own and ``True`` in a batch
    that also mentions the leaf, which is a verdict decided by which *other*
    files a run happened to include. A parent sorts before any of its
    descendants because it is a prefix of them, so the sort makes the batch
    self-consistent: no pair here can hide the shadow that another pair here
    is asking about.

    It does **not** make the answer independent of everything else. Any
    earlier-sorting parent whose own import loads ``P.S`` -- an unrelated
    third package doing ``import P.S`` at module level -- replaces the
    binding before this ever looks, and a shadow written as a ``def`` or an
    assignment is then invisible. Which is to say the verdict for such a
    package can still depend on which other files a run includes; it is no
    longer decided by the run's own leaf pair, which was the systematic case
    and the reproducible one.

    A shadow the package installs by importing its own leaf (``from .S
    import S``, the idiom this exists for) is immune to all of that: the
    module is in ``sys.modules`` from then on, so no later import re-runs the
    ``setattr``, and every real instance in the corpus has that shape.
    Making the answer order-free for the rest means not reading runtime state
    at all -- parsing ``P``'s ``__init__`` with ``ast`` here, the way the
    first-party layer already does -- at the price of a second copy of that
    rule in a module that cannot import the first.
    """
    out: dict[str, bool | str | None] = {}
    for parent, name in sorted(pairs):
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

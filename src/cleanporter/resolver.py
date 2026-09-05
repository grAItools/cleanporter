"""Module/object resolution, layered for correctness and safety.

Order of resolution for a ``from PARENT import NAME``:

1. **First-party, filesystem** (``firstparty.ModuleMap``): if ``PARENT`` is one
   of the packages under analysis, decide purely from the source tree -- no
   imports, no side effects, correct for namespace packages.
2. **Stdlib / third-party, interpreter probe** (``_probe``): ask the target
   interpreter via ``importlib`` whether ``PARENT.NAME`` is a submodule. Only
   the parent package is imported (cached); never the leaf, never objects.
3. **Undetermined** -> ``None``. ``check`` reports it, ``fix`` skips it.

The interpreter probe runs in-process when ``python`` is the current
interpreter, otherwise in a subprocess so tool deps stay out of the target env
and native-library crashes are contained.

Answers are cached per ``(parent, name)``, and `warm` classifies a whole batch
of pairs up front -- one subprocess round-trip for a run rather than one per
import.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

from cleanporter import firstparty, model

from . import _probe

#: Wall-clock budget for one out-of-process probe batch. A probe that
#: outlives it is killed and its whole batch reported undetermined.
_PROBE_TIMEOUT = 120

_AMBIGUOUS = "'{name}' is both a submodule of '{parent}' and bound in its __init__"
_NOT_IMPORTABLE = "'{parent}' is not importable in the target interpreter"


class Resolver:
    def __init__(self, module_map: firstparty.ModuleMap, python: str | None = None) -> None:
        self._map = module_map
        self._python = python or sys.executable
        self._in_process = (
            pathlib.Path(self._python).resolve() == pathlib.Path(sys.executable).resolve()
        )
        self._cache: dict[tuple[str, str], bool | None] = {}
        #: Every ``(module, name)`` some analysed file *uses*. Populated by
        #: `analyze.build`; empty when the resolver is used standalone, which
        #: makes `is_load_bearing` answer False -- no evidence, no claim.
        self._uses: frozenset[tuple[str, str]] = frozenset()
        #: Modules some analysed file star-imports, which could need any name.
        self._star_imported: frozenset[str] = frozenset()
        self._notes: dict[tuple[str, str], str] = {}
        self._probe_path = str(pathlib.Path(_probe.__file__).resolve())

    def _from_kind(self, key: tuple[str, str], kind: model.Kind) -> bool | None:
        if kind is model.Kind.MODULE:
            self._cache[key] = True
        elif kind is model.Kind.OBJECT:
            self._cache[key] = False
        else:
            self._cache[key] = None
            self._notes[key] = _AMBIGUOUS.format(parent=key[0], name=key[1])
        return self._cache[key]

    def is_module(self, parent: str, name: str) -> bool | None:
        """True if ``parent.name`` is a module, False if object, None if unknown."""
        key = (parent, name)
        if key in self._cache:
            return self._cache[key]

        # 1. First-party filesystem answer is authoritative and side-effect free.
        kind = self._map.classify(parent, name)
        if kind is not None:
            return self._from_kind(key, kind)

        # 2. Interpreter probe for stdlib / third-party.
        result = self._probe([key]).get(key)
        self._cache[key] = result
        return result

    def is_first_party(self, dotted: str) -> bool:
        """True when *dotted*'s top-level component is one of the analysis roots.

        Only the first dotted component is checked against the module map, so
        ``is_first_party("pkg.nonexistent")`` is ``True`` whenever ``pkg`` is
        first-party -- it does not verify that the full dotted path exists.
        This fails safe: such a name is still reported when unresolvable, it
        is just never mis-rewritten as third-party.
        """
        return self._map.is_first_party(dotted)

    def note_uses(self, uses: set[tuple[str, str]], star_imported: set[str]) -> None:
        """Record every ``module.name`` the analysed files read, however spelled."""
        self._uses = frozenset(uses)
        self._star_imported = frozenset(star_imported)

    def qualname_for(self, path: pathlib.Path, relative_level: int = 0) -> str | None:
        """Dotted module name of *path*, or None when it is not under a root."""
        return self._map.qualname_for(path, relative_level)

    def replacement_unreachable(self, parent: str) -> str | None:
        """Why the import the fixer would *write* for *parent* cannot be trusted.

        Returns the reason, or ``None`` when the replacement provably binds
        the module ``parent``.

        The fixer turns ``from P.S import obj`` into an import of ``P.S``
        itself, spelled ``from P import S``, and qualifies every use as
        ``S.obj``. That statement imports ``P``, binds ``getattr(P, 'S')``,
        and falls back to importing the submodule *only if that attribute is
        absent* -- so whatever ``P``'s ``__init__`` binds under the name
        ``S`` wins, silently. gt4py's
        ``iterator/transforms/concat_where/__init__.py`` binds
        ``transform_to_as_fieldop`` to a *function* of that name, so the
        emitted ``from ...concat_where import transform_to_as_fieldop``
        yields the function and every rewritten use site raises
        ``AttributeError``: code that imports, parses, and does not run.

        Read inside ``P`` itself the same hazard needs no separate rule.
        There ``P``'s attributes *are* the file's own module-level names, and
        the file is ``P``'s ``__init__``, so the binding this reads is the
        competing one. In ``celery/security/__init__.py`` that produced
        ``kombu.serialization`` under a name meant for
        ``celery.security.serialization``: code that imports, runs, and is
        wrong.

        Whether ``P.S`` is reachable as an attribute of ``P`` is the question
        `is_module` already answers, by the settled rule -- a name that is
        both a submodule on disk and a top-level binding in the package's
        ``__init__`` is `model.Kind.AMBIGUOUS`, never guessed. Asking it here
        rather than inventing a second rule keeps the two from drifting.
        Anything short of a firm "yes, a module" means the replacement cannot
        be trusted, and the import is kept exactly as written -- including
        the case where this run cannot see ``P.S`` at all. That is a claim
        about the module map's reach rather than about ``P``: the map answers
        for a first-party *top-level* name whether or not it scanned the
        subtree, so a run pointed at one distribution of a namespace package
        can call a sibling's module an object. Declining costs a fix that
        would have been correct; the message says which run would not, and
        pointing cleanporter at the whole tree (or declaring ``source_roots``)
        settles it.

        Reading the ``__init__`` is a parse of what is *on disk*, so this sees
        bindings the author wrote, not ones this run is about to introduce.
        Those are prevented at source instead, by
        `rewrite._Fixer._allocate_token` refusing to allocate a submodule's
        name at module scope in the first place.

        A *parent* with no dot needs no check at all: the replacement is
        ``import P``, which binds the module and nothing else.
        """
        package, _, token = parent.rpartition(".")
        if not package:
            return None
        verdict = self.is_module(package, token)
        if verdict is True:
            return None
        cause = (
            f"'{package}' has no submodule '{token}' under this run's import roots"
            if verdict is False
            else self.reason(package, token)
        )
        return (
            f"the replacement 'from {package} import {token}' cannot be shown "
            f"to bind the module '{parent}': {cause}"
        )

    def submodules(self, dotted: str) -> frozenset[str]:
        """Leaf names of *dotted*'s own submodules; empty when it has none.

        First-party only, and deliberately so: the fixer asks this about the
        module it is *rewriting*, which is always a file under analysis.
        """
        return self._map.submodules(dotted)

    def is_load_bearing(self, module: str, name: str) -> bool:
        """True when ``module.name`` is a re-export another analysed file needs.

        Rewriting ``module``'s own ``from P import name`` is correct for that
        file in isolation, and it *deletes* ``module.name``. That only matters
        if something imports it from there -- so this asks both halves:

        * does ``module`` bind ``name`` by importing it rather than defining
          it (`firstparty.ModuleMap.is_reexport`), so a rewrite would remove
          the attribute at all, and
        * does any file in this run *use* ``module.name``?

        A use is any of ``from module import name``, a ``module.name``
        attribute read through an import binding, or ``from module import *``
        (which could need any of them). Counting only the first was not
        enough: ``import M`` plus ``M.name`` is the very shape this tool
        rewrites everything into, so a second ``--fix`` run would delete the
        attribute the first run had just protected.

        Both must hold. A name that is also *defined* in ``module`` (imported
        under a ``try``, defined in the ``except``) survives the rewrite, and
        a re-export nobody uses is free to fix.

        The evidence is limited to the files under analysis, which is the same
        boundary every other guard has: a consumer outside the run is the
        documented cross-file limitation, unchanged.
        """
        used = (module, name) in self._uses or module in self._star_imported
        return used and self._map.is_reexport(module, name)

    def reason(self, parent: str, name: str) -> str:
        """Human explanation for an unresolved (``None``) verdict."""
        key = (parent, name)
        return self._notes.get(key, _NOT_IMPORTABLE.format(parent=parent))

    def warm(self, pairs: list[tuple[str, str]]) -> None:
        """Classify a batch up front (one subprocess round-trip for the lot).

        Batching is also what makes the probe's ancestors-before-descendants
        rule effective (`_probe.classify_many`): a pair asked on its own,
        later, can find a parent whose binding an earlier leaf import has
        already replaced. Every pair a run needs is collected in
        `analyze.build`, so that path is the one that matters.
        """
        pending: list[tuple[str, str]] = []
        for key in pairs:
            if key in self._cache:
                continue
            kind = self._map.classify(*key)
            if kind is not None:
                self._from_kind(key, kind)
            else:
                pending.append(key)
        if pending:
            self._cache.update(self._probe(pending))

    # -- interpreter probe -------------------------------------------------
    def _probe(self, pairs: list[tuple[str, str]]) -> dict[tuple[str, str], bool | None]:
        if not pairs:
            return {}
        if self._in_process:
            flat = _probe.classify_many(pairs)
        else:
            # Every failure mode of the bridge -- a non-zero exit, an
            # interpreter that cannot be run at all, one that hangs past the
            # timeout, or one that writes something that is not the expected
            # JSON map -- reports the *whole batch* as undetermined. "Never
            # guess" applies to the transport exactly as it does to the
            # classification: reporting nothing is recoverable, guessing
            # wrong in --fix mode is not.
            try:
                proc = subprocess.run(
                    [self._python, self._probe_path],
                    input=json.dumps(pairs),
                    capture_output=True,
                    text=True,
                    timeout=_PROBE_TIMEOUT,
                    check=False,
                )
            except (subprocess.SubprocessError, OSError):
                return dict.fromkeys(pairs)
            if proc.returncode != 0:
                return dict.fromkeys(pairs)
            try:
                flat = json.loads(proc.stdout or "{}")
            except ValueError:
                return dict.fromkeys(pairs)
            if not isinstance(flat, dict):
                return dict.fromkeys(pairs)
        out: dict[tuple[str, str], bool | None] = {}
        for parent, name in pairs:
            answer = flat.get(f"{parent}\x00{name}")
            if answer == _probe.AMBIGUOUS:
                # Same verdict as `model.Kind.AMBIGUOUS` from the filesystem
                # layer, and the same note, so the two layers say the same
                # thing about the same shape.
                self._notes[(parent, name)] = _AMBIGUOUS.format(parent=parent, name=name)
                out[(parent, name)] = None
            else:
                # Anything that is not one of the two booleans is
                # undetermined -- including whatever a malformed bridge
                # response might contain.
                out[(parent, name)] = answer if isinstance(answer, bool) else None
        return out

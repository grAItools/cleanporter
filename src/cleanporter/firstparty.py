"""First-party module discovery from the filesystem (no imports, no side effects).

Given the paths under analysis we infer the *import roots* (directories that sit
on ``sys.path`` for these files) and enumerate every dotted name that is a
package or module. That lets us:

* classify ``from PARENT import NAME`` for first-party ``PARENT`` with certainty
  (is ``PARENT.NAME`` a directory/``.py`` under the tree, or does ``PARENT``'s
  own source bind ``NAME``?), and
* compute a file's dotted module name so relative imports (``from . import x``,
  ``from .a.b import Y``) can be resolved to an absolute ``PARENT``.

Namespace packages (PEP 420, no ``__init__.py``) are treated as packages when a
directory contains any Python submodules/subpackages, including extension
modules.

Roots are inferred per path and then ranked against each other -- declared
roots outrank inferred ones, deeper outranks shallower, and a root another file
has shown to be a package is demoted out of the running. See `qualname_for` and
`demote_roots`, where the rules and the cases that forced them are written out.

**Object only on positive evidence.** A submodule that is *not* on disk is not
thereby an object. A checkout routinely lacks real submodules: a
``_version.py`` setuptools_scm writes at build time, a protobuf ``_pb2``, a
Cython module whose ``.pyx`` is all the tree holds, the sibling portion of a
PEP 420 namespace package installed from another distribution. Calling those
objects once made ``--fix`` turn ``from pkg import _version`` into
``pkg._version`` -- an ``AttributeError`` whenever ``pkg/__init__`` had not
happened to import it. So when ``PARENT.NAME`` is not on disk,
`ModuleMap.classify` reads what the parent's source *binds* ``NAME`` to, and
follows every binding to where it comes from (`ModuleMap._lookup_in` spells
out how): a ``def`` is an object, ``from .sub import helpers`` is whatever
``sub.helpers`` is, ``from os import path`` is the probe's to answer
(`Deferral`). An object or a module needs every binding to agree; anything
else is `model.Kind.UNDETERMINED`, with the reason in
`ModuleMap.unresolved_reason`.

**What the scan skips.** At an import root, the directories discovery never
walks (`discover.ALWAYS_SKIP_DIRS`: ``build``, ``dist``, ``node_modules``,
``site-packages``, ``__pycache__``) are not scanned: a ``build/lib/pkg`` copy
of the tree is not a second first-party package, and a ``dist/`` does not
make ``dist`` a first-party name. *Below* a root they are ordinary names --
pip really has a ``pip/_internal/operations/build/`` package -- so only
``__pycache__`` and dot-directories are skipped there. ``exclude`` is
deliberately *not* honoured here. It says which files to analyse, not which
modules exist; an excluded directory still holds modules other files import,
and dropping them from the map would turn those imports from a proven module
into ``CP002`` for no gain in safety.
"""

from __future__ import annotations

import dataclasses
import pathlib
import sys

from cleanporter import _bindings, discover, model

#: Suffixes CPython will import as an extension module.
EXTENSION_SUFFIXES = frozenset({".so", ".pyd"})


def _module_stem(path: pathlib.Path) -> str:
    """``accel.cpython-314-x86_64-linux-gnu.so`` -> ``accel``."""
    return path.name.split(".")[0]


def _is_importable_file(path: pathlib.Path) -> bool:
    return path.suffix == ".py" or path.suffix in EXTENSION_SUFFIXES


def _is_pkg_dir(d: pathlib.Path) -> bool:
    if (d / "__init__.py").is_file():
        return True
    # PEP 420 namespace package: a directory that contributes submodules.
    return d.is_dir() and any(
        _is_importable_file(c) or (c.is_dir() and c.name != "__pycache__") for c in d.iterdir()
    )


def _root_for(path: pathlib.Path) -> pathlib.Path:
    """Return the directory that would be on ``sys.path`` for ``path``.

    Walk upward while the parent still looks like a package, so a file deep in a
    package resolves against the top-level package's parent.
    """
    d = path if path.is_dir() else path.parent
    while (d.parent / d.name).is_dir() and (d / "__init__.py").is_file():
        if not _is_pkg_dir(d.parent):
            break
        d = d.parent
    return d.parent if (d / "__init__.py").is_file() else d


def _nesting_warnings(roots: list[pathlib.Path]) -> list[str]:
    """One warning per pair of inferred roots where one contains the other.

    Nested roots are legitimate (a ``src/`` layout plus a ``tests/`` package
    infers both ``src`` and the repo root), but they make the same file
    reachable under two different dotted names, so it is worth saying out
    loud which one won -- that ambiguity is what once produced
    ``from src.mypkg import helpers``.
    """
    out: list[str] = []
    for outer in roots:
        out.extend(
            f"import roots nest: '{outer}' contains '{inner}'; each file is "
            "qualified against the most specific root that can hold it, "
            "and a declared root beats an inferred one"
            for inner in roots
            if inner is not outer and inner.is_relative_to(outer)
        )
    return out


@dataclasses.dataclass(frozen=True)
class _Evidence:
    """Everything a module's source says one of its names could be bound to.

    A name can be bound by several statements -- ``try: from ._speedups
    import f`` with ``def f`` in the ``except`` -- and without running the
    module which one wins is unknown, so every binding is kept and the verdict
    is decided over all of them: an object only if every one is an object, a
    module only if every one is a module.
    """

    #: What the bindings read here resolved to: ``True`` a module, ``False``
    #: an object.
    kinds: frozenset[bool] = frozenset()
    #: ``(module, name)`` pairs outside the first-party tree that a binding
    #: was imported from; only the interpreter probe can answer for them.
    external: frozenset[tuple[str, str]] = frozenset()
    #: Non-empty when some binding cannot be known without running code: the
    #: innermost reason why. It decides the verdict whatever else was found.
    unknown: str = ""
    #: The first hop of the chain that led to `unknown` (``'pkg' imports 'X'
    #: from 'pkg.core'``), so a reason names where the chain starts and where
    #: it failed without reciting every link in between.
    via: str = ""

    def __or__(self, other: _Evidence) -> _Evidence:
        first = self if self.unknown else other
        return _Evidence(
            self.kinds | other.kinds, self.external | other.external, first.unknown, first.via
        )

    def through(self, hop: str) -> _Evidence:
        """This (undetermined) evidence, reached by *hop*."""
        return _Evidence(unknown=self.unknown, via=hop)

    @property
    def reason(self) -> str:
        """`unknown`, prefixed with the hop that led to it."""
        return f"{self.via}, and {self.unknown}" if self.via else self.unknown

    @property
    def empty(self) -> bool:
        """True when nothing binds the name at all."""
        return not (self.kinds or self.external or self.unknown)


_NOTHING = _Evidence()
_OBJECT = _Evidence(kinds=frozenset({False}))
_MODULE = _Evidence(kinds=frozenset({True}))
#: `ModuleMap._low` when no in-progress lookup has been reached.
_NO_BACK_EDGE = sys.maxsize
#: `ModuleMap._low` once a lookup has been cut short by a limit: below every
#: depth, so nothing on the way back up is memoised.
_TRUNCATED = -1
#: How many lookups deep a chain of re-exports and star imports is followed
#: before the answer is "undetermined". Real chains are a handful long; the
#: limit exists so that a pathological one ends in a finding, not in a
#: ``RecursionError``.
_MAX_DEPTH = 64
#: How many module evaluations one top-level question may cost. Memoisation
#: keeps acyclic graphs linear, but lookups inside a cycle cannot be memoised
#: until the cycle closes, so a large cyclic knot of star imports could
#: otherwise take exponential time.
_MAX_EVALUATIONS = 20_000
#: Reason given when a non-monotone step reads a value that is still being
#: computed further up the chain (see `ModuleMap._lookup`).
_CIRCULAR = "that is part of a circular import, so what it binds depends on import order"


@dataclasses.dataclass(frozen=True)
class Deferral:
    """A first-party ``PARENT.NAME`` whose verdict rests on third-party imports.

    ``PARENT`` binds ``NAME`` with ``from M import X`` for some ``M`` outside
    the analysed tree -- ``from os import path``, ``from numpy import
    ndarray``. Whether that is a module is the interpreter probe's question,
    not this map's, so the map hands back what it did find and which pairs the
    probe must answer; `resolver.Resolver` combines them by the same rule
    `_Evidence` states.
    """

    #: Verdicts of the first-party bindings: ``True`` module, ``False`` object.
    local: frozenset[bool]
    #: The third-party ``(M, X)`` pairs to ask the probe about.
    pairs: tuple[tuple[str, str], ...]


@dataclasses.dataclass(frozen=True)
class _Verdict:
    kind: model.Kind
    reason: str = ""
    deferral: Deferral | None = None


def disagreement(parent: str, name: str) -> str:
    """The reason given for a name whose bindings disagree about being a module."""
    return (
        f"'{parent}' binds '{name}' more than once, to a module and to something "
        "that is not, and which binding wins is decided at run time"
    )


def _anchor(package: str, level: int, module: str | None) -> str | None:
    """Absolute name of ``from <level dots><module> import ...`` read in *package*."""
    if level == 0:
        return module
    parts = package.split(".") if package else []
    if level - 1 >= len(parts):
        return None
    base = parts[: len(parts) - (level - 1)]
    return ".".join([*base, module] if module else base)


class ModuleMap:
    """Enumerates first-party packages/modules under a set of import roots.

    ``declared`` names the roots the user stated outright (``--root`` /
    ``source_roots``); the rest were inferred from the filesystem. The
    distinction only matters when several roots contain the same file --
    see `qualname_for`.
    """

    def __init__(self, roots: list[pathlib.Path], declared: tuple[pathlib.Path, ...] = ()) -> None:
        self.roots = [r.resolve() for r in roots]
        #: Roots the user declared, which outrank inferred ones.
        self.declared: frozenset[pathlib.Path] = frozenset(d.resolve() for d in declared)
        self.roots += [d for d in sorted(self.declared) if d not in self.roots]
        #: Human-readable notes about the root set itself (see `_nesting_warnings`).
        self.warnings: list[str] = _nesting_warnings(self.roots)
        #: Roots another file has shown to be a package (see `demote_roots`).
        self._demoted: set[pathlib.Path] = set()
        self._modules: set[str] = set()  # dotted names of .py modules
        self._packages: set[str] = set()  # dotted names of packages
        self._inits: dict[str, list[pathlib.Path]] = {}  # dotted package -> its __init__.py files
        #: dotted module -> *every* ``.py`` on disk that claims that name (a
        #: package contributes its ``__init__.py``). Normally one, but a name
        #: can be claimed twice -- ``pkg/`` beside ``pkg.py``, or the same
        #: package under two import roots -- and which file an interpreter
        #: actually imports depends on ``sys.path`` and on precedence rules
        #: this filesystem-only map does not adjudicate. Every claimant is
        #: kept so the queries below can answer for all of them rather than
        #: for whichever happened to be scanned last. Extension modules are
        #: absent: there is no source to parse, so `is_reexport` cannot
        #: answer for them and says no.
        self._sources: dict[str, list[pathlib.Path]] = {}
        #: dotted package -> the leaf names of its immediate children. Built
        #: while scanning because `submodules` is asked once per rewritten
        #: file and deriving it by filtering `_modules` would be quadratic.
        self._children: dict[str, set[str]] = {}
        #: dotted names some claimant provides with no source to read: a
        #: namespace package (no ``__init__.py``) or an extension module.
        self._sourceless: set[str] = set()
        self._verdicts: dict[tuple[str, str], _Verdict] = {}
        #: Finished `_lookup` results. One computed while a lookup further up
        #: the chain was still in progress is partial and is not kept.
        self._memo: dict[tuple[str, str], _Evidence] = {}
        #: Lookups in progress, with their depth on the chain.
        self._active: dict[tuple[str, str], int] = {}
        #: Shallowest in-progress lookup reached since the current one began.
        self._low = _NO_BACK_EDGE
        #: Module evaluations spent on the current top-level question.
        self._evaluations = 0
        #: `_star_shadowing` questions in progress, so that packages whose
        #: star imports lead back to one another end rather than recurse.
        self._shadow_checks: dict[tuple[str, str], int] = {}
        for root in self.roots:
            self._scan(root, root)
        #: Top-level names of everything scanned, for `is_first_party`.
        self._tops: frozenset[str] = frozenset(
            d.split(".", 1)[0] for d in self._packages | self._modules
        )

    @classmethod
    def from_paths(
        cls, paths: list[pathlib.Path], declared: tuple[pathlib.Path, ...] = ()
    ) -> ModuleMap:
        roots: set[pathlib.Path] = set()
        for p in paths:
            roots.add(_root_for(p.resolve()))
        return cls(sorted(roots), declared=declared)

    def demote_roots(self, evidence: dict[str, list[pathlib.Path]]) -> None:
        """Rank down inferred roots that some *other* file imports as a package.

        ``evidence`` maps a top-level name imported absolutely (``import
        analytics.io`` -> ``analytics``) to the files that import it.

        A PEP 420 namespace package holding a regular subpackage --
        ``analytics/`` with no ``__init__.py`` around ``analytics/io/`` -- is
        the canonical PEP 420 layout, and it defeats both of the other rules
        in `qualname_for`: `_root_for` infers ``analytics`` as a root, and
        ``analytics/io/__init__.py`` really can sit one package deep, so its
        own relative imports do not rule that root out. Nothing *inside*
        ``analytics`` can settle it. A file outside it saying ``from
        analytics.io import x`` can: ``analytics`` is then a package under
        some higher root, so it is not a root itself. Without this, ``from
        .readers import read`` in that ``__init__.py`` is classified as an
        import from ``io.readers``. The fixer no longer writes that name --
        a relative import's replacement stays relative
        (`_imports.module_import_spelling`) -- but it once rewrote the line to
        ``from io import readers``, the standard library.

        Only inferred roots that sit inside another root are affected;
        a declared root is never demoted.
        """
        for root in self.roots:
            if root in self.declared:
                continue
            if not any(root != o and root.is_relative_to(o) for o in self.roots):
                continue  # nothing to fall back to; demoting would say nothing
            if any(not f.resolve().is_relative_to(root) for f in evidence.get(root.name, ())):
                self._demoted.add(root)

    def _scan(self, root: pathlib.Path, directory: pathlib.Path) -> None:
        for child in sorted(directory.iterdir()):
            if child.name == "__pycache__" or child.name.startswith("."):
                continue
            if directory == root and child.name in discover.ALWAYS_SKIP_DIRS and child.is_dir():
                continue  # only at a root: see the module docstring
            if child.is_dir() and _is_pkg_dir(child):
                dotted = self._dotted(root, child)
                self._packages.add(dotted)
                self._note_child(dotted)
                init = child / "__init__.py"
                if init.is_file():
                    self._inits.setdefault(dotted, []).append(init)
                    self._sources.setdefault(dotted, []).append(init)
                else:
                    self._sourceless.add(dotted)
                self._scan(root, child)
            elif _is_importable_file(child):
                stem = _module_stem(child)
                if stem and stem != "__init__":
                    dotted = self._dotted(root, child.with_name(stem))
                    self._modules.add(dotted)
                    self._note_child(dotted)
                    if child.suffix == ".py":
                        self._sources.setdefault(dotted, []).append(child)
                    else:
                        self._sourceless.add(dotted)

    def _note_child(self, dotted: str) -> None:
        package, _, leaf = dotted.rpartition(".")
        if package:
            self._children.setdefault(package, set()).add(leaf)

    @staticmethod
    def _dotted(root: pathlib.Path, path: pathlib.Path) -> str:
        return ".".join(path.relative_to(root).parts)

    # -- queries -----------------------------------------------------------
    def is_first_party(self, dotted: str) -> bool:
        """True when *dotted*'s top-level component lives under an import root.

        Anything the scan found counts, and so does a bare file or directory
        of that name directly under a root -- a directory holding no Python
        still makes the name first-party rather than something to import and
        probe. The exception is a directory the scan skips on purpose, so a
        ``build/`` or ``dist/`` artifact at a root does not claim the name of
        the PyPI package ``build``.
        """
        top = dotted.split(".", 1)[0]
        if top in self._tops:
            return True
        for root in self.roots:
            candidate = root / top
            if (root / f"{top}.py").exists():
                return True
            if candidate.exists() and not (top in discover.ALWAYS_SKIP_DIRS and candidate.is_dir()):
                return True
        return False

    def classify(self, parent: str, name: str) -> model.Kind | None:
        """First-party answer, or ``None`` if ``parent`` is not first-party.

        `model.Kind.MODULE` when ``parent.name`` is on disk and ``parent``'s
        ``__init__`` does not also bind ``name`` (`model.Kind.AMBIGUOUS` when
        it does). Otherwise the answer comes from what ``parent``'s source
        binds ``name`` to, each binding followed to where it comes from (see
        `_lookup_in`): `model.Kind.OBJECT` or `model.Kind.MODULE` when every
        binding agrees, `model.Kind.DEFERRED` when some come from a
        third-party module only the probe can answer for (see `deferral`),
        and `model.Kind.UNDETERMINED` for everything else, with the reason in
        `unresolved_reason`. Missing is never an object.
        """
        if not self.is_first_party(parent):
            return None
        return self._verdict(parent, name).kind

    def unresolved_reason(self, parent: str, name: str) -> str:
        """Why `classify` said `model.Kind.UNDETERMINED`; empty for any other verdict."""
        return self._verdict(parent, name).reason

    def deferral(self, parent: str, name: str) -> Deferral | None:
        """What a `model.Kind.DEFERRED` verdict is waiting on; ``None`` otherwise."""
        return self._verdict(parent, name).deferral

    def _verdict(self, parent: str, name: str) -> _Verdict:
        key = (parent, name)
        if key not in self._verdicts:
            self._verdicts[key] = self._decide(parent, name)
        return self._verdicts[key]

    def _decide(self, parent: str, name: str) -> _Verdict:
        full = f"{parent}.{name}"
        if full in self._packages or full in self._modules:
            kind, why = self._submodule(parent, name)
            return _Verdict(kind, why if kind is model.Kind.UNDETERMINED else "")
        if parent not in self._packages and parent not in self._modules:
            return _Verdict(
                model.Kind.UNDETERMINED,
                f"'{parent}' is not a module or package on disk under this run's "
                "import roots, so nothing here shows what it binds",
            )
        self._evaluations = 0
        found = self._lookup(parent, name)
        if found.unknown:
            return _Verdict(
                model.Kind.UNDETERMINED,
                f"'{full}' is not on disk under this run's import roots, and {found.reason}",
            )
        if found.empty:
            return _Verdict(
                model.Kind.UNDETERMINED,
                f"'{full}' is neither on disk under this run's import roots "
                f"nor bound in '{parent}'",
            )
        if found.external:
            return _Verdict(
                model.Kind.DEFERRED,
                deferral=Deferral(found.kinds, tuple(sorted(found.external))),
            )
        if len(found.kinds) > 1:
            return _Verdict(model.Kind.UNDETERMINED, disagreement(parent, name))
        return _Verdict(model.Kind.MODULE if True in found.kinds else model.Kind.OBJECT)

    def _submodule(self, package: str, name: str) -> tuple[model.Kind, str]:
        """Verdict for ``package.name`` *on disk*: whether ``__init__`` leaves it alone.

        ``from package import name`` binds ``getattr(package, "name")`` and
        imports the submodule only when that attribute is absent, so the
        submodule is what it gets only if nothing in ``__init__`` binds the
        name first. `model.Kind.MODULE` when provably nothing does;
        `model.Kind.AMBIGUOUS` when something does -- a statement in the
        ``__init__`` itself, or a star import that brings the name in (``from
        ._impl import *`` where ``_impl`` defines ``helpers`` shadows
        ``pkg/helpers.py`` exactly as ``helpers = ...`` would);
        `model.Kind.UNDETERMINED` when that cannot be read: an ``__init__``
        that cannot be parsed, a star import from a module this run cannot
        read, a cycle of star imports back to this question. Reading any of
        those as "binds nothing" would call the submodule reachable on no
        evidence.
        """
        for init in self._inits.get(package, ()):
            bindings = _bindings.top_level_bindings(str(init), package)
            if bindings is None:
                return model.Kind.UNDETERMINED, (
                    f"'{init}' cannot be read or parsed, so whether it rebinds '{name}' is unknown"
                )
            if name in bindings:
                return model.Kind.AMBIGUOUS, (
                    f"'{package}.{name}' is both a submodule and bound in '{package}'"
                )
        return self._star_shadowing(package, name)

    def _star_shadowing(self, package: str, name: str) -> tuple[model.Kind, str]:
        """The star-import half of `_submodule`.

        "The star imports bind nothing, so the submodule is reachable" is a
        non-monotone step, exactly like the ones `_lookup` guards: read off a
        cycle cut short, "nothing" is not proof. So this takes part in the
        same low-link bookkeeping. It notes the depth it started at; asking
        the same question again while it is in progress is a cycle, and marks
        everything computed since as partial so none of it is memoised; and a
        "nothing" whose evaluation reached anything still in progress is
        `_CIRCULAR`, not `model.Kind.MODULE`. A binding found is still a
        binding -- the star union only grows -- so ``AMBIGUOUS`` stands.
        """
        key = (package, name)
        full = f"{package}.{name}"
        if key in self._shadow_checks:
            self._low = min(self._low, self._shadow_checks[key] - 1)
            return model.Kind.UNDETERMINED, f"'{full}' is a submodule, and {_CIRCULAR}"
        self._shadow_checks[key] = len(self._active)
        outer, self._low = self._low, _NO_BACK_EDGE
        found = _NOTHING
        try:
            for init in self._inits.get(package, ()):
                ns = _bindings.namespace(str(init))
                for level, target in ns.stars if ns is not None else ():
                    found |= self._via_star(package, _anchor(package, level, target), name)
        finally:
            del self._shadow_checks[key]
        inner = self._low
        self._low = min(outer, inner)
        if found.unknown:
            return model.Kind.UNDETERMINED, (
                f"'{full}' is a submodule, but {found.reason}, so whether a star import "
                "shadows it is unknown"
            )
        if not found.empty:
            return model.Kind.AMBIGUOUS, (
                f"'{full}' is both a submodule and bound in '{package}' by a star import"
            )
        if inner != _NO_BACK_EDGE:
            return model.Kind.UNDETERMINED, (
                f"'{full}' is a submodule, and whether a star import shadows it: {_CIRCULAR}"
            )
        return model.Kind.MODULE, ""

    def _no_source(self, module: str) -> str:
        if self._sources.get(module):
            return f"some file claiming '{module}' has no source to read"
        if module in self._packages:
            return f"'{module}' is a namespace package, with no __init__.py to read"
        if module in self._modules:
            return f"'{module}' is an extension module, with no source to read"
        return f"'{module}' is not on disk under this run's import roots"

    def _lookup(self, module: str, name: str) -> _Evidence:
        """What ``module.name`` can hold once *module* has run, read from its source.

        Every file claiming *module* is asked (see `is_reexport` for why) and
        they must agree.

        Star-import and re-export chains can loop, and can reach the same
        module along many paths -- a diamond of star imports is exponential to
        walk naively. Arriving at a lookup that is still in progress is a
        cycle, and the loop back is cut: it returns nothing. This is Tarjan's
        low-link bookkeeping. A lookup that reached nothing still in progress
        above it (the root of its strongly connected component, or not in a
        cycle at all) is final and memoised; one that did is partial, and is
        not.

        Cutting is only sound where combining answers is *monotone*: the star
        union, where a loop back can only re-add what the evaluation in
        progress already counts. Every step that is not -- "nothing binds it,
        so the fallback applies" (`_via_from`, a listed-but-unbound
        ``__all__`` name, a PEP 562 ``__getattr__``, "no star import shadows
        this submodule" in `_star_shadowing`) or "these files agree" --
        would read a cut as a real absence and answer differently depending
        on where the cycle happened to be entered. So each such step asks
        whether its inputs reached anything in progress (`_tracked`) and, if
        they did, answers `_CIRCULAR` instead. Reaching something in progress
        is a property of the cycle, not of the entry point: every lookup in a
        cycle reaches back into it however it is entered, and once the cycle
        is complete its root's memoised answer carries the verdict (unknown
        is absorbing) to every later question. So the answer does not depend
        on the order questions are asked in.

        Chains are followed at most `_MAX_DEPTH` deep and a question may cost
        at most `_MAX_EVALUATIONS` evaluations; past either, the answer is
        undetermined and nothing computed on the way is memoised. That limit
        is the one place order can still matter: whether a question hits it
        depends on how much earlier questions already memoised, so a verdict
        near the limit may differ between runs that ask different questions
        -- but only between undetermined and the correct answer, never
        between two answers.
        """
        key = (module, name)
        if key in self._memo:
            return self._memo[key]
        if key in self._active:
            self._low = min(self._low, self._active[key])
            return _NOTHING
        if len(self._active) >= _MAX_DEPTH or self._evaluations >= _MAX_EVALUATIONS:
            self._low = _TRUNCATED
            return _Evidence(
                unknown="the chain of re-exports and star imports behind it is too long or "
                "too tangled to follow"
            )
        self._evaluations += 1
        depth = self._active[key] = len(self._active)
        outer, self._low = self._low, _NO_BACK_EDGE
        try:
            found = self._evaluate(module, name)
        finally:
            del self._active[key]
        inner = self._low
        if inner >= depth:
            # Final: this lookup closed every cycle it took part in.
            self._memo[key] = found
            inner = _NO_BACK_EDGE
        self._low = min(outer, inner)
        return found

    def _tracked(self, module: str, name: str) -> tuple[_Evidence, bool]:
        """`_attribute`, plus whether it reached a lookup still in progress."""
        outer, self._low = self._low, _NO_BACK_EDGE
        found = self._attribute(module, name)
        inner = self._low
        self._low = min(outer, inner)
        return found, inner != _NO_BACK_EDGE

    def _evaluate(self, module: str, name: str) -> _Evidence:
        sources = self._sources.get(module, [])
        if module in self._sourceless or not sources:
            return _Evidence(unknown=self._no_source(module))
        if len(sources) == 1:
            return self._lookup_in(sources[0], module, name)[0]
        answers = {self._lookup_in(source, module, name) for source in sources}
        if any(touched for _, touched in answers):
            return _Evidence(unknown=_CIRCULAR, via=f"'{module}' is claimed by several files")
        if len(answers) == 1:
            return answers.pop()[0]
        return _Evidence(unknown=f"the files claiming '{module}' disagree about '{name}'")

    def _lookup_in(self, source: pathlib.Path, module: str, name: str) -> tuple[_Evidence, bool]:
        """`_lookup` for one file claiming *module*: every binding of *name* in it.

        * A ``def``, a ``class`` or an assignment binds an object; a plain
          ``import X as NAME`` binds a module.
        * ``from M import X as NAME`` binds whatever ``M.X`` is, so it is
          followed there (`_via_from`): a submodule on disk is a module, a
          first-party ``M`` is read the same way, recursively, and a
          third-party ``M`` is left for the probe. That includes ``from .
          import NAME``, which binds the submodule ``NAME`` -- one this run
          cannot see, when it is not on disk, and so never evidence of an
          object.
        * ``from M import *`` is followed into a first-party ``M`` (see
          `_via_star`).

        A PEP 562 ``__getattr__`` is consulted only when nothing binds the
        name, so it only matters then. Also returns whether any of this
        reached a lookup still in progress.
        """
        ns = _bindings.namespace(str(source))
        if ns is None:
            return _Evidence(unknown=f"'{source}' cannot be parsed"), False
        package = module if source.name == "__init__.py" else module.rpartition(".")[0]
        outer, self._low = self._low, _NO_BACK_EDGE
        found = _NOTHING
        if name in ns.defined:
            found |= _OBJECT
        if name in ns.module_imports:
            found |= _MODULE
        for level, origin, original in ns.from_imports.get(name, ()):
            found |= self._via_from(module, name, _anchor(package, level, origin), original)
        for level, target in ns.stars:
            found |= self._via_star(module, _anchor(package, level, target), name)
        inner = self._low
        self._low = min(outer, inner)
        touched = inner != _NO_BACK_EDGE
        if ns.has_getattr and not found.unknown and (found.empty or touched):
            getattr_hook = f"'{module}' defines a module-level __getattr__ (PEP 562)"
            if touched:
                return _Evidence(unknown=_CIRCULAR, via=getattr_hook), touched
            return _Evidence(unknown=f"{getattr_hook} that may supply it"), touched
        return found, touched

    def _via_from(self, module: str, name: str, origin: str | None, original: str) -> _Evidence:
        """What ``from ORIGIN import ORIGINAL [as NAME]`` inside *module* binds.

        When ``ORIGIN`` does not bind ``ORIGINAL``, the import falls back to
        the submodule ``ORIGIN.ORIGINAL`` -- so "nothing there" is not an
        empty answer but an undetermined one, and that is exactly the step a
        cycle must not be allowed to fake (see `_lookup`).
        """
        if origin is None:
            return _Evidence(
                unknown=f"'{module}' has a relative import that climbs above its top-level package"
            )
        if not self.is_first_party(origin):
            return _Evidence(external=frozenset({(origin, original)}))
        submodule = f"{origin}.{original}"
        on_disk = submodule in self._packages or submodule in self._modules
        own = f"'{module}' binds '{name}' by importing its own submodule"
        if (origin, original) == (module, name) and not on_disk:
            # ``from . import NAME`` in the package's own ``__init__``.
            return _Evidence(unknown=f"{own} of that name")
        prefix = f"'{module}' imports '{original}' from '{origin}'"
        found, touched = self._tracked(origin, original)
        if found.unknown:
            return found.through(prefix)
        if touched:
            return _Evidence(unknown=_CIRCULAR, via=prefix)
        if found.empty:
            if origin == module:
                return _Evidence(unknown=f"{own} '{original}', which is not on disk")
            return _Evidence(
                unknown=f"{prefix}, which neither binds it nor has a submodule of that name on disk"
            )
        return found

    def _via_star(self, module: str, target: str | None, name: str) -> _Evidence:
        """What ``from TARGET import *`` inside *module* binds under *name*.

        Only a first-party *target* with source can be followed. What it
        exports is its static ``__all__`` when it has one, its public names
        when it has none, and unknowable when ``__all__`` is built at runtime.
        A name ``__all__`` lists but the target does not bind is imported as
        the submodule of that name, so it is not simply absent.
        """
        if target is None:
            return _Evidence(
                unknown=f"'{module}' has a relative star import that climbs above its top-level "
                "package"
            )
        prefix = f"'{module}' star-imports from '{target}'"
        if not self.is_first_party(target):
            return _Evidence(
                unknown=f"{prefix}, which is not first-party, so this run cannot read it"
            )
        sources = self._sources.get(target, [])
        if target in self._sourceless or not sources:
            return _Evidence(unknown=f"{prefix}, and {self._no_source(target)}")
        answers: set[_Evidence] = set()
        partial = False
        for source in sources:
            ns = _bindings.namespace(str(source))
            if ns is None or ns.all_dynamic:
                return _Evidence(
                    unknown=f"{prefix}, whose __all__ cannot be read without running it"
                )
            listed = ns.all_names is not None and name in ns.all_names
            exported = listed if ns.all_names is not None else name[:1] != "_"
            found, touched = self._tracked(target, name) if exported else (_NOTHING, False)
            if listed and not found.unknown and (found.empty or touched):
                found = _Evidence(
                    unknown=_CIRCULAR
                    if touched
                    else f"its __all__ lists '{name}' without binding it, so the star import "
                    "would import a submodule that is not on disk"
                )
            answers.add(found)
            partial = partial or touched
        if partial and len(sources) > 1:
            return _Evidence(unknown=_CIRCULAR, via=f"{prefix}, which is claimed by several files")
        if len(answers) != 1:
            return _Evidence(unknown=f"{prefix}, and the files claiming it disagree about '{name}'")
        found = answers.pop()
        if found.unknown:
            return found.through(prefix)
        return found

    def _attribute(self, module: str, name: str) -> _Evidence:
        """``module.name`` as an import of it sees it: the submodule on disk, or `_lookup`."""
        full = f"{module}.{name}"
        if full in self._packages or full in self._modules:
            kind, why = self._submodule(module, name)
            return _MODULE if kind is model.Kind.MODULE else _Evidence(unknown=why)
        return self._lookup(module, name)

    def submodules(self, dotted: str) -> frozenset[str]:
        """Leaf names of the modules and subpackages directly under *dotted*.

        Empty for anything that is not a package on disk, which is what makes
        this safe to ask about any file: a plain module has no children, so
        the answer is "nothing to avoid".

        The caller is the fixer, and what it needs this for is that inside
        ``P/__init__.py`` a module-level name *is* an attribute of ``P``. A
        binding the fixer introduces there under the name of one of ``P``'s
        own submodules occupies that submodule's attribute slot until the
        first `import P.SUB` anywhere replaces it -- silently, and long after
        the rewrite. See `rewrite._Fixer._allocate_token`.

        Only the filesystem is consulted, so a submodule that exists solely in
        another namespace-package portion outside the analysed roots is not
        listed. That under-approximates, which is the one direction this
        cannot be conservative in; it is the same boundary every other
        first-party answer has.
        """
        return frozenset(self._children.get(dotted, ()))

    def is_reexport(self, parent: str, name: str) -> bool:
        """True when first-party ``parent`` only *re-exports* ``name``.

        That is: ``parent`` binds ``name`` by importing it from somewhere
        else rather than defining it, so ``parent.name`` exists only as long
        as that import keeps its current shape -- and this tool may be about
        to change it, in the very same run.

        Answers only for first-party modules we can read the source of, and
        that is the whole point rather than a limitation: a third-party
        ``parent`` is never rewritten, so its re-exports do not move. The
        hazard exists exactly where the fixer's reach does.

        When more than one file on disk claims ``parent`` -- ``pkg/`` beside
        ``pkg.py``, which is what a stale flat module left next to a newer
        package looks like -- *every* claimant is asked and any yes wins.
        Which of them an interpreter imports is a ``sys.path`` question this
        map cannot answer, so answering for one of them is a guess, and the
        guess is unsafe in one direction only: reading the wrong file says
        "not a re-export", the guard stands down, and the rewrite deletes an
        attribute another file imports. Saying yes for a file that does not
        win costs a fix that was safe; saying no for one that does costs
        working code. A claimant that cannot be read or parsed answers yes, for
        the same reason.
        """
        return any(
            names is None or name in names
            for names in (
                _bindings.import_bound_names(str(source))
                for source in self._sources.get(parent, ())
            )
        )

    def qualname_for(self, path: pathlib.Path, relative_level: int = 0) -> str | None:
        """Dotted module name for a source file, for relative-import resolution.

        Roots routinely nest -- a ``src/`` layout plus a ``tests/__init__.py``
        infers both ``src`` and the repo root -- and only one of them is
        actually on ``sys.path`` for this file. Picking the wrong one produces
        a dotted name that does not exist at runtime, and every relative
        import in the file is classified under it. (``--fix`` once wrote that
        name into the file, too: code that compiles and raises
        ``ModuleNotFoundError``. A relative import's replacement is now
        relative, so it no longer does.) Candidates are ranked by three rules, in
        order:

        1. **The file's own relative-import depth is a floor.** A file whose
           deepest relative import is ``from ..x import y`` (*level* 2) cannot
           be a top-level module: Python requires it to sit at least *level*
           packages deep, so any root that would give it fewer components is
           impossible. This matters for PEP 420 namespace packages, where
           `_root_for` stops its upward walk at the namespace directory and so
           infers a bogus root one level too deep -- the source text is
           evidence about the file's position that the directory tree alone
           does not carry.
        1b. **A root another file imports as a package is not a root.** See
           `demote_roots`; this is the same kind of evidence as rule 1, taken
           from a file other than this one.
        2. **A declared root outranks an inferred one.** ``--root src`` is the
           user telling us the answer; inferring past it is never right.
        3. **The most specific (deepest) root wins** among what is left. That
           is what keeps a ``src`` layout from being qualified against the repo
           root as ``src.mypkg.consumer``.

        If rules 1 and 1b leave nothing, the file has an unanchorable relative
        import; the best-ranked candidate is returned anyway so the import is
        reported as CP002 rather than vanishing.
        """
        anchor = self._anchor(path, relative_level)
        return None if anchor is None else anchor[1]

    def root_for_absolute_spelling(
        self, path: pathlib.Path, relative_level: int = 0
    ) -> pathlib.Path | None:
        """The root *path* is anchored in, when it could vouch for ``import pkg``.

        ``from . import C`` in a top-level package ``pkg`` has no relative
        replacement, and the absolute ``import pkg`` is only as good as the
        claim that this root is on ``sys.path`` (`_imports.module_import_spelling`).
        An inferred root is a reading of the directory tree, and never makes
        that claim; a declared one is the user making it -- but only a root
        that is not self-evidently wrong is taken at its word. So this is the
        winning root of `qualname_for` when *all* of these hold, and ``None``
        otherwise:

        * the file really anchors there (rules 1 and 1b of `qualname_for`);
        * the root is not itself a package directory (no ``__init__.py``):
          ``--root src/pkg`` puts a package's *inside* on ``sys.path``, and
          ``import sub`` there can name some other top-level ``sub``;
        * it neither nests inside nor contains another root, declared or
          inferred -- the case `_nesting_warnings` already warns about, where
          the same file has two dotted names;
        * no other root holds a top-level ``pkg`` too, so ``import pkg`` has
          one candidate, not a ``sys.path`` race;
        * the file is not ``pkg``'s own ``__init__``, where ``import pkg``
          would only bind the package to a name inside itself.

        The caller decides what to do with an inferred root that passes:
        `project` offers it in the `CP003` message as the root that, if
        declared, would lift the finding, and uses a declared one to allow
        the spelling.
        """
        anchor = self._anchor(path, relative_level)
        if anchor is None or not anchor[2]:
            return None
        root, dotted, _usable = anchor
        top = dotted.split(".", 1)[0]
        if not top or (path.name == "__init__.py" and "." not in dotted):
            return None
        if (root / "__init__.py").is_file():
            return None
        for other in self.roots:
            if other == root or other in self._demoted:
                continue
            if other.is_relative_to(root) or root.is_relative_to(other):
                return None
            if (other / top).is_dir() or any(
                _is_importable_file(child) and _module_stem(child) == top
                for child in other.glob(f"{top}.*")
            ):
                return None
        return root

    def _anchor(
        self, path: pathlib.Path, relative_level: int
    ) -> tuple[pathlib.Path, str, bool] | None:
        """``(root, dotted name, usable)`` for *path* by the rules in `qualname_for`."""
        path = path.resolve()
        best: tuple[tuple[bool, int], pathlib.Path, str] | None = None
        chosen: tuple[tuple[bool, int], pathlib.Path, str] | None = None
        for root in self.roots:
            try:
                rel = path.relative_to(root)
            except ValueError:
                continue
            parts = list(rel.with_suffix("").parts)
            # Rule 1: `__init__` counts as the component a relative import
            # anchors on, so it is counted here and dropped afterwards.
            usable = len(parts) > relative_level and root not in self._demoted
            if parts and parts[-1] == "__init__":
                parts.pop()
            rank = (root in self.declared, len(root.parts))  # rules 2 then 3
            candidate = (rank, root, ".".join(parts))
            if best is None or rank > best[0]:
                best = candidate
            if usable and (chosen is None or rank > chosen[0]):
                chosen = candidate
        if chosen is not None:
            return chosen[1], chosen[2], True
        return None if best is None else (best[1], best[2], False)

"""First-party module discovery from the filesystem (no imports, no side effects).

Given the paths under analysis we infer the *import roots* (directories that sit
on ``sys.path`` for these files) and enumerate every dotted name that is a
package or module. That lets us:

* classify ``from PARENT import NAME`` for first-party ``PARENT`` with certainty
  (is ``PARENT.NAME`` a directory/``.py`` under the tree?), and
* compute a file's dotted module name so relative imports (``from . import x``,
  ``from .a.b import Y``) can be resolved to an absolute ``PARENT``.

Namespace packages (PEP 420, no ``__init__.py``) are treated as packages when a
directory contains any Python submodules/subpackages, including extension
modules.

Roots are inferred per path and then ranked against each other -- declared
roots outrank inferred ones, deeper outranks shallower, and a root another file
has shown to be a package is demoted out of the running. See `qualname_for` and
`demote_roots`, where the rules and the cases that forced them are written out.
"""

from __future__ import annotations

import pathlib

from cleanporter import _bindings, model

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
        for root in self.roots:
            self._scan(root, root)

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
        .readers import read`` in that ``__init__.py`` is rewritten to ``from
        io import readers`` -- the standard library.

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
            if child.is_dir() and _is_pkg_dir(child):
                dotted = self._dotted(root, child)
                self._packages.add(dotted)
                self._note_child(dotted)
                init = child / "__init__.py"
                if init.is_file():
                    self._inits.setdefault(dotted, []).append(init)
                    self._sources.setdefault(dotted, []).append(init)
                self._scan(root, child)
            elif _is_importable_file(child):
                stem = _module_stem(child)
                if stem and stem != "__init__":
                    dotted = self._dotted(root, child.with_name(stem))
                    self._modules.add(dotted)
                    self._note_child(dotted)
                    if child.suffix == ".py":
                        self._sources.setdefault(dotted, []).append(child)

    def _note_child(self, dotted: str) -> None:
        package, _, leaf = dotted.rpartition(".")
        if package:
            self._children.setdefault(package, set()).add(leaf)

    @staticmethod
    def _dotted(root: pathlib.Path, path: pathlib.Path) -> str:
        return ".".join(path.relative_to(root).parts)

    # -- queries -----------------------------------------------------------
    def is_first_party(self, dotted: str) -> bool:
        top = dotted.split(".", 1)[0]
        return any(top == p.split(".", 1)[0] for p in self._packages | self._modules) or any(
            (root / top).exists() or (root / f"{top}.py").exists() for root in self.roots
        )

    def classify(self, parent: str, name: str) -> model.Kind | None:
        """First-party answer, or ``None`` if ``parent`` is not first-party."""
        if not self.is_first_party(parent):
            return None
        full = f"{parent}.{name}"
        on_disk = full in self._packages or full in self._modules
        shadowed = any(
            name in _bindings.top_level_bindings(str(init), parent)
            for init in self._inits.get(parent, ())
        )
        if on_disk and shadowed:
            return model.Kind.AMBIGUOUS
        if on_disk:
            return model.Kind.MODULE
        return model.Kind.OBJECT

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
        working code.
        """
        return any(
            name in _bindings.import_bound_names(str(source))
            for source in self._sources.get(parent, ())
        )

    def qualname_for(self, path: pathlib.Path, relative_level: int = 0) -> str | None:
        """Dotted module name for a source file, for relative-import resolution.

        Roots routinely nest -- a ``src/`` layout plus a ``tests/__init__.py``
        infers both ``src`` and the repo root -- and only one of them is
        actually on ``sys.path`` for this file. Picking the wrong one produces
        a dotted name that does not exist at runtime, which ``--fix`` then
        writes into the file: code that compiles and raises
        ``ModuleNotFoundError``. Candidates are ranked by three rules, in
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
        path = path.resolve()
        best: tuple[tuple[bool, int], str] | None = None
        chosen: tuple[tuple[bool, int], str] | None = None
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
            candidate = (rank, ".".join(parts))
            if best is None or rank > best[0]:
                best = candidate
            if usable and (chosen is None or rank > chosen[0]):
                chosen = candidate
        winner = chosen or best
        return None if winner is None else winner[1]

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
"""

from __future__ import annotations

from pathlib import Path

from ._bindings import top_level_bindings
from .model import Kind

#: Suffixes CPython will import as an extension module.
EXTENSION_SUFFIXES = frozenset({".so", ".pyd"})


def _module_stem(path: Path) -> str:
    """``accel.cpython-314-x86_64-linux-gnu.so`` -> ``accel``."""
    return path.name.split(".")[0]


def _is_importable_file(path: Path) -> bool:
    return path.suffix == ".py" or path.suffix in EXTENSION_SUFFIXES


def _is_pkg_dir(d: Path) -> bool:
    if (d / "__init__.py").is_file():
        return True
    # PEP 420 namespace package: a directory that contributes submodules.
    return d.is_dir() and any(
        _is_importable_file(c) or (c.is_dir() and c.name != "__pycache__")
        for c in d.iterdir()
    )


def _root_for(path: Path) -> Path:
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


class ModuleMap:
    """Enumerates first-party packages/modules under a set of import roots."""

    def __init__(self, roots: list[Path]) -> None:
        self.roots = [r.resolve() for r in roots]
        self._modules: set[str] = set()  # dotted names of .py modules
        self._packages: set[str] = set()  # dotted names of packages
        self._inits: dict[str, Path] = {}  # dotted package -> its __init__.py
        for root in self.roots:
            self._scan(root, root)

    @classmethod
    def from_paths(cls, paths: list[Path]) -> ModuleMap:
        roots: set[Path] = set()
        for p in paths:
            roots.add(_root_for(p.resolve()))
        return cls(sorted(roots))

    def _scan(self, root: Path, directory: Path) -> None:
        for child in sorted(directory.iterdir()):
            if child.name == "__pycache__" or child.name.startswith("."):
                continue
            if child.is_dir() and _is_pkg_dir(child):
                dotted = self._dotted(root, child)
                self._packages.add(dotted)
                init = child / "__init__.py"
                if init.is_file():
                    self._inits[dotted] = init
                self._scan(root, child)
            elif _is_importable_file(child):
                stem = _module_stem(child)
                if stem and stem != "__init__":
                    self._modules.add(self._dotted(root, child.with_name(stem)))

    @staticmethod
    def _dotted(root: Path, path: Path) -> str:
        return ".".join(path.relative_to(root).parts)

    # -- queries -----------------------------------------------------------
    def is_first_party(self, dotted: str) -> bool:
        top = dotted.split(".", 1)[0]
        return any(top == p.split(".", 1)[0] for p in self._packages | self._modules) or any(
            (root / top).exists() or (root / f"{top}.py").exists() for root in self.roots
        )

    def classify(self, parent: str, name: str) -> Kind | None:
        """First-party answer, or ``None`` if ``parent`` is not first-party."""
        if not self.is_first_party(parent):
            return None
        full = f"{parent}.{name}"
        on_disk = full in self._packages or full in self._modules
        init = self._inits.get(parent)
        shadowed = init is not None and name in top_level_bindings(str(init))
        if on_disk and shadowed:
            return Kind.AMBIGUOUS
        if on_disk:
            return Kind.MODULE
        return Kind.OBJECT

    def qualname_for(self, path: Path) -> str | None:
        """Dotted module name for a source file, for relative-import resolution."""
        path = path.resolve()
        for root in self.roots:
            try:
                rel = path.relative_to(root)
            except ValueError:
                continue
            parts = list(rel.with_suffix("").parts)
            if parts and parts[-1] == "__init__":
                parts.pop()
            return ".".join(parts)
        return None

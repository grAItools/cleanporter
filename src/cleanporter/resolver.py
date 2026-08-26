"""Hybrid module/symbol resolution for cleanporter.

Decides whether ``from P import S`` imports a *module* (allowed) or an
ordinary object (style-guide violation). Two layers:

1. Static: walk project source roots and site-packages on disk looking for
   submodules (``S.py``, ``S/__init__.py``, namespace dirs, extension
   modules) and statically bound names in the target module's top level.
2. Runtime fallback (optional): import the parent module and check whether
   the attribute is actually a module. Needed for lazy ``__getattr__``
   exports, stdlib aliases like ``os.path``, and C extensions.
"""

from __future__ import annotations

import ast
import inspect
import importlib
import sys
import sysconfig
from dataclasses import dataclass
from enum import Enum, auto
from functools import lru_cache
from pathlib import Path


class SymbolKind(Enum):
    MODULE = auto()
    OBJECT = auto()
    UNRESOLVABLE = auto()


class Origin(Enum):
    FIRST_PARTY = auto()
    THIRD_PARTY = auto()
    STDLIB = auto()
    UNKNOWN = auto()


@dataclass(frozen=True)
class Classification:
    kind: SymbolKind
    origin: Origin
    note: str | None = None

    @property
    def is_violation(self) -> bool:
        return self.kind is SymbolKind.OBJECT


@dataclass(frozen=True)
class _ModuleArtifact:
    """Where a dotted module name resolved to."""

    kind: str  # "file" | "package" | "namespace" | "extension"
    path: Path | None  # file, package dir (with __init__), or ns dir
    origin: Origin


_EXTENSION_SUFFIXES = {".so", ".pyd"}


def _looks_like_extension(path: Path) -> bool:
    return path.suffix in _EXTENSION_SUFFIXES


def _submodule_fs_hits(package_dir: Path, symbol: str) -> bool:
    """True if *symbol* exists as a (potentially importable) child module."""
    if (package_dir / f"{symbol}.py").is_file():
        return True
    if (package_dir / symbol / "__init__.py").is_file():
        return True
    if (package_dir / symbol).is_dir():
        # Namespace-style subpackage with no __init__.py still imports.
        return any((package_dir / symbol).iterdir())
    for entry in package_dir.glob(f"{symbol}.*"):
        if _looks_like_extension(entry):
            return True
    return False


@lru_cache(maxsize=1024)
def _top_level_bindings(path: str) -> tuple[frozenset[str], bool, bool]:
    """Return (bound names, has star-import, has __getattr__) for a file."""
    try:
        tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return frozenset(), False, False

    names: set[str] = set()
    has_star = False
    has_getattr = False

    def scan_body(body: list[ast.stmt]) -> None:
        nonlocal has_star, has_getattr
        for stmt in body:
            match stmt:
                case ast.FunctionDef() | ast.AsyncFunctionDef() as fn:
                    names.add(fn.name)
                    if fn.name == "__getattr__":
                        has_getattr = True
                case ast.ClassDef() as cls:
                    names.add(cls.name)
                case ast.Assign():
                    for target in stmt.targets:
                        names.update(n.id for n in ast.walk(target) if isinstance(n, ast.Name))
                case ast.AnnAssign():
                    if isinstance(stmt.target, ast.Name):
                        names.add(stmt.target.id)
                case ast.AugAssign():
                    if isinstance(stmt.target, ast.Name):
                        names.add(stmt.target.id)
                case ast.Import():
                    for alias in stmt.names:
                        names.add(alias.asname or alias.name.split(".")[0])
                case ast.ImportFrom():
                    for alias in stmt.names:
                        if alias.name == "*":
                            has_star = True
                        else:
                            names.add(alias.asname or alias.name)
                case ast.If():
                    scan_body(stmt.body)
                    scan_body(stmt.orelse)
                case ast.Try() | ast.TryStar():
                    scan_body(stmt.body)
                    scan_body(stmt.orelse)
                    scan_body(stmt.finalbody)
                    for handler in stmt.handlers:
                        scan_body(handler.body)
                case ast.With():
                    scan_body(stmt.body)

    scan_body(tree.body)
    return frozenset(names), has_star, has_getattr


class Resolver:
    """Shared resolver instance used by both checker and fixer."""

    def __init__(
        self,
        project_roots: list[Path],
        runtime_fallback: bool = True,
        extra_roots: list[tuple[Path, Origin]] | None = None,
    ) -> None:
        self._roots: list[tuple[Path, Origin]] = [
            (root.resolve(), Origin.FIRST_PARTY) for root in project_roots
        ]
        self._roots.extend(extra_roots or [])
        self._runtime_fallback = runtime_fallback
        self._artifact_cache: dict[str, _ModuleArtifact | None] = {}
        self._probe_cache: dict[tuple[str, str], SymbolKind | None] = {}
        self._syspath_prepared = False

    # ------------------------------------------------------------------ #
    # static resolution                                                   #
    # ------------------------------------------------------------------ #

    @staticmethod
    def default_site_packages() -> list[Path]:
        roots: list[Path] = []
        try:
            purelib = sysconfig.get_path("purelib")
            if purelib:
                roots.append(Path(purelib))
        except (KeyError, ValueError):
            pass
        for entry in sys.path:
            candidate = Path(entry)
            if (
                candidate.is_dir()
                and candidate.name in {"site-packages", "dist-packages"}
                and candidate not in roots
            ):
                roots.append(candidate)
        return roots

    def resolve_module(self, dotted: str) -> _ModuleArtifact | None:
        if dotted in self._artifact_cache:
            return self._artifact_cache[dotted]
        artifact = self._resolve_uncached(dotted)
        self._artifact_cache[dotted] = artifact
        return artifact

    def _resolve_toplevel(self, part: str) -> _ModuleArtifact | None:
        for root, origin in self._roots:
            candidate = root / f"{part}.py"
            if candidate.is_file():
                return _ModuleArtifact("file", candidate, origin)
            pkg_init = root / part / "__init__.py"
            if pkg_init.is_file():
                return _ModuleArtifact("package", root / part, origin)
            ns_dir = root / part
            if ns_dir.is_dir():
                return _ModuleArtifact("namespace", ns_dir, origin)
            for entry in root.glob(f"{part}.*"):
                if _looks_like_extension(entry) and entry.is_file():
                    return _ModuleArtifact("extension", entry, origin)
        return None

    def _descend(self, parent: _ModuleArtifact, part: str) -> _ModuleArtifact | None:
        if parent.kind == "file":
            return None
        assert parent.path is not None
        base = parent.path
        candidate = base / f"{part}.py"
        if candidate.is_file():
            return _ModuleArtifact("file", candidate, parent.origin)
        child_pkg = base / part / "__init__.py"
        if child_pkg.is_file():
            return _ModuleArtifact("package", base / part, parent.origin)
        child_ns = base / part
        if child_ns.is_dir():
            return _ModuleArtifact("namespace", child_ns, parent.origin)
        for entry in base.glob(f"{part}.*"):
            if _looks_like_extension(entry) and entry.is_file():
                return _ModuleArtifact("extension", entry, parent.origin)
        return None

    def _resolve_uncached(self, dotted: str) -> _ModuleArtifact | None:
        parts = dotted.split(".")
        if not all(p.isidentifier() for p in parts):
            return None
        head = self._resolve_toplevel(parts[0])
        if head is None:
            return None
        current = head
        for part in parts[1:]:
            nxt = self._descend(current, part)
            if nxt is None:
                return None
            current = nxt
        return current

    def is_stdlib_top_level(self, top: str) -> bool:
        return top in getattr(sys, "stdlib_module_names", frozenset())

    # ------------------------------------------------------------------ #
    # classification                                                      #
    # ------------------------------------------------------------------ #

    def classify_absolute(self, prefix: str | None, symbol: str) -> Classification:
        """Classify ``from <prefix> import <symbol>``.

        ``prefix`` is the absolute dotted module path; ``None`` means it
        could not be anchored (e.g., unresolvable relative import).
        """
        if prefix is None or not symbol.isidentifier() or symbol == "*":
            return Classification(
                SymbolKind.UNRESOLVABLE,
                self._origin_for(prefix),
                "unanchored relative import" if prefix is None else None,
            )
        artifact = self.resolve_module(prefix)
        if artifact is None:
            # Static layout unknown (stdlib layout differs per install,
            # zipped wheels, ...). Honor the runtime fallback before giving up.
            probe = self._probe(prefix, symbol)
            if probe is SymbolKind.MODULE:
                return Classification(SymbolKind.MODULE, self._fallback_origin(prefix))
            if probe is SymbolKind.OBJECT:
                return Classification(
                    SymbolKind.OBJECT,
                    self._fallback_origin(prefix),
                    f"{symbol!r} of {prefix!r} resolves to a non-module object",
                )
            return Classification(
                SymbolKind.UNRESOLVABLE,
                self._fallback_origin(prefix),
                f"module {prefix!r} not found",
            )

        # Submodule on disk?
        fs_module = False
        if artifact.kind in ("package", "namespace") and artifact.path is not None:
            fs_module = _submodule_fs_hits(artifact.path, symbol)

        # Statically bound attribute?
        bound: bool | None = None
        has_star = False
        has_getattr = False
        source_path: Path | None = None
        if artifact.kind == "package":
            init = artifact.path / "__init__.py"
            if init.is_file():
                source_path = init
        elif artifact.kind == "file":
            source_path = artifact.path
        if source_path is not None:
            bindings, has_star, has_getattr = _top_level_bindings(str(source_path))
            bound = symbol in bindings

        # Ambiguous: both a submodule and an explicit binding in __init__.
        if fs_module and bound:
            return Classification(
                SymbolKind.UNRESOLVABLE,
                artifact.origin,
                f"{symbol!r} is both a submodule of {prefix!r} and bound in its __init__",
            )
        if fs_module:
            return Classification(SymbolKind.MODULE, artifact.origin)
        if artifact.kind == "namespace" and bound is None:
            # Namespace package: no __init__.py to inspect statically, but
            # Python still executes one implicitly -- let the runtime decide.
            return self._runtime_verdict(
                prefix,
                symbol,
                artifact.origin,
                default=SymbolKind.UNRESOLVABLE,
                note_override="namespace package lacks static bindings",
            )

        # Bound to a plain object (positive static evidence).
        if bound:
            return self._runtime_verdict(prefix, symbol, artifact.origin, default=SymbolKind.OBJECT)

        # Not bound anywhere statically. Possibilities: dynamic export
        # (__getattr__, C extension), star-import we cannot see through,
        # or a plain missing attribute.
        if has_star:
            return self._runtime_verdict(prefix, symbol, artifact.origin, default=SymbolKind.UNRESOLVABLE,
                                         note_override="star-import in module prevents static proof")
        if has_getattr or artifact.kind == "extension":
            return self._runtime_verdict(prefix, symbol, artifact.origin, default=SymbolKind.UNRESOLVABLE,
                                         note_override="dynamic exports (__getattr__/extension)")
        if bound is False and artifact.kind in ("file", "package"):
            # Definitively absent attribute: importing would raise
            # ImportError/AttributeError; still counts as a violation.
            return Classification(SymbolKind.OBJECT, artifact.origin, f"{symbol!r} does not exist in {prefix!r}")
        return Classification(SymbolKind.UNRESOLVABLE, artifact.origin, "could not prove either way")

    def _runtime_verdict(
        self,
        prefix: str,
        symbol: str,
        origin: Origin,
        default: SymbolKind,
        note_override: str | None = None,
    ) -> Classification:
        key = (prefix, symbol)
        if key not in self._probe_cache:
            self._probe_cache[key] = self._probe(prefix, symbol)
        probe = self._probe_cache[key]
        if probe is SymbolKind.MODULE:
            return Classification(SymbolKind.MODULE, origin)
        if probe is SymbolKind.OBJECT:
            return Classification(SymbolKind.OBJECT, origin)
        return Classification(default, origin, note_override)

    def _prepare_sys_path(self) -> None:
        """Expose first-party source roots on sys.path so dev checkouts can
        be imported by the runtime fallback without an installed package."""
        if self._syspath_prepared:
            return
        self._syspath_prepared = True
        for root, origin in self._roots:
            if origin is Origin.FIRST_PARTY and str(root) not in sys.path:
                sys.path.insert(0, str(root))

    def _probe(self, prefix: str, symbol: str) -> SymbolKind | None:
        """Runtime fallback: import the parent and inspect the attribute.

        Returns None when fallback disabled, import failed, or attribute
        absent (in which case the caller's default applies).
        """
        if not self._runtime_fallback:
            return None
        self._prepare_sys_path()
        try:
            module = importlib.import_module(prefix)
        except Exception:
            return None
        try:
            obj = getattr(module, symbol)
        except AttributeError:
            # Language fallback: `from P import S` imports P.S as a submodule
            # when P exposes no such attribute (namespace pkgs, lazy inits).
            try:
                importlib.import_module(f"{prefix}.{symbol}")
                return SymbolKind.MODULE
            except Exception:
                return None
        except Exception:
            return None
        return SymbolKind.MODULE if inspect.ismodule(obj) else SymbolKind.OBJECT

    # ------------------------------------------------------------------ #
    # origin helpers                                                      #
    # ------------------------------------------------------------------ #

    def _origin_for(self, prefix: str | None) -> Origin:
        if prefix is None:
            return Origin.FIRST_PARTY  # relative import => inside the project
        artifact = self.resolve_module(prefix)
        return artifact.origin if artifact else self._fallback_origin(prefix)

    def _fallback_origin(self, prefix: str) -> Origin:
        top = prefix.split(".")[0]
        if self.is_stdlib_top_level(top):
            return Origin.STDLIB
        return Origin.THIRD_PARTY


def discover_source_roots(config_root: Path, configured: tuple[str, ...]) -> list[Path]:
    """Determine first-party source roots.

    If configured explicitly ([tool.cleanporter].source_roots), use those.
    Otherwise scan standard locations: the src/ layout, direct packages at
    the repository root, and standalone .py modules at the root.
    """
    if configured:
        return [config_root / rel for rel in configured]

    roots: list[Path] = []
    src = config_root / "src"
    if src.is_dir():
        roots.append(src)

    _SKIP_DIRS = {
        "__pycache__",
        "node_modules",
        ".venv",
        "venv",
        "build",
        "dist",
        ".git",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
    }
    root_is_source = False
    try:
        children = sorted(config_root.iterdir())
    except OSError:
        children = []
    for child in children:
        if child.name.startswith(".") or child.name in _SKIP_DIRS:
            continue
        if child.is_dir() and (child / "__init__.py").is_file():
            root_is_source = True
            break
        if child.is_file() and child.suffix == ".py":
            root_is_source = True
            break
    if root_is_source and config_root not in roots:
        roots.append(config_root)
    return roots

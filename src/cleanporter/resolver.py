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
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from . import _probe
from .firstparty import ModuleMap
from .model import Kind

_AMBIGUOUS = "'{name}' is both a submodule of '{parent}' and bound in its __init__"
_NOT_IMPORTABLE = "'{parent}' is not importable in the target interpreter"


class Resolver:
    def __init__(self, module_map: ModuleMap, python: str | None = None) -> None:
        self._map = module_map
        self._python = python or sys.executable
        self._in_process = Path(self._python).resolve() == Path(sys.executable).resolve()
        self._cache: dict[tuple[str, str], bool | None] = {}
        self._notes: dict[tuple[str, str], str] = {}
        self._probe_path = str(Path(_probe.__file__).resolve())

    def _from_kind(self, key: tuple[str, str], kind: Kind) -> bool | None:
        if kind is Kind.MODULE:
            self._cache[key] = True
        elif kind is Kind.OBJECT:
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

    def reason(self, parent: str, name: str) -> str:
        """Human explanation for an unresolved (``None``) verdict."""
        key = (parent, name)
        return self._notes.get(key, _NOT_IMPORTABLE.format(parent=parent))

    def warm(self, pairs: list[tuple[str, str]]) -> None:
        """Classify a batch up front (one subprocess round-trip for the lot)."""
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
            proc = subprocess.run(
                [self._python, self._probe_path],
                input=json.dumps(pairs),
                capture_output=True,
                text=True,
                timeout=120,
            )
            if proc.returncode != 0:
                # Whole-batch failure -> everything undetermined (never guess).
                return {p: None for p in pairs}
            flat = json.loads(proc.stdout or "{}")
        out: dict[tuple[str, str], bool | None] = {}
        for parent, name in pairs:
            out[(parent, name)] = flat.get(f"{parent}\x00{name}")
        return out

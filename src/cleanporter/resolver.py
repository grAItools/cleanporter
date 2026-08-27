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
            out[(parent, name)] = flat.get(f"{parent}\x00{name}")
        return out

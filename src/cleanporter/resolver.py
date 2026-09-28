"""Module/object resolution, layered for correctness and safety.

Order of resolution for a ``from PARENT import NAME``:

1. **First-party, filesystem** (``firstparty.ModuleMap``): if ``PARENT`` is one
   of the packages under analysis, decide purely from the source tree -- no
   imports, no side effects, correct for namespace packages. A module on disk
   is a module; otherwise the answer is what the parent's own source binds
   the name to, followed to its origin. A first-party name with no such
   evidence is undetermined here and is **not** passed on to the probe: the
   probe would import first-party code, which this layer exists to avoid.
   The one hand-off is a first-party re-export of a *third-party* name
   (``from os import path`` in a package ``__init__``): the map defers, the
   origin ``(os, path)`` joins the probe batch, and `Resolver._settle`
   combines the answers.
2. **Stdlib / third-party, interpreter probe** (``_probe``): ask the target
   interpreter via ``importlib`` whether ``PARENT.NAME`` is a submodule. Only
   the parent package is imported (cached); never the leaf, never objects.
3. **Undetermined** -> ``None``. ``check`` reports it, ``fix`` skips it.

The interpreter probe runs in-process when ``python`` is provably the current
interpreter *environment* (see `_is_this_interpreter`), otherwise in a
subprocess so tool deps stay out of the target env and native-library crashes
are contained.

Probing imports third-party packages, and some print on import. In-process,
``sys.stdout`` is pointed at ``sys.stderr`` for the duration, so a banner
cannot land in a ``--diff`` patch on stdout (the stream contract in `cli`);
out of process, the reply is framed so a banner cannot corrupt it (`_probe`).
When an out-of-process batch fails anyway -- a crash, a timeout, no reply --
the batch is still undetermined, and *why* is kept in `take_warnings` for the
CLI to print, with the tail of the probe's stderr: the per-import reason ("not
importable in the target interpreter") is the only thing the user would
otherwise see, and it blames the packages rather than the probe.

Answers are cached per ``(parent, name)``, and `warm` classifies a whole batch
of pairs up front -- one subprocess round-trip for a run rather than one per
import.
"""

from __future__ import annotations

import contextlib
import json
import os
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

#: How much of a failed probe's stderr a warning quotes, from the end.
_STDERR_TAIL_LINES = 5
_STDERR_TAIL_CHARS = 600


def _stderr_tail(stderr: str | bytes | None) -> str:
    """The last few lines of a probe's stderr, one line, for a warning."""
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", "replace")
    lines = [line for line in (stderr or "").splitlines() if line.strip()]
    tail = " | ".join(lines[-_STDERR_TAIL_LINES:])
    if len(tail) > _STDERR_TAIL_CHARS:
        tail = "..." + tail[-_STDERR_TAIL_CHARS:]
    return f"; stderr: {tail}" if tail else ""


def _is_this_interpreter(python: str) -> bool:
    """Whether running *python* would give this very environment, provably.

    What an interpreter can import is decided by its *environment*, and a
    virtual environment is found from where its executable is invoked -- the
    ``pyvenv.cfg`` beside it or one level up -- not from the binary it links
    to. Every venv built from one base Python is a symlink to the same file,
    so comparing symlink-*resolved* paths, as this used to, called any other
    venv of the same Python "this interpreter" and probed it in process,
    against cleanporter's own ``sys.path``: a package only the target had was
    "not importable", and one only cleanporter's environment had was
    classified as if the target had it.

    So the comparison is of the path as invoked, made absolute without
    touching a symlink, against ``sys.executable``. Equal strings name the
    same location and so the same environment. The one lexical step that
    could break that is collapsing ``..`` -- ``venv/link/../bin/python``
    reads as ``venv/bin/python`` but, through a symlinked ``link``, runs
    something else -- so a path with a ``..`` component is never called equal.

    A bare command name (no path separator) is never equal either: the
    subprocess looks it up on ``PATH``, whereas making it absolute joins it to
    the cwd, so ``python3`` run from inside a venv's ``bin`` would be answered
    in process while ``PATH`` names some other interpreter entirely.
    Every miss costs a subprocess, never a wrong answer.
    """
    if not any(sep in python for sep in (os.sep, os.altsep) if sep):
        return False
    if not sys.executable or ".." in pathlib.PurePath(python).parts:
        return False
    return str(pathlib.Path(python).absolute()) == sys.executable


class Resolver:
    def __init__(self, module_map: firstparty.ModuleMap, python: str | None = None) -> None:
        self._map = module_map
        self._python = python or sys.executable
        self._in_process = python is None or _is_this_interpreter(python)
        self._cache: dict[tuple[str, str], bool | None] = {}
        #: Every ``(module, name)`` some analysed file *uses*. Populated by
        #: `analyze.build`; empty when the resolver is used standalone, which
        #: makes `is_load_bearing` answer False -- no evidence, no claim.
        self._uses: frozenset[tuple[str, str]] = frozenset()
        #: Modules some analysed file star-imports, which could need any name.
        self._star_imported: frozenset[str] = frozenset()
        self._notes: dict[tuple[str, str], str] = {}
        self._probe_path = str(pathlib.Path(_probe.__file__).resolve())
        #: Why each failed probe batch failed (kind + stderr tail; the
        #: interpreter is fixed per resolver) -> imports it left unresolved.
        self._failures: dict[str, int] = {}

    def _from_kind(self, key: tuple[str, str], kind: model.Kind) -> bool | None:
        if kind is model.Kind.MODULE:
            self._cache[key] = True
        elif kind is model.Kind.OBJECT:
            self._cache[key] = False
        elif kind is model.Kind.AMBIGUOUS:
            self._cache[key] = None
            self._notes[key] = _AMBIGUOUS.format(parent=key[0], name=key[1])
        elif kind is model.Kind.DEFERRED:
            return self._settle(key)
        else:
            # UNDETERMINED: first-party, but neither a submodule on disk nor
            # provably bound in the parent. The map knows which evidence was
            # missing, so its words are the note.
            self._cache[key] = None
            self._notes[key] = self._map.unresolved_reason(*key)
        return self._cache[key]

    def _settle(self, key: tuple[str, str]) -> bool | None:
        """Decide a `model.Kind.DEFERRED` pair from the probe's third-party answers.

        The parent binds the name from a module outside the analysed tree --
        ``from os import path`` in a first-party ``__init__`` -- so whatever
        the probe says about that origin is what the parent's attribute is.
        Combined with the first-party bindings by the map's own rule: one
        answer only when every binding gives the same one. The probe is asked
        only for origins `warm` has not already batched.
        """
        deferral = self._map.deferral(*key)
        pairs = deferral.pairs if deferral is not None else ()
        missing = [pair for pair in pairs if pair not in self._cache]
        if missing:
            self._cache.update(self._probe(missing))
        verdicts: set[bool] = set(deferral.local) if deferral is not None else set()
        for origin, original in pairs:
            answer = self._cache.get((origin, original))
            if answer is None:
                self._cache[key] = None
                self._notes[key] = (
                    f"'{key[0]}' binds '{key[1]}' by importing '{original}' from "
                    f"'{origin}', and {self.reason(origin, original)}"
                )
                return None
            verdicts.add(answer)
        if len(verdicts) == 1:
            self._cache[key] = verdicts.pop()
        else:
            self._cache[key] = None
            self._notes[key] = firstparty.disagreement(*key)
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

    def replacement_unreachable(self, parent: str, replacement: str | None = None) -> str | None:
        """Why the import the fixer would *write* for *parent* cannot be trusted.

        Returns the reason, or ``None`` when the replacement provably binds
        the module ``parent``. *replacement* is the statement as the fixer
        would spell it, for the message -- a relative import's replacement is
        relative (`_imports.module_import_spelling`) -- and defaults to the
        absolute ``from P import S``. The question asked is the same either
        way: both spellings bind ``getattr(P, 'S')``.

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
        the case where this run cannot see ``P.S`` at all. The map answers for
        a first-party *top-level* name whether or not it scanned the subtree,
        so a run pointed at one distribution of a namespace package cannot see
        a sibling's modules; it says so (`model.Kind.UNDETERMINED`, with the
        map's reason) rather than calling them objects. Declining costs a fix
        that would have been correct; the message says which evidence was
        missing, and pointing cleanporter at the whole tree (or declaring
        ``source_roots``) settles it.

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
            f"'{package}' binds '{token}' to something that is not a module"
            if verdict is False
            else self.reason(package, token)
        )
        spelled = replacement or f"from {package} import {token}"
        return f"the replacement '{spelled}' cannot be shown to bind the module '{parent}': {cause}"

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
        deferred: list[tuple[str, str]] = []
        for key in pairs:
            if key in self._cache:
                continue
            kind = self._map.classify(*key)
            if kind is None:
                pending.append(key)
            elif kind is model.Kind.DEFERRED:
                # A first-party re-export of a third-party name: its origin
                # joins this batch, so settling it costs no second round trip.
                deferred.append(key)
                deferral = self._map.deferral(*key)
                pending.extend(
                    p for p in (deferral.pairs if deferral else ()) if p not in self._cache
                )
            else:
                self._from_kind(key, kind)
        if pending:
            self._cache.update(self._probe(list(dict.fromkeys(pending))))
        for key in deferred:
            self._settle(key)

    def take_warnings(self) -> list[str]:
        """Warnings raised by probing since the last call, then forget them.

        One per *distinct* failure of the out-of-process probe, saying why it
        failed, with the imports it left unresolved summed across batches.
        After `warm`, each lookup it did not foresee probes on its own, so a
        broken interpreter fails once per lookup -- the same failure, which
        is worth saying once. The pairs are already reported undetermined;
        this is the explanation.
        """
        taken = [
            f"interpreter probe '{self._python}' {why}; {count} import(s) left unresolved"
            for why, count in self._failures.items()
        ]
        self._failures = {}
        return taken

    # -- interpreter probe -------------------------------------------------
    def _probe(self, pairs: list[tuple[str, str]]) -> dict[tuple[str, str], bool | None]:
        if not pairs:
            return {}
        if self._in_process:
            # Importing a parent runs its code; a package that prints on
            # import must not write into a patch on cleanporter's stdout.
            with contextlib.redirect_stdout(sys.stderr):
                flat: dict[str, object] = dict(_probe.classify_many(pairs))
        else:
            reply = self._probe_out_of_process(pairs)
            if reply is None:
                return dict.fromkeys(pairs)
            flat = reply
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

    def _probe_out_of_process(self, pairs: list[tuple[str, str]]) -> dict[str, object] | None:
        """Run the probe under ``self._python``; ``None`` (and a warning) on failure.

        Every failure mode of the bridge -- a non-zero exit, an interpreter
        that cannot be run at all, one that hangs past the timeout, or one
        that writes no framed JSON map -- reports the *whole batch* as
        undetermined. "Never guess" applies to the transport exactly as it
        does to the classification: reporting nothing is recoverable,
        guessing wrong in --fix mode is not.
        """
        try:
            proc = subprocess.run(
                [self._python, self._probe_path],
                input=json.dumps(pairs),
                capture_output=True,
                text=True,
                timeout=_PROBE_TIMEOUT,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            why = f"timed out after {_PROBE_TIMEOUT}s{_stderr_tail(exc.stderr)}"
        except (subprocess.SubprocessError, OSError) as exc:
            why = f"could not be run: {exc}"
        else:
            reply = _probe.read_reply(proc.stdout or "")
            if proc.returncode == 0 and reply is not None:
                return reply
            status = (
                f"exited with status {proc.returncode}"
                if proc.returncode != 0
                else "sent no readable reply"
            )
            why = status + _stderr_tail(proc.stderr)
        self._failures[why] = self._failures.get(why, 0) + len(pairs)
        return None

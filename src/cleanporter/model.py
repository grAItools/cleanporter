"""Result types shared by the analyzer, fixer and CLI."""

from __future__ import annotations

import dataclasses
import enum
import pathlib


class Kind(enum.Enum):
    """What ``PARENT.NAME`` resolves to, as far as the filesystem can tell."""

    MODULE = "module"
    OBJECT = "object"
    #: Both a submodule on disk and a top-level binding in the parent's
    #: ``__init__``. The binding wins at import time, so this cannot be
    #: decided statically -- report it, never guess.
    AMBIGUOUS = "ambiguous"
    #: First-party, but neither on disk as a submodule nor provably bound in
    #: the parent: a generated ``_version.py``, a ``_pb2`` module, a Cython
    #: module built out of tree, a sibling portion of a namespace package, a
    #: name only a PEP 562 ``__getattr__`` could supply. Absence is not
    #: evidence of an object, so this is reported, never guessed.
    UNDETERMINED = "undetermined"
    #: First-party parent, not on disk, and bound (at least in part) by a
    #: ``from M import X`` whose ``M`` is third-party: the interpreter probe
    #: has to answer for ``M.X`` before there is a verdict. Never a final
    #: answer; `resolver.Resolver` settles it.
    DEFERRED = "deferred"


class Status(enum.Enum):
    #: ``NAME`` is an object imported by name -> fixable violation.
    VIOLATION = "violation"
    #: Could not classify (parent not importable, ambiguous, ...) -> never fixed.
    UNRESOLVED = "unresolved"
    #: Structurally a violation but deliberately not rewritten. Reported as
    #: "not rewritten", and counts toward the failure exit code: the fixer
    #: declined, and a human has to decide what to do about it.
    SKIPPED = "skipped"
    #: Matched a ``[tool.cleanporter.skip]`` rule, so it was never analysed.
    #: Distinct from `SKIPPED` in the one way that matters: the author asked
    #: for this, so it is not a failure and never reaches the exit code.
    SKIPPED_BY_CONFIG = "skipped-by-config"
    #: An inline ``cleanporter: ignore[...]`` comment that suppressed
    #: nothing -- no finding of a code it names on the names it covers. Counts
    #: toward the failure exit code: a stale suppression would otherwise
    #: silently swallow the next finding to land on its line.
    UNUSED_SUPPRESSION = "unused-suppression"
    #: A module bound under a name its alias convention does not allow
    #: (``[[tool.cleanporter.alias]]``; see `cleanporter.aliases`). Counts
    #: toward the failure exit code. Report-only: ``--fix`` never renames an
    #: existing binding, though every binding it *creates* follows the
    #: convention.
    ALIAS_MISMATCH = "alias-mismatch"


@dataclasses.dataclass(frozen=True)
class Finding:
    path: pathlib.Path
    line: int
    column: int
    parent: str
    name: str
    status: Status
    detail: str = ""
    #: For a `CP001` from a *relative* import, the module import that would
    #: replace it, spelled as the fixer spells its statement
    #: (``from . import readers``); empty otherwise. `message` quotes it as
    #: advice, because ``parent`` is the absolute name the import root
    #: implies -- ``io.readers`` for a namespace directory -- which is how
    #: the resolver classifies the import, not how anyone should write it.
    module_import: str = ""

    @property
    def code(self) -> str:
        return {
            Status.VIOLATION: "CP001",
            Status.UNRESOLVED: "CP002",
            Status.SKIPPED: "CP003",
            Status.SKIPPED_BY_CONFIG: "CP004",
            Status.UNUSED_SUPPRESSION: "CP005",
            Status.ALIAS_MISMATCH: "CP006",
        }[self.status]

    @property
    def message(self) -> str:
        """What `format` says after ``PATH:LINE:COLUMN: CODE``."""
        if self.status is Status.VIOLATION:
            # The conventional spelling, as advice -- not necessarily what
            # `--fix` writes, which can reuse an existing binding of the
            # module or pick a free alias.
            token = self.parent.rsplit(".", 1)[-1]
            module = f" ('{self.module_import}')" if self.module_import else ""
            return (
                f"imports object '{self.name}' from module '{self.parent}'; "
                f"import the module{module} and use '{token}.{self.name}'"
            )
        if self.status is Status.UNRESOLVED and self.name == "?":
            # A whole file that could not be read, decoded, parsed or written.
            return f"file not processed: {self.detail}"
        if self.status is Status.UNRESOLVED:
            return (
                f"could not determine whether '{self.parent}.{self.name}' "
                f"is a module: {self.detail}"
            )
        if self.status is Status.SKIPPED_BY_CONFIG:
            return f"{self._subject()} skipped by configuration: {self.detail}"
        if self.status is Status.UNUSED_SUPPRESSION:
            return f"unused suppression: {self.detail}"
        if self.status is Status.ALIAS_MISMATCH:
            # ``parent`` is the module, ``name`` the name it is bound under.
            return f"module '{self.parent}' is bound as '{self.name}': {self.detail}"
        return f"{self._subject()} not rewritten: {self.detail}"

    def format(self) -> str:
        return f"{self.path}:{self.line}:{self.column}: {self.code} {self.message}"

    def _subject(self) -> str:
        return "file" if self.name == "?" else f"'{self.name}' from '{self.parent}'"

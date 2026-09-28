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


@dataclasses.dataclass(frozen=True)
class Finding:
    path: pathlib.Path
    line: int
    column: int
    parent: str
    name: str
    status: Status
    detail: str = ""

    @property
    def code(self) -> str:
        return {
            Status.VIOLATION: "CP001",
            Status.UNRESOLVED: "CP002",
            Status.SKIPPED: "CP003",
            Status.SKIPPED_BY_CONFIG: "CP004",
        }[self.status]

    @property
    def replacement(self) -> str | None:
        """The spelling a `CP001` use site takes after the fix (``helpers.Widget``).

        ``None`` for every other status: nothing is rewritten for those.
        """
        if self.status is not Status.VIOLATION:
            return None
        return f"{self.parent.rsplit('.', 1)[-1]}.{self.name}"

    @property
    def message(self) -> str:
        """What `format` says after ``PATH:LINE:COLUMN: CODE``."""
        if self.replacement is not None:
            return (
                f"imports object '{self.name}' from module '{self.parent}'; "
                f"import the module and use '{self.replacement}'"
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
        return f"{self._subject()} not rewritten: {self.detail}"

    def format(self) -> str:
        return f"{self.path}:{self.line}:{self.column}: {self.code} {self.message}"

    def _subject(self) -> str:
        return "file" if self.name == "?" else f"'{self.name}' from '{self.parent}'"

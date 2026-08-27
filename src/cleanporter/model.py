"""Result types shared by the analyzer, fixer and CLI."""

from __future__ import annotations

import enum
from dataclasses import dataclass
from pathlib import Path


class Kind(enum.Enum):
    """What ``PARENT.NAME`` resolves to, as far as the filesystem can tell."""

    MODULE = "module"
    OBJECT = "object"
    #: Both a submodule on disk and a top-level binding in the parent's
    #: ``__init__``. The binding wins at import time, so this cannot be
    #: decided statically -- report it, never guess.
    AMBIGUOUS = "ambiguous"


class Status(enum.Enum):
    #: ``NAME`` is an object imported by name -> fixable violation.
    VIOLATION = "violation"
    #: Could not classify (parent not importable, ambiguous, ...) -> never fixed.
    UNRESOLVED = "unresolved"
    #: Structurally a violation but deliberately not rewritten.
    SKIPPED = "skipped"


@dataclass(frozen=True)
class Finding:
    path: Path
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
        }[self.status]

    def format(self) -> str:
        loc = f"{self.path}:{self.line}:{self.column}"
        if self.status is Status.VIOLATION:
            token = self.parent.rsplit(".", 1)[-1]
            msg = (
                f"imports object '{self.name}' from module '{self.parent}'; "
                f"import the module and use '{token}.{self.name}'"
            )
        elif self.status is Status.UNRESOLVED:
            msg = (
                f"could not determine whether '{self.parent}.{self.name}' "
                f"is a module: {self.detail}"
            )
        else:
            subject = "file" if self.name == "?" else f"'{self.name}' from '{self.parent}'"
            msg = f"{subject} not rewritten: {self.detail}"
        return f"{loc}: {self.code} {msg}"

"""Result types shared by the analyzer, fixer and CLI."""

from __future__ import annotations

import enum
from dataclasses import dataclass
from pathlib import Path


class Status(enum.Enum):
    #: ``NAME`` is an object imported by name -> fixable violation.
    VIOLATION = "violation"
    #: Could not classify (parent not importable, etc.) -> reported, never fixed.
    UNKNOWN = "unknown"
    #: Structurally a violation but unsafe to auto-fix (``import *``, re-export,
    #: name collision we won't resolve) -> reported, not auto-fixed.
    UNFIXABLE = "unfixable"


@dataclass(frozen=True)
class Finding:
    path: Path
    line: int
    column: int
    parent: str  # the module imported FROM (absolute, resolved)
    name: str  # the bound name that is an object
    status: Status
    detail: str = ""

    @property
    def code(self) -> str:
        return {
            Status.VIOLATION: "MI001",
            Status.UNKNOWN: "MI900",
            Status.UNFIXABLE: "MI002",
        }[self.status]

    def format(self) -> str:
        loc = f"{self.path}:{self.line}:{self.column}"
        if self.status is Status.VIOLATION:
            token = self.parent.rsplit(".", 1)[-1]
            msg = (
                f"imports object '{self.name}' from module '{self.parent}'; "
                f"import the module and use '{token}.{self.name}'"
            )
        elif self.status is Status.UNKNOWN:
            msg = f"could not determine whether '{self.parent}.{self.name}' is a module: {self.detail}"
        else:
            msg = f"'{self.name}' from '{self.parent}' is not auto-fixable: {self.detail}"
        return f"{loc}: {self.code} {msg}"

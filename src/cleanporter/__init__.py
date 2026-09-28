"""cleanporter: enforce Google Python Style Guide 2.2 (import modules, not members).

The library API: `run` is a whole run -- check, diff or fix, per `Mode` --
returning a `RunResult`; `build` makes the `Project` a run works on, and
`analyze_record` and `fix_record` are one file of it.
"""

from __future__ import annotations

import importlib.metadata

try:
    __version__ = importlib.metadata.version("cleanporter")
except importlib.metadata.PackageNotFoundError:  # pragma: no cover
    # Running from a source tree that was never installed.
    __version__ = "0.0.0+unknown"

__all__ = [
    "Config",
    "Mode",
    "Project",
    "Resolver",
    "RunResult",
    "__version__",
    "analyze_record",
    "build",
    "fix_record",
    "run",
]

from .analyze import analyze_record
from .config import Config
from .engine import Mode, RunResult, run
from .project import Project, build
from .resolver import Resolver
from .rewrite import fix_record

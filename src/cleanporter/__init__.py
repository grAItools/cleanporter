"""cleanporter: enforce Google Python Style Guide 2.2 (import modules, not members)."""

from __future__ import annotations

import importlib.metadata

try:
    __version__ = importlib.metadata.version("cleanporter")
except importlib.metadata.PackageNotFoundError:  # pragma: no cover
    # Running from a source tree that was never installed.
    __version__ = "0.0.0+unknown"

__all__ = ["Config", "Resolver", "__version__", "analyze_record", "build", "fix_record"]

from .analyze import analyze_record, build
from .config import Config
from .resolver import Resolver
from .rewrite import fix_record

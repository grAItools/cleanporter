"""cleanporter: enforce Google Python Style Guide 2.2 (import modules, not members)."""

from __future__ import annotations

__version__ = "0.2.0"

__all__ = ["Config", "Resolver", "__version__", "analyze_record", "build", "fix_record"]

from .analyze import analyze_record, build
from .config import Config
from .resolver import Resolver
from .rewrite import fix_record

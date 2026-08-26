"""Configuration: exemptions and enforcement scope.

The Google style guide explicitly exempts ``typing``; we extend the default
allowlist to the other places where importing names is idiomatic and blessed
(``typing_extensions``, ``collections.abc``, ``__future__``). Everything is
configurable so a project can widen or narrow enforcement.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Modules whose members may be imported directly by name.
DEFAULT_EXEMPT_MODULES: frozenset[str] = frozenset(
    {
        "typing",
        "typing_extensions",
        "collections.abc",
        "__future__",
    }
)


@dataclass(frozen=True)
class Config:
    #: ``from MODULE import X`` is allowed when MODULE is in this set.
    exempt_modules: frozenset[str] = DEFAULT_EXEMPT_MODULES
    #: Individual bound names that are always allowed (e.g. ``annotations``).
    exempt_names: frozenset[str] = frozenset()
    #: Interpreter used for the stdlib/third-party probe (None -> current).
    python: str | None = None
    #: Extra import roots to treat as first-party (besides inferred ones).
    extra_roots: tuple[str, ...] = field(default_factory=tuple)

    def is_exempt(self, parent: str, name: str) -> bool:
        if name in self.exempt_names:
            return True
        # Exempt if the from-module or any ancestor is exempted.
        parts = parent.split(".")
        return any(".".join(parts[:i]) in self.exempt_modules for i in range(len(parts), 0, -1))

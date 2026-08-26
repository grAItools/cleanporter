"""Shared fixtures: build synthetic projects for tests."""

from __future__ import annotations

import shutil
import sys
import sysconfig
from pathlib import Path

import pytest

from cleanporter.config import Config
from cleanporter.resolver import Origin, Resolver

HELPERS_SRC = '''"""Helpers."""

THING = 42


class Widget:
    pass


def make():
    return Widget()
'''

DATA_SRC = '''"""Submodule under mypkg.sub."""

VALUE = 7
'''


@pytest.fixture()
def make_project(tmp_path: Path) -> Path:
    """Create a demo source tree rooted at tmp_path and chdir-like config."""

    def build(*, src_layout: bool = True) -> Path:
        base = tmp_path / "proj"
        pkg_root = base / ("src" if src_layout else "") / "mypkg"
        (pkg_root / "sub").mkdir(parents=True, exist_ok=True)
        (pkg_root / "helpers.py").write_text(HELPERS_SRC, encoding="utf-8")
        (pkg_root / "sub" / "__init__.py").write_text(
            '"""Subpackage exposing an object alongside its data module."""\n\nSUB_OBJECT = 99\n',
            encoding="utf-8",
        )
        (pkg_root / "sub" / "data.py").write_text(DATA_SRC, encoding="utf-8")
        (base / "pyproject.toml").write_text(
            '[project]\nname = "demo"\nversion = "0"\n', encoding="utf-8"
        )
        return base

    return build


@pytest.fixture()
def config(make_project) -> Config:
    base = make_project()
    return Config(root=base)


def make_resolver(cfg, *, runtime_fallback: bool = True) -> Resolver:
    """Accept a Config or a project base Path."""
    config = cfg if isinstance(cfg, Config) else Config(root=Path(cfg))
    roots = discover(config)
    extra = [(Path(p), Origin.THIRD_PARTY) for p in site_packages()]
    return Resolver(
        roots,
        runtime_fallback=runtime_fallback,
        extra_roots=extra,
    )


def discover(config) -> list[Path]:
    from cleanporter.resolver import discover_source_roots

    cfg = config if isinstance(config, Config) else Config(root=Path(config))
    return discover_source_roots(cfg.root, cfg.source_roots)


def site_packages() -> list[str]:
    found = []
    purelib = sysconfig.get_path("purelib")
    if purelib:
        found.append(purelib)
    for entry in sys.path:
        if Path(entry).is_dir() and Path(entry).name == "site-packages" and entry not in found:
            found.append(entry)
    return found

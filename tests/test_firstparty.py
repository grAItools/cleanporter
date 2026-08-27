"""Filesystem classification of first-party packages."""

from __future__ import annotations

from pathlib import Path

from cleanporter.firstparty import ModuleMap
from cleanporter.model import Kind


def _pkg(tmp_path: Path) -> Path:
    root = tmp_path / "src"
    (root / "amb").mkdir(parents=True)
    (root / "amb" / "__init__.py").write_text("", encoding="utf-8")
    (root / "amb" / "mod.py").write_text("Q = 1\n", encoding="utf-8")
    return root


def test_py_submodule_is_a_module(tmp_path):
    root = _pkg(tmp_path)
    mm = ModuleMap([root])
    assert mm.classify("amb", "mod") is Kind.MODULE


def test_plain_object_is_not_a_module(tmp_path):
    root = _pkg(tmp_path)
    (root / "amb" / "__init__.py").write_text("Thing = object()\n", encoding="utf-8")
    mm = ModuleMap([root])
    assert mm.classify("amb", "Thing") is Kind.OBJECT


def test_extension_submodule_is_a_module(tmp_path):
    root = _pkg(tmp_path)
    (root / "amb" / "accel.cpython-314-x86_64-linux-gnu.so").touch()
    mm = ModuleMap([root])
    assert mm.classify("amb", "accel") is Kind.MODULE


def test_windows_extension_submodule_is_a_module(tmp_path):
    root = _pkg(tmp_path)
    (root / "amb" / "fast.cp310-win_amd64.pyd").touch()
    mm = ModuleMap([root])
    assert mm.classify("amb", "fast") is Kind.MODULE


def test_directory_holding_only_an_extension_is_a_package(tmp_path):
    root = _pkg(tmp_path)
    (root / "amb" / "native").mkdir()
    (root / "amb" / "native" / "core.abi3.so").touch()
    mm = ModuleMap([root])
    assert mm.classify("amb", "native") is Kind.MODULE
    assert mm.classify("amb.native", "core") is Kind.MODULE


def test_non_first_party_defers_to_the_probe(tmp_path):
    mm = ModuleMap([_pkg(tmp_path)])
    assert mm.classify("collections", "OrderedDict") is None


def test_submodule_shadowed_by_an_init_binding_is_ambiguous(tmp_path):
    root = _pkg(tmp_path)
    (root / "amb" / "__init__.py").write_text(
        'mod = "shadowing string, not the submodule"\n', encoding="utf-8"
    )
    mm = ModuleMap([root])
    assert mm.classify("amb", "mod") is Kind.AMBIGUOUS


def test_init_importing_its_own_submodule_is_not_ambiguous(tmp_path):
    root = _pkg(tmp_path)
    (root / "amb" / "__init__.py").write_text("from . import mod\n", encoding="utf-8")
    mm = ModuleMap([root])
    assert mm.classify("amb", "mod") is Kind.MODULE


def test_for_loop_binding_that_shadows_a_submodule_is_ambiguous(tmp_path):
    root = _pkg(tmp_path)
    (root / "amb" / "__init__.py").write_text(
        'for mod in ["placeholder"]:\n    pass\n', encoding="utf-8"
    )
    mm = ModuleMap([root])
    assert mm.classify("amb", "mod") is Kind.AMBIGUOUS


def test_grandparent_relative_import_that_shadows_a_submodule_is_ambiguous(tmp_path):
    root = tmp_path / "src"
    (root / "pkg" / "sub").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "sub" / "__init__.py").write_text("from .. import Y\n", encoding="utf-8")
    (root / "pkg" / "sub" / "Y.py").write_text("Q = 1\n", encoding="utf-8")
    mm = ModuleMap([root])
    assert mm.classify("pkg.sub", "Y") is Kind.AMBIGUOUS


def test_aliased_self_import_that_shadows_a_real_submodule_is_ambiguous(tmp_path):
    root = _pkg(tmp_path)
    (root / "amb" / "__init__.py").write_text("from . import mod as m\n", encoding="utf-8")
    (root / "amb" / "m.py").write_text("Q = 1\n", encoding="utf-8")
    mm = ModuleMap([root])
    assert mm.classify("amb", "m") is Kind.AMBIGUOUS


def test_bare_annotation_does_not_bind_and_is_not_ambiguous(tmp_path):
    root = _pkg(tmp_path)
    (root / "amb" / "__init__.py").write_text(
        "from types import ModuleType\nmod: ModuleType\n", encoding="utf-8"
    )
    mm = ModuleMap([root])
    assert mm.classify("amb", "mod") is Kind.MODULE


# -- nested import roots (final review, Critical 1) --------------------------


def _src_layout(tmp_path: Path) -> Path:
    """A src-layout project whose ``tests/__init__.py`` drags the repo root in."""
    (tmp_path / "src" / "mypkg").mkdir(parents=True)
    (tmp_path / "src" / "mypkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "mypkg" / "helpers.py").write_text("Widget = object\n", encoding="utf-8")
    (tmp_path / "src" / "mypkg" / "consumer.py").write_text(
        "from .helpers import Widget\nw = Widget()\n", encoding="utf-8"
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "tests" / "test_it.py").write_text("", encoding="utf-8")
    return tmp_path


def test_the_most_specific_root_wins_for_qualname(tmp_path):
    root = _src_layout(tmp_path)
    mm = ModuleMap([root, root / "src"])
    assert mm.qualname_for(root / "src" / "mypkg" / "consumer.py") == "mypkg.consumer"


def test_from_paths_on_a_src_layout_still_qualifies_against_src(tmp_path):
    root = _src_layout(tmp_path)
    # What `cleanporter` with no path arguments does: every file under `.`.
    files = sorted(root.rglob("*.py"))
    mm = ModuleMap.from_paths(files)
    assert {r.name for r in mm.roots} == {root.name, "src"}, "both roots are inferred"
    assert mm.qualname_for(root / "src" / "mypkg" / "consumer.py") == "mypkg.consumer"
    assert mm.qualname_for(root / "src" / "mypkg" / "__init__.py") == "mypkg"


def test_nesting_roots_is_reported_as_a_warning(tmp_path):
    root = _src_layout(tmp_path)
    mm = ModuleMap([root, root / "src"])
    assert any("nest" in w for w in mm.warnings)
    assert ModuleMap([root / "src"]).warnings == []


# -- PEP 420 namespace packages (re-review blocker) --------------------------
#
# `_root_for` stops walking up at the first directory without `__init__.py`,
# so a namespace-package directory is itself inferred as an import root. That
# bogus root is *deeper* than the real one, so the longest-match rule above
# would pick it and truncate the file's dotted name -- and a truncated anchor
# resolves relative imports to the wrong absolute parent. Two disqualifiers
# keep it out: the file's own relative-import depth, and a declared root.


def _flat_namespace(tmp_path: Path) -> Path:
    """``mypkg/`` with no ``__init__.py``; ``tests/`` drags in the repo root."""
    (tmp_path / "mypkg").mkdir()
    (tmp_path / "mypkg" / "helpers.py").write_text("Widget = object\n", encoding="utf-8")
    (tmp_path / "mypkg" / "consumer.py").write_text(
        "from .helpers import Widget\nw = Widget()\n", encoding="utf-8"
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "__init__.py").write_text("", encoding="utf-8")
    return tmp_path


def _nested_namespace(tmp_path: Path) -> Path:
    """``pkg/__init__.py`` plus a namespace subpackage ``pkg/sub/``."""
    (tmp_path / "pkg" / "sub").mkdir(parents=True)
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "sub" / "other.py").write_text("Thing = object\n", encoding="utf-8")
    (tmp_path / "pkg" / "sub" / "mod.py").write_text(
        "from .other import Thing\nt = Thing()\n", encoding="utf-8"
    )
    return tmp_path


def test_a_flat_namespace_package_is_not_mistaken_for_an_import_root(tmp_path):
    root = _flat_namespace(tmp_path)
    mm = ModuleMap.from_paths(sorted(root.rglob("*.py")))
    assert root / "mypkg" in mm.roots, "the bogus root is still inferred"
    # ... but a file holding `from .helpers import ...` cannot be top-level.
    consumer = root / "mypkg" / "consumer.py"
    assert mm.qualname_for(consumer, relative_level=1) == "mypkg.consumer"


def test_a_namespace_subpackage_is_not_mistaken_for_an_import_root(tmp_path):
    root = _nested_namespace(tmp_path)
    mm = ModuleMap.from_paths(sorted(root.rglob("*.py")))
    assert mm.qualname_for(root / "pkg" / "sub" / "mod.py", relative_level=1) == "pkg.sub.mod"


def test_a_deeper_relative_import_pushes_the_root_further_up(tmp_path):
    root = _nested_namespace(tmp_path)
    (root / "pkg" / "sub" / "mod.py").write_text("from ..other import Thing\n", encoding="utf-8")
    mm = ModuleMap.from_paths(sorted(root.rglob("*.py")))
    # `from ..x` needs two packages above it, which only the repo root gives.
    assert mm.qualname_for(root / "pkg" / "sub" / "mod.py", relative_level=2) == "pkg.sub.mod"


def test_a_namespace_package_init_is_qualified_against_its_parent(tmp_path):
    root = _nested_namespace(tmp_path)
    (root / "pkg" / "sub" / "__init__.py").write_text(
        "from .other import Thing\n", encoding="utf-8"
    )
    mm = ModuleMap.from_paths(sorted(root.rglob("*.py")))
    assert mm.qualname_for(root / "pkg" / "sub" / "__init__.py", relative_level=1) == "pkg.sub"


def test_an_unanchorable_relative_import_still_gets_a_qualname(tmp_path):
    """No root satisfies the floor -> best effort, so CP002 is still reported."""
    root = _flat_namespace(tmp_path)
    mm = ModuleMap([root])
    assert mm.qualname_for(root / "mypkg" / "consumer.py", relative_level=9) == "mypkg.consumer"


def _declared_namespace(tmp_path: Path) -> Path:
    """A src layout whose package is a PEP 420 namespace package."""
    (tmp_path / "src" / "mypkg").mkdir(parents=True)
    (tmp_path / "src" / "mypkg" / "other.py").write_text("Thing = object\n", encoding="utf-8")
    (tmp_path / "src" / "mypkg" / "mod.py").write_text(
        "from .other import Thing\nt = Thing()\n", encoding="utf-8"
    )
    return tmp_path


def test_a_declared_root_outranks_a_deeper_inferred_one(tmp_path):
    root = _declared_namespace(tmp_path)
    mm = ModuleMap.from_paths(sorted(root.rglob("*.py")), declared=(root / "src",))
    assert root / "src" in mm.roots, "a declared root is always in the root set"
    # Even with no relative import to set a floor, `--root src` is the answer.
    assert mm.qualname_for(root / "src" / "mypkg" / "mod.py") == "mypkg.mod"


def test_a_declared_root_is_kept_even_when_no_file_implies_it(tmp_path):
    root = _declared_namespace(tmp_path)
    mm = ModuleMap([], declared=(root / "src",))
    assert mm.roots == [(root / "src").resolve()]
    assert mm.classify("mypkg", "other") is Kind.MODULE


# -- a namespace package holding a regular subpackage ------------------------
#
# `analytics/` (no `__init__.py`) around `analytics/io/__init__.py` is the
# canonical PEP 420 layout, and it defeats both rules above: `analytics` is
# inferred as a root, and `io/__init__.py` can honestly sit one package deep.
# Only a file *outside* it can settle the question.


def _namespace_with_subpackage(tmp_path: Path) -> Path:
    (tmp_path / "analytics" / "io").mkdir(parents=True)
    (tmp_path / "analytics" / "io" / "readers.py").write_text("read = print\n", encoding="utf-8")
    (tmp_path / "analytics" / "io" / "__init__.py").write_text(
        "from .readers import read\n", encoding="utf-8"
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "tests" / "test_it.py").write_text(
        "from analytics.io import read\n", encoding="utf-8"
    )
    return tmp_path


def _map_for(root: Path) -> ModuleMap:
    return ModuleMap.from_paths(sorted(root.rglob("*.py")))


def test_a_root_that_another_file_imports_as_a_package_is_demoted(tmp_path):
    root = _namespace_with_subpackage(tmp_path)
    init = root / "analytics" / "io" / "__init__.py"
    mm = _map_for(root)
    assert mm.qualname_for(init, relative_level=1) == "io", "undecidable on its own"

    mm = _map_for(root)
    mm.demote_roots({"analytics": [root / "tests" / "test_it.py"]})
    # Not `io`, which makes `from .readers import read` a stdlib rewrite.
    assert mm.qualname_for(init, relative_level=1) == "analytics.io"


def test_evidence_from_inside_the_candidate_root_does_not_demote_it(tmp_path):
    """A file under `src/` writing `from src.mypkg import x` -- possibly one an
    earlier bad rewrite produced -- must not cement `src` as a package."""
    root = _src_layout(tmp_path)
    consumer = root / "src" / "mypkg" / "consumer.py"
    mm = _map_for(root)
    mm.demote_roots({"src": [consumer]})
    assert mm.qualname_for(consumer, relative_level=1) == "mypkg.consumer"


def test_a_declared_root_is_never_demoted(tmp_path):
    root = _namespace_with_subpackage(tmp_path)
    mm = ModuleMap.from_paths(sorted(root.rglob("*.py")), declared=(root / "analytics",))
    mm.demote_roots({"analytics": [root / "tests" / "test_it.py"]})
    assert mm.qualname_for(root / "analytics" / "io" / "__init__.py", 1) == "io"


def test_a_root_with_no_fallback_is_not_demoted(tmp_path):
    root = _namespace_with_subpackage(tmp_path)
    mm = ModuleMap([root / "analytics"])
    mm.demote_roots({"analytics": [root / "tests" / "test_it.py"]})
    assert mm.qualname_for(root / "analytics" / "io" / "__init__.py", 1) == "io"

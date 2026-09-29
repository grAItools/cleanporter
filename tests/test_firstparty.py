"""Filesystem classification of first-party packages."""

from __future__ import annotations

import pathlib
import random
import time

import pytest

from cleanporter import _bindings, firstparty, model


def _pkg(tmp_path: pathlib.Path) -> pathlib.Path:
    root = tmp_path / "src"
    (root / "amb").mkdir(parents=True)
    (root / "amb" / "__init__.py").write_text("", encoding="utf-8", newline="\n")
    (root / "amb" / "mod.py").write_text("Q = 1\n", encoding="utf-8", newline="\n")
    return root


def test_py_submodule_is_a_module(tmp_path):
    root = _pkg(tmp_path)
    mm = firstparty.ModuleMap([root])
    assert mm.classify("amb", "mod") is model.Kind.MODULE


def test_plain_object_is_not_a_module(tmp_path):
    root = _pkg(tmp_path)
    (root / "amb" / "__init__.py").write_text("Thing = object()\n", encoding="utf-8", newline="\n")
    mm = firstparty.ModuleMap([root])
    assert mm.classify("amb", "Thing") is model.Kind.OBJECT


def test_extension_submodule_is_a_module(tmp_path):
    root = _pkg(tmp_path)
    (root / "amb" / "accel.cpython-314-x86_64-linux-gnu.so").touch()
    mm = firstparty.ModuleMap([root])
    assert mm.classify("amb", "accel") is model.Kind.MODULE


def test_windows_extension_submodule_is_a_module(tmp_path):
    root = _pkg(tmp_path)
    (root / "amb" / "fast.cp310-win_amd64.pyd").touch()
    mm = firstparty.ModuleMap([root])
    assert mm.classify("amb", "fast") is model.Kind.MODULE


def test_directory_holding_only_an_extension_is_a_package(tmp_path):
    root = _pkg(tmp_path)
    (root / "amb" / "native").mkdir()
    (root / "amb" / "native" / "core.abi3.so").touch()
    mm = firstparty.ModuleMap([root])
    assert mm.classify("amb", "native") is model.Kind.MODULE
    assert mm.classify("amb.native", "core") is model.Kind.MODULE


def test_non_first_party_defers_to_the_probe(tmp_path):
    mm = firstparty.ModuleMap([_pkg(tmp_path)])
    assert mm.classify("collections", "OrderedDict") is None


def test_submodule_shadowed_by_an_init_binding_is_ambiguous(tmp_path):
    root = _pkg(tmp_path)
    (root / "amb" / "__init__.py").write_text(
        'mod = "shadowing string, not the submodule"\n', encoding="utf-8", newline="\n"
    )
    mm = firstparty.ModuleMap([root])
    assert mm.classify("amb", "mod") is model.Kind.AMBIGUOUS


def test_init_importing_its_own_submodule_is_not_ambiguous(tmp_path):
    root = _pkg(tmp_path)
    (root / "amb" / "__init__.py").write_text("from . import mod\n", encoding="utf-8", newline="\n")
    mm = firstparty.ModuleMap([root])
    assert mm.classify("amb", "mod") is model.Kind.MODULE


def test_init_importing_its_own_submodule_absolutely_is_not_ambiguous(tmp_path):
    """``from amb import mod`` inside ``amb/__init__.py`` is the same statement.

    Same package, same fallback to importing the submodule when the
    attribute is absent, same module bound -- django's
    ``db/models/__init__.py`` simply spells it absolutely. Reading only the
    relative form made every consumer of ``django.db.models.signals``
    unresolvable.
    """
    root = _pkg(tmp_path)
    (root / "amb" / "__init__.py").write_text(
        "from amb import mod\n", encoding="utf-8", newline="\n"
    )
    mm = firstparty.ModuleMap([root])
    assert mm.classify("amb", "mod") is model.Kind.MODULE


def test_an_absolute_import_of_another_packages_module_still_shadows(tmp_path):
    """Only the file's *own* package is discounted; anything else is a binding."""
    root = _pkg(tmp_path)
    (root / "other").mkdir()
    (root / "other" / "__init__.py").write_text("", encoding="utf-8", newline="\n")
    (root / "other" / "mod.py").write_text("Q = 2\n", encoding="utf-8", newline="\n")
    (root / "amb" / "__init__.py").write_text(
        "from other import mod\n", encoding="utf-8", newline="\n"
    )
    mm = firstparty.ModuleMap([root])
    assert mm.classify("amb", "mod") is model.Kind.AMBIGUOUS


def test_an_aliased_absolute_self_import_still_shadows(tmp_path):
    """``from amb import mod as m`` binds ``m``, which nothing auto-populates."""
    root = _pkg(tmp_path)
    (root / "amb" / "__init__.py").write_text(
        "from amb import mod as m\n", encoding="utf-8", newline="\n"
    )
    (root / "amb" / "m.py").write_text("Q = 1\n", encoding="utf-8", newline="\n")
    mm = firstparty.ModuleMap([root])
    assert mm.classify("amb", "m") is model.Kind.AMBIGUOUS


def test_a_competing_binding_beside_the_self_import_is_still_ambiguous(tmp_path):
    """The discount is for the statement, not for the name.

    ``mod`` bound anywhere else in the file wins the attribute lookup the
    import falls back from, so the pair stays undecidable. Holds for the
    relative spelling too, which used to have the name subtracted out from
    under its own reassignment.
    """
    root = _pkg(tmp_path)
    for init in ("from amb import mod\nmod = wrap(mod)\n", "from . import mod\nmod = 42\n"):
        (root / "amb" / "__init__.py").write_text(init, encoding="utf-8", newline="\n")
        mm = firstparty.ModuleMap([root])
        assert mm.classify("amb", "mod") is model.Kind.AMBIGUOUS, init


def test_for_loop_binding_that_shadows_a_submodule_is_ambiguous(tmp_path):
    root = _pkg(tmp_path)
    (root / "amb" / "__init__.py").write_text(
        'for mod in ["placeholder"]:\n    pass\n', encoding="utf-8", newline="\n"
    )
    mm = firstparty.ModuleMap([root])
    assert mm.classify("amb", "mod") is model.Kind.AMBIGUOUS


def test_grandparent_relative_import_that_shadows_a_submodule_is_ambiguous(tmp_path):
    root = tmp_path / "src"
    (root / "pkg" / "sub").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8", newline="\n")
    (root / "pkg" / "sub" / "__init__.py").write_text(
        "from .. import Y\n", encoding="utf-8", newline="\n"
    )
    (root / "pkg" / "sub" / "Y.py").write_text("Q = 1\n", encoding="utf-8", newline="\n")
    mm = firstparty.ModuleMap([root])
    assert mm.classify("pkg.sub", "Y") is model.Kind.AMBIGUOUS


def test_aliased_self_import_that_shadows_a_real_submodule_is_ambiguous(tmp_path):
    root = _pkg(tmp_path)
    (root / "amb" / "__init__.py").write_text(
        "from . import mod as m\n", encoding="utf-8", newline="\n"
    )
    (root / "amb" / "m.py").write_text("Q = 1\n", encoding="utf-8", newline="\n")
    mm = firstparty.ModuleMap([root])
    assert mm.classify("amb", "m") is model.Kind.AMBIGUOUS


def test_bare_annotation_does_not_bind_and_is_not_ambiguous(tmp_path):
    root = _pkg(tmp_path)
    (root / "amb" / "__init__.py").write_text(
        "from types import ModuleType\nmod: ModuleType\n", encoding="utf-8", newline="\n"
    )
    mm = firstparty.ModuleMap([root])
    assert mm.classify("amb", "mod") is model.Kind.MODULE


# -- a module and a package that share a dotted name -------------------------


def _shadowed_package(tmp_path: pathlib.Path) -> pathlib.Path:
    """``amb/`` re-exporting ``Q``, with a stale flat ``amb.py`` beside it."""
    root = _pkg(tmp_path)
    (root / "amb" / "__init__.py").write_text(
        "from amb.mod import Q\n", encoding="utf-8", newline="\n"
    )
    (root / "amb.py").write_text("Q = 2\n", encoding="utf-8", newline="\n")
    return root


def test_a_flat_module_does_not_hide_the_packages_reexports(tmp_path):
    """``amb.py`` beside ``amb/`` must not be the only file consulted.

    Python imports the package and ignores the flat module, but the map kept
    one source per dotted name and the flat module is scanned last, so
    `is_reexport` read ``amb.py`` -- which re-exports nothing -- and said no.
    The guard then stood down and ``--fix`` deleted ``amb.Q``.
    """
    mm = firstparty.ModuleMap([_shadowed_package(tmp_path)])
    assert mm.is_reexport("amb", "Q") is True


def test_a_name_neither_claimant_reexports_is_not_a_reexport(tmp_path):
    """Consulting every claimant must not turn into saying yes to everything."""
    mm = firstparty.ModuleMap([_shadowed_package(tmp_path)])
    assert mm.is_reexport("amb", "absent") is False


# -- a package's own submodule names ------------------------------------------


def test_submodules_lists_the_immediate_children(tmp_path):
    root = _pkg(tmp_path)
    (root / "amb" / "deep").mkdir()
    (root / "amb" / "deep" / "__init__.py").write_text("", encoding="utf-8", newline="\n")
    (root / "amb" / "deep" / "buried.py").write_text("", encoding="utf-8", newline="\n")
    (root / "amb" / "accel.abi3.so").touch()
    mm = firstparty.ModuleMap([root])
    assert mm.submodules("amb") == frozenset({"mod", "deep", "accel"})
    assert mm.submodules("amb.deep") == frozenset({"buried"})


def test_submodules_of_a_plain_module_is_empty(tmp_path):
    """What makes this safe to ask about any file: a module has no children."""
    mm = firstparty.ModuleMap([_pkg(tmp_path)])
    assert mm.submodules("amb.mod") == frozenset()
    assert mm.submodules("not.a.thing") == frozenset()


# -- nested import roots (final review, Critical 1) --------------------------


def _src_layout(tmp_path: pathlib.Path) -> pathlib.Path:
    """A src-layout project whose ``tests/__init__.py`` drags the repo root in."""
    (tmp_path / "src" / "mypkg").mkdir(parents=True)
    (tmp_path / "src" / "mypkg" / "__init__.py").write_text("", encoding="utf-8", newline="\n")
    (tmp_path / "src" / "mypkg" / "helpers.py").write_text(
        "Widget = object\n", encoding="utf-8", newline="\n"
    )
    (tmp_path / "src" / "mypkg" / "consumer.py").write_text(
        "from .helpers import Widget\nw = Widget()\n", encoding="utf-8", newline="\n"
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "__init__.py").write_text("", encoding="utf-8", newline="\n")
    (tmp_path / "tests" / "test_it.py").write_text("", encoding="utf-8", newline="\n")
    return tmp_path


def test_the_most_specific_root_wins_for_qualname(tmp_path):
    root = _src_layout(tmp_path)
    mm = firstparty.ModuleMap([root, root / "src"])
    assert mm.qualname_for(root / "src" / "mypkg" / "consumer.py") == "mypkg.consumer"


def test_from_paths_on_a_src_layout_still_qualifies_against_src(tmp_path):
    root = _src_layout(tmp_path)
    # What `cleanporter` with no path arguments does: every file under `.`.
    files = sorted(root.rglob("*.py"))
    mm = firstparty.ModuleMap.from_paths(files)
    assert {r.name for r in mm.roots} == {root.name, "src"}, "both roots are inferred"
    assert mm.qualname_for(root / "src" / "mypkg" / "consumer.py") == "mypkg.consumer"
    assert mm.qualname_for(root / "src" / "mypkg" / "__init__.py") == "mypkg"


def test_nesting_roots_is_reported_as_a_warning(tmp_path):
    root = _src_layout(tmp_path)
    mm = firstparty.ModuleMap([root, root / "src"])
    assert any("nest" in w for w in mm.warnings)
    assert firstparty.ModuleMap([root / "src"]).warnings == []


# -- PEP 420 namespace packages (re-review blocker) --------------------------
#
# `_root_for` stops walking up at the first directory without `__init__.py`,
# so a namespace-package directory is itself inferred as an import root. That
# bogus root is *deeper* than the real one, so the longest-match rule above
# would pick it and truncate the file's dotted name -- and a truncated anchor
# resolves relative imports to the wrong absolute parent. Two disqualifiers
# keep it out: the file's own relative-import depth, and a declared root.


def _flat_namespace(tmp_path: pathlib.Path) -> pathlib.Path:
    """``mypkg/`` with no ``__init__.py``; ``tests/`` drags in the repo root."""
    (tmp_path / "mypkg").mkdir()
    (tmp_path / "mypkg" / "helpers.py").write_text(
        "Widget = object\n", encoding="utf-8", newline="\n"
    )
    (tmp_path / "mypkg" / "consumer.py").write_text(
        "from .helpers import Widget\nw = Widget()\n", encoding="utf-8", newline="\n"
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "__init__.py").write_text("", encoding="utf-8", newline="\n")
    return tmp_path


def _nested_namespace(tmp_path: pathlib.Path) -> pathlib.Path:
    """``pkg/__init__.py`` plus a namespace subpackage ``pkg/sub/``."""
    (tmp_path / "pkg" / "sub").mkdir(parents=True)
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8", newline="\n")
    (tmp_path / "pkg" / "sub" / "other.py").write_text(
        "Thing = object\n", encoding="utf-8", newline="\n"
    )
    (tmp_path / "pkg" / "sub" / "mod.py").write_text(
        "from .other import Thing\nt = Thing()\n", encoding="utf-8", newline="\n"
    )
    return tmp_path


def test_a_flat_namespace_package_is_not_mistaken_for_an_import_root(tmp_path):
    root = _flat_namespace(tmp_path)
    mm = firstparty.ModuleMap.from_paths(sorted(root.rglob("*.py")))
    assert root / "mypkg" in mm.roots, "the bogus root is still inferred"
    # ... but a file holding `from .helpers import ...` cannot be top-level.
    consumer = root / "mypkg" / "consumer.py"
    assert mm.qualname_for(consumer, relative_level=1) == "mypkg.consumer"


def test_a_namespace_subpackage_is_not_mistaken_for_an_import_root(tmp_path):
    root = _nested_namespace(tmp_path)
    mm = firstparty.ModuleMap.from_paths(sorted(root.rglob("*.py")))
    assert mm.qualname_for(root / "pkg" / "sub" / "mod.py", relative_level=1) == "pkg.sub.mod"


def test_a_deeper_relative_import_pushes_the_root_further_up(tmp_path):
    root = _nested_namespace(tmp_path)
    (root / "pkg" / "sub" / "mod.py").write_text(
        "from ..other import Thing\n", encoding="utf-8", newline="\n"
    )
    mm = firstparty.ModuleMap.from_paths(sorted(root.rglob("*.py")))
    # `from ..x` needs two packages above it, which only the repo root gives.
    assert mm.qualname_for(root / "pkg" / "sub" / "mod.py", relative_level=2) == "pkg.sub.mod"


def test_a_namespace_package_init_is_qualified_against_its_parent(tmp_path):
    root = _nested_namespace(tmp_path)
    (root / "pkg" / "sub" / "__init__.py").write_text(
        "from .other import Thing\n", encoding="utf-8", newline="\n"
    )
    mm = firstparty.ModuleMap.from_paths(sorted(root.rglob("*.py")))
    assert mm.qualname_for(root / "pkg" / "sub" / "__init__.py", relative_level=1) == "pkg.sub"


def test_an_unanchorable_relative_import_still_gets_a_qualname(tmp_path):
    """No root satisfies the floor -> best effort, so CP002 is still reported."""
    root = _flat_namespace(tmp_path)
    mm = firstparty.ModuleMap([root])
    assert mm.qualname_for(root / "mypkg" / "consumer.py", relative_level=9) == "mypkg.consumer"


def _declared_namespace(tmp_path: pathlib.Path) -> pathlib.Path:
    """A src layout whose package is a PEP 420 namespace package."""
    (tmp_path / "src" / "mypkg").mkdir(parents=True)
    (tmp_path / "src" / "mypkg" / "other.py").write_text(
        "Thing = object\n", encoding="utf-8", newline="\n"
    )
    (tmp_path / "src" / "mypkg" / "mod.py").write_text(
        "from .other import Thing\nt = Thing()\n", encoding="utf-8", newline="\n"
    )
    return tmp_path


def test_a_declared_root_outranks_a_deeper_inferred_one(tmp_path):
    root = _declared_namespace(tmp_path)
    mm = firstparty.ModuleMap.from_paths(sorted(root.rglob("*.py")), declared=(root / "src",))
    assert root / "src" in mm.roots, "a declared root is always in the root set"
    # Even with no relative import to set a floor, `--root src` is the answer.
    assert mm.qualname_for(root / "src" / "mypkg" / "mod.py") == "mypkg.mod"


def test_a_declared_root_is_kept_even_when_no_file_implies_it(tmp_path):
    root = _declared_namespace(tmp_path)
    mm = firstparty.ModuleMap([], declared=(root / "src",))
    assert mm.roots == [(root / "src").resolve()]
    assert mm.classify("mypkg", "other") is model.Kind.MODULE


def test_only_a_usable_declared_root_counts_as_declared_anchoring(tmp_path):
    root = _declared_namespace(tmp_path)
    mod = root / "src" / "mypkg" / "mod.py"
    files = sorted(root.rglob("*.py"))
    declared = firstparty.ModuleMap.from_paths(files, declared=(root / "src",))
    assert declared.anchored_in_declared_root(mod, relative_level=1)
    # A depth no root can hold: the best-effort qualname is not an anchor.
    assert not declared.anchored_in_declared_root(mod, relative_level=9)
    assert not firstparty.ModuleMap.from_paths(files).anchored_in_declared_root(mod, 1)
    assert not declared.anchored_in_declared_root(tmp_path.parent / "elsewhere.py")


# -- a namespace package holding a regular subpackage ------------------------
#
# `analytics/` (no `__init__.py`) around `analytics/io/__init__.py` is the
# canonical PEP 420 layout, and it defeats both rules above: `analytics` is
# inferred as a root, and `io/__init__.py` can honestly sit one package deep.
# Only a file *outside* it can settle the question.


def _namespace_with_subpackage(tmp_path: pathlib.Path) -> pathlib.Path:
    (tmp_path / "analytics" / "io").mkdir(parents=True)
    (tmp_path / "analytics" / "io" / "readers.py").write_text(
        "read = print\n", encoding="utf-8", newline="\n"
    )
    (tmp_path / "analytics" / "io" / "__init__.py").write_text(
        "from .readers import read\n", encoding="utf-8", newline="\n"
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "__init__.py").write_text("", encoding="utf-8", newline="\n")
    (tmp_path / "tests" / "test_it.py").write_text(
        "from analytics.io import read\n", encoding="utf-8", newline="\n"
    )
    return tmp_path


def _map_for(root: pathlib.Path) -> firstparty.ModuleMap:
    return firstparty.ModuleMap.from_paths(sorted(root.rglob("*.py")))


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
    mm = firstparty.ModuleMap.from_paths(sorted(root.rglob("*.py")), declared=(root / "analytics",))
    mm.demote_roots({"analytics": [root / "tests" / "test_it.py"]})
    assert mm.qualname_for(root / "analytics" / "io" / "__init__.py", 1) == "io"


def test_a_root_with_no_fallback_is_not_demoted(tmp_path):
    root = _namespace_with_subpackage(tmp_path)
    mm = firstparty.ModuleMap([root / "analytics"])
    mm.demote_roots({"analytics": [root / "tests" / "test_it.py"]})
    assert mm.qualname_for(root / "analytics" / "io" / "__init__.py", 1) == "io"


# -- an object needs positive evidence, not absence ----------------------------


def _write(root: pathlib.Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")


def _map(tmp_path: pathlib.Path, files: dict[str, str]) -> firstparty.ModuleMap:
    """``_pkg`` (``amb/__init__.py`` and ``amb/mod.py``) plus *files*."""
    root = _pkg(tmp_path)
    _write(root, files)
    return firstparty.ModuleMap([root])


def _undetermined(mm: firstparty.ModuleMap, parent: str, name: str) -> str:
    """Assert the verdict is UNDETERMINED and hand back its reason."""
    assert mm.classify(parent, name) is model.Kind.UNDETERMINED
    reason = mm.unresolved_reason(parent, name)
    assert reason
    return reason


def test_a_name_neither_on_disk_nor_bound_is_undetermined(tmp_path: pathlib.Path) -> None:
    mm = firstparty.ModuleMap([_pkg(tmp_path)])
    assert _undetermined(mm, "amb", "Nope") == (
        "'amb.Nope' is neither on disk under this run's import roots nor bound in 'amb'"
    )


def test_a_generated_version_module_is_not_an_object(tmp_path: pathlib.Path) -> None:
    """setuptools_scm's ``_version.py`` is written at build time, never checked in."""
    init = "try:\n    from amb import _version\nexcept ImportError:\n    _version = None\n"
    mm = _map(tmp_path, {"amb/__init__.py": init})
    reason = _undetermined(mm, "amb", "_version")
    assert "'amb' binds '_version' by importing its own submodule of that name" in reason


def test_a_relative_import_of_an_absent_submodule_is_not_an_object(tmp_path: pathlib.Path) -> None:
    mm = _map(tmp_path, {"amb/__init__.py": "from . import messages_pb2\n"})
    assert "importing its own submodule of that name" in _undetermined(mm, "amb", "messages_pb2")


def test_a_pyx_only_module_is_not_an_object(tmp_path: pathlib.Path) -> None:
    """The ``.pyx`` is source for an extension built out of tree; it is not importable."""
    mm = _map(tmp_path, {"amb/fast.pyx": "def f(): pass\n"})
    assert "neither on disk" in _undetermined(mm, "amb", "fast")


def test_a_sibling_portion_of_a_namespace_package_is_not_an_object(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "src"
    _write(root, {"ns/mine.py": "X = 1\n"})
    mm = firstparty.ModuleMap([root])
    assert mm.classify("ns", "mine") is model.Kind.MODULE
    reason = _undetermined(mm, "ns", "theirs")
    assert "'ns' is a namespace package, with no __init__.py to read" in reason


def test_an_extension_parent_cannot_show_what_it_binds(tmp_path: pathlib.Path) -> None:
    root = _pkg(tmp_path)
    (root / "amb" / "accel.abi3.so").touch()
    mm = firstparty.ModuleMap([root])
    assert "extension module" in _undetermined(mm, "amb.accel", "compute")


def test_a_parent_that_is_not_on_disk_is_undetermined(tmp_path: pathlib.Path) -> None:
    mm = firstparty.ModuleMap([_pkg(tmp_path)])
    assert "'amb.gen' is not a module or package on disk" in _undetermined(mm, "amb.gen", "X")


def test_a_definition_in_the_parent_is_an_object(tmp_path: pathlib.Path) -> None:
    mm = _map(tmp_path, {"amb/__init__.py": "def speed(): ...\n", "amb/plain.py": "LIMIT = 3\n"})
    assert mm.classify("amb", "speed") is model.Kind.OBJECT
    assert mm.classify("amb.plain", "LIMIT") is model.Kind.OBJECT
    assert mm.unresolved_reason("amb", "speed") == ""


# -- following a ``from M import X`` to where X comes from ---------------------


def test_an_aliased_import_of_a_submodule_on_disk_is_a_module(tmp_path: pathlib.Path) -> None:
    """``from . import _version as version``: ``amb.version`` *is* ``amb._version``."""
    mm = _map(
        tmp_path,
        {"amb/__init__.py": "from . import _version as version\n", "amb/_version.py": "v = 1\n"},
    )
    assert mm.classify("amb", "version") is model.Kind.MODULE


def test_a_reexported_submodule_of_a_subpackage_is_a_module(tmp_path: pathlib.Path) -> None:
    mm = _map(
        tmp_path,
        {
            "amb/__init__.py": "from .sub import helpers\n",
            "amb/sub/__init__.py": "",
            "amb/sub/helpers.py": "def h(): ...\n",
        },
    )
    assert mm.classify("amb", "helpers") is model.Kind.MODULE


def test_a_reexported_first_party_object_is_an_object(tmp_path: pathlib.Path) -> None:
    mm = _map(
        tmp_path,
        {"amb/__init__.py": "from .core import Widget\n", "amb/core.py": "class Widget: ...\n"},
    )
    assert mm.classify("amb", "Widget") is model.Kind.OBJECT


def test_a_reexport_chain_is_followed_to_the_end(tmp_path: pathlib.Path) -> None:
    mm = _map(
        tmp_path,
        {
            "amb/__init__.py": "from amb.a import thing\n",
            "amb/a.py": "from .b import thing\n",
            "amb/b.py": "from .c import thing\n",
            "amb/c.py": "thing = 1\n",
        },
    )
    assert mm.classify("amb", "thing") is model.Kind.OBJECT


def test_a_reexport_of_a_name_its_origin_does_not_bind_is_undetermined(
    tmp_path: pathlib.Path,
) -> None:
    mm = _map(tmp_path, {"amb/__init__.py": "from .mod import missing\n"})
    reason = _undetermined(mm, "amb", "missing")
    assert (
        "'amb' imports 'missing' from 'amb.mod', which neither binds it nor has a submodule"
        in reason
    )


def test_a_third_party_origin_is_deferred_to_the_probe(tmp_path: pathlib.Path) -> None:
    mm = _map(tmp_path, {"amb/__init__.py": "from os import path, getcwd\n"})
    for name, origin in (("path", ("os", "path")), ("getcwd", ("os", "getcwd"))):
        assert mm.classify("amb", name) is model.Kind.DEFERRED
        assert mm.deferral("amb", name) == firstparty.Deferral(frozenset(), (origin,))


def test_an_origin_without_source_is_undetermined(tmp_path: pathlib.Path) -> None:
    """``from ._speedups import f`` with ``_speedups`` an extension: no source to follow."""
    root = _pkg(tmp_path)
    (root / "amb" / "_speedups.abi3.so").touch()
    init = "try:\n    from ._speedups import f\nexcept ImportError:\n    def f(): ...\n"
    _write(root, {"amb/__init__.py": init})
    mm = firstparty.ModuleMap([root])
    assert "'amb._speedups' is an extension module" in _undetermined(mm, "amb", "f")


def test_every_binding_of_a_name_must_agree(tmp_path: pathlib.Path) -> None:
    """A pure-Python fallback beside an accelerated import: both objects, so an object."""
    both_objects = (
        "try:\n    from ._speedups import f\nexcept ImportError:\n    from ._slow import f\n"
    )
    mm = _map(
        tmp_path,
        {
            "amb/__init__.py": both_objects,
            "amb/_speedups.py": "def f(): ...\n",
            "amb/_slow.py": "def f(): ...\n",
        },
    )
    assert mm.classify("amb", "f") is model.Kind.OBJECT


def test_bindings_that_disagree_are_undetermined(tmp_path: pathlib.Path) -> None:
    mixed = "try:\n    from . import mod as impl\nexcept ImportError:\n    impl = None\n"
    mm = _map(tmp_path, {"amb/__init__.py": mixed})
    assert "more than once, to a module and to something that is not" in _undetermined(
        mm, "amb", "impl"
    )


def test_a_plain_import_binds_a_module(tmp_path: pathlib.Path) -> None:
    mm = _map(tmp_path, {"amb/__init__.py": "import json as codec\n"})
    assert mm.classify("amb", "codec") is model.Kind.MODULE


def test_a_reexport_cycle_terminates(tmp_path: pathlib.Path) -> None:
    mm = _map(
        tmp_path,
        {
            "amb/__init__.py": "from .a import x\n",
            "amb/a.py": "from .b import x\n",
            "amb/b.py": "from .a import x\n",
        },
    )
    assert "part of a circular import" in _undetermined(mm, "amb", "x")


# -- a submodule shadowed through a star import ---------------------------------


def test_a_star_import_that_binds_a_submodules_name_shadows_it(tmp_path: pathlib.Path) -> None:
    """``from ._impl import *`` brings ``_impl.helpers`` in over ``pkg/helpers.py``.

    Only direct bindings used to be read here, so this was called a plain
    module, and ``--fix`` wrote ``from amb import helpers`` / ``helpers.f()``
    -- the function ``_impl.helpers`` has no ``f``.
    """
    mm = _map(
        tmp_path,
        {
            "amb/__init__.py": "from ._impl import *\n",
            "amb/_impl.py": "def helpers():\n    return 0\n",
            "amb/helpers.py": "def f():\n    return 1\n",
        },
    )
    assert mm.classify("amb", "helpers") is model.Kind.AMBIGUOUS


def test_a_star_import_that_does_not_export_the_name_leaves_the_submodule(
    tmp_path: pathlib.Path,
) -> None:
    mm = _map(
        tmp_path,
        {
            "amb/__init__.py": "from ._impl import *\n",
            "amb/_impl.py": '__all__ = ["other"]\ndef helpers(): ...\ndef other(): ...\n',
            "amb/helpers.py": "def f(): ...\n",
        },
    )
    assert mm.classify("amb", "helpers") is model.Kind.MODULE


def test_a_star_import_this_run_cannot_read_leaves_a_submodule_undetermined(
    tmp_path: pathlib.Path,
) -> None:
    """``from os.path import *`` could bind any name, including a submodule's."""
    mm = _map(tmp_path, {"amb/__init__.py": "from os.path import *\n"})
    reason = _undetermined(mm, "amb", "mod")
    assert "'amb.mod' is a submodule, but 'amb' star-imports from 'os.path'" in reason


def test_packages_whose_star_imports_lead_back_to_each_other_terminate(
    tmp_path: pathlib.Path,
) -> None:
    mm = _map(
        tmp_path,
        {
            "amb/__init__.py": "from .sub import *\n",
            "amb/sub/__init__.py": "from amb import *\n",
            "amb/sub/mod.py": "",
        },
    )
    assert mm.classify("amb.sub", "mod") is model.Kind.UNDETERMINED
    assert mm.classify("amb", "mod") is model.Kind.UNDETERMINED


# -- source encodings, and sources that cannot be read -------------------------

_LATIN1_INIT = (
    '# -*- coding: latin-1 -*-\nGREETING = "caf\xe9"\nmod = "shadow"\nfrom .other import Q\n'
).encode("latin-1")


def test_a_latin1_init_is_read_in_its_declared_encoding(tmp_path: pathlib.Path) -> None:
    """PEP 263: the coding cookie decides, not UTF-8.

    Decoding as UTF-8 made this ``__init__`` look like it bound nothing: its
    objects came out undetermined, the shadowed ``mod`` came out a plain
    module, and the re-export guard stood down for ``Q``.
    """
    root = _pkg(tmp_path)
    (root / "amb" / "__init__.py").write_bytes(_LATIN1_INIT)
    _write(root, {"amb/other.py": "Q = 1\n"})
    mm = firstparty.ModuleMap([root])
    assert mm.classify("amb", "GREETING") is model.Kind.OBJECT
    assert mm.classify("amb", "Q") is model.Kind.OBJECT
    assert mm.classify("amb", "mod") is model.Kind.AMBIGUOUS
    assert mm.is_reexport("amb", "Q")
    assert not mm.is_reexport("amb", "GREETING")


def test_a_bom_prefixed_init_is_read(tmp_path: pathlib.Path) -> None:
    root = _pkg(tmp_path)
    (root / "amb" / "__init__.py").write_bytes(b"\xef\xbb\xbfTHING = 1\n")
    mm = firstparty.ModuleMap([root])
    assert mm.classify("amb", "THING") is model.Kind.OBJECT


def test_an_init_that_cannot_be_parsed_proves_nothing(tmp_path: pathlib.Path) -> None:
    """Unreadable is not "binds nothing": every answer is the one that cannot hurt."""
    root = _pkg(tmp_path)
    (root / "amb" / "__init__.py").write_text("def broken(:\n", encoding="utf-8", newline="\n")
    mm = firstparty.ModuleMap([root])
    # A submodule it might shadow is not called reachable...
    assert "cannot be read or parsed" in _undetermined(mm, "amb", "mod")
    # ...a name it might bind is not called an object...
    assert "cannot be parsed" in _undetermined(mm, "amb", "Thing")
    # ...and the re-export guard assumes the worst.
    assert mm.is_reexport("amb", "anything")


def test_a_submodule_behind_a_star_cycle_is_undetermined_whichever_is_asked_first(
    tmp_path: pathlib.Path,
) -> None:
    """``amb`` star-imports ``amb.t``, which star-imports ``amb`` back.

    Whether ``amb.mod`` is shadowed depends on what ``amb.t`` hands back,
    which depends on ``amb`` itself. Asking about ``amb.t`` first once read
    the cut cycle as "nothing shadows it" and called ``amb.mod`` a plain
    module, then later ambiguous; asking about ``amb`` first said
    undetermined for both.
    """
    root = _pkg(tmp_path)
    _write(root, {"amb/__init__.py": "from .t import *\n", "amb/t.py": "from . import *\n"})
    queries = [("amb.t", "mod"), ("amb", "mod")]
    for order in (queries, queries[::-1]):
        mm = firstparty.ModuleMap([root])
        verdicts = [mm.classify(*query) for query in order]
        assert verdicts == [model.Kind.UNDETERMINED] * 2, order
        assert "circular import" in mm.unresolved_reason("amb", "mod")


def _r9(tmp_path: pathlib.Path) -> pathlib.Path:
    """``pkg`` star-imports ``pkg.m``, which imports ``ver`` back from ``pkg``.

    ``pkg.ver`` is ``None`` or ``pkg.m``'s copy of it depending on which of
    the two modules is imported first -- and ``ver.py`` is generated, so not
    on disk.
    """
    root = _pkg(tmp_path)
    _write(
        root,
        {
            "amb/__init__.py": "from .m import *\n\nver = None\n",
            "amb/m.py": "from amb import ver\n",
        },
    )
    return root


def test_a_circular_reexport_is_undetermined_whichever_is_asked_first(
    tmp_path: pathlib.Path,
) -> None:
    root = _r9(tmp_path)
    queries = [("amb.m", "ver"), ("amb", "ver")]
    for order in (queries, queries[::-1]):
        mm = firstparty.ModuleMap([root])
        for parent, name in order:
            assert "part of a circular import" in _undetermined(mm, parent, name), order


def _random_package(rnd: random.Random, root: pathlib.Path) -> None:
    """Small modules wired together at random by every construct the map reads.

    Includes star imports of the package from its own submodules, which make
    the submodule-shadowing check (`firstparty.ModuleMap._star_shadowing`)
    part of a cycle.
    """
    names = ["X", "Y", "m0", "m1", "_p"]
    modules = [f"m{i}" for i in range(5)]

    def body() -> str:
        lines: list[str] = []
        for _ in range(rnd.randint(0, 5)):
            roll, target, name = rnd.random(), rnd.choice(modules), rnd.choice(names)
            if roll < 0.22:
                lines.append(f"from .{target} import *")
            elif roll < 0.26:
                lines.append("from . import *")  # the package itself, from a submodule
            elif roll < 0.3:
                lines.append("from pkg import *")
            elif roll < 0.5:
                lines.append(f"from .{target} import {name}")
            elif roll < 0.58:
                lines.append(f"from .{target} import {name} as {rnd.choice(names)}")
            elif roll < 0.7:
                lines.append(f"{name} = 1")
            elif roll < 0.75:
                lines.append(f"from os import path as {name}")
            elif roll < 0.8:
                lines.append(f"from . import {name}")
            elif roll < 0.87:
                lines.append(f"__all__ = {rnd.sample(names, rnd.randint(0, 3))!r}")
            elif roll < 0.93:
                lines.append("def __getattr__(name):\n    raise AttributeError")
            else:
                lines.append(f"import json as {name}")
        return "\n".join(lines) + "\n"

    _write(root, {"pkg/__init__.py": body(), **{f"pkg/{m}.py": body() for m in modules[:3]}})


def test_a_getattr_hook_inside_a_cycle_is_undetermined_whichever_is_asked_first(
    tmp_path: pathlib.Path,
) -> None:
    """``a`` falls back to ``__getattr__`` only if nothing binds the name -- which,
    with ``a`` and ``b`` star-importing each other, depends on who ran first."""
    root = _pkg(tmp_path)
    _write(
        root,
        {
            "amb/a.py": "from .b import *\n\ndef __getattr__(name):\n    raise AttributeError\n",
            "amb/b.py": "from .a import *\nfrom .c import X\n",
            "amb/c.py": "X = 1\n",
        },
    )
    queries = [("amb.a", "X"), ("amb.b", "X")]
    for order in (queries, queries[::-1]):
        mm = firstparty.ModuleMap([root])
        for parent, name in order:
            assert "part of a circular import" in _undetermined(mm, parent, name), order


#: The first 24 seeds of `_random_package`, plus seeds (found by searching the
#: first 3,000) that expose order dependence when a guard is taken out: when
#: `_via_from` ignores a cut, and when `_star_shadowing` reads a cut as "no
#: star import shadows this submodule".
_ORDER_SEEDS = [
    *range(24),
    *(127, 305, 508, 697, 1181, 1490, 2088, 2202),  # the `_via_from` guard
    *(63, 251, 297, 480, 492, 514, 563, 722),  # the `_star_shadowing` guard
]


@pytest.mark.parametrize("seed", _ORDER_SEEDS)
def test_verdicts_do_not_depend_on_the_order_questions_are_asked_in(
    tmp_path: pathlib.Path, seed: int
) -> None:
    """A map asked everything in a shuffled order agrees with a fresh map per question.

    Cycles are the risk: a lookup inside one is computed with the cycle cut,
    and a step that reads "nothing there" off a cut must not leak into an
    answer (see `firstparty.ModuleMap._lookup`). Seeded, so it is the same
    graphs every run, including ones known to catch that leak; an unseeded
    1,500-graph version of this is what found it.
    """
    rnd = random.Random(seed)  # noqa: S311 -- reproducible test data, not cryptography
    _random_package(rnd, tmp_path)
    parents = ["pkg", "pkg.m0", "pkg.m1", "pkg.m2"]
    queries = [(p, n) for p in parents for n in ["X", "Y", "m0", "m1", "_p"]]

    def verdict(mm: firstparty.ModuleMap, query: tuple[str, str]) -> object:
        return (mm.classify(*query), mm.deferral(*query))

    fresh = {q: verdict(firstparty.ModuleMap([tmp_path]), q) for q in queries}
    for order in range(3):
        shuffled = queries[:]
        order_rnd = random.Random(seed * 1000 + order)  # noqa: S311 -- reproducible order
        order_rnd.shuffle(shuffled)
        shared = firstparty.ModuleMap([tmp_path])
        for query in shuffled:
            assert verdict(shared, query) == fresh[query], (seed, order, query)


def test_a_very_long_reexport_chain_is_undetermined_not_a_crash(tmp_path: pathlib.Path) -> None:
    """Two hundred ``from .cN import X`` in a row used to raise ``RecursionError``."""
    length = 200
    files = {"amb/__init__.py": "from .c0 import X\n"}
    files |= {f"amb/c{i}.py": f"from .c{i + 1} import X\n" for i in range(length)}
    files[f"amb/c{length}.py"] = "X = 1\n"
    mm = _map(tmp_path, files)
    assert "too long or too tangled to follow" in _undetermined(mm, "amb", "X")
    # A short enough suffix of the same chain is still followed to the end.
    assert mm.classify(f"amb.c{length - 10}", "X") is model.Kind.OBJECT


def test_a_cyclic_knot_of_star_imports_ends_in_a_finding(tmp_path: pathlib.Path) -> None:
    """The diamond below, with its bottom star-importing the top: one big cycle.

    Nothing inside a cycle can be memoised before the cycle closes, so this
    is the shape the evaluation budget exists for.
    """
    depth = 24
    files = {"amb/__init__.py": "from .l0a import *\nfrom .l0b import *\n"}
    for level in range(depth):
        nxt = (
            f"from .l{level + 1}a import *\nfrom .l{level + 1}b import *\n"
            if level + 1 < depth
            else "from amb import *\n"
        )
        files[f"amb/l{level}a.py"] = nxt
        files[f"amb/l{level}b.py"] = nxt
    mm = _map(tmp_path, files)
    start = time.perf_counter()
    assert mm.classify("amb", "deep") is model.Kind.UNDETERMINED
    assert time.perf_counter() - start < 5


def test_a_module_getattr_can_supply_an_unbound_name(tmp_path: pathlib.Path) -> None:
    """PEP 562: ``__getattr__`` answers only for names nothing else binds."""
    init = "BOUND = 1\n\ndef __getattr__(name):\n    raise AttributeError\n"
    mm = _map(tmp_path, {"amb/__init__.py": init})
    assert "__getattr__ (PEP 562)" in _undetermined(mm, "amb", "lazy")
    assert mm.classify("amb", "BOUND") is model.Kind.OBJECT


# -- star imports --------------------------------------------------------------


def _star_chain(
    tmp_path: pathlib.Path, impl: str, init: str = "from .core import *\n"
) -> firstparty.ModuleMap:
    """``amb`` star-imports ``amb.core``, which star-imports ``amb._impl``."""
    return _map(
        tmp_path,
        {"amb/__init__.py": init, "amb/core.py": "from amb._impl import *\n", "amb/_impl.py": impl},
    )


def test_a_star_import_chain_is_followed_to_the_binding(tmp_path: pathlib.Path) -> None:
    mm = _star_chain(tmp_path, "def helper(): ...\ndef _private(): ...\n")
    assert mm.classify("amb", "helper") is model.Kind.OBJECT
    # Without ``__all__`` a star import skips underscored names.
    assert "neither on disk" in _undetermined(mm, "amb", "_private")


def test_a_star_import_is_followed_when_spelled_absolutely(tmp_path: pathlib.Path) -> None:
    mm = _star_chain(tmp_path, "def helper(): ...\n", init="from amb.core import *\n")
    assert mm.classify("amb", "helper") is model.Kind.OBJECT


def test_a_star_import_respects_a_static_all(tmp_path: pathlib.Path) -> None:
    mm = _star_chain(tmp_path, '__all__ = ["exported"]\ndef exported(): ...\ndef hidden(): ...\n')
    assert mm.classify("amb", "exported") is model.Kind.OBJECT
    assert "neither on disk" in _undetermined(mm, "amb", "hidden")


def test_a_static_all_can_export_an_underscored_name(tmp_path: pathlib.Path) -> None:
    mm = _star_chain(
        tmp_path, '__all__ = ["_exported"]\ndef _exported(): ...\n', init="from ._impl import *\n"
    )
    assert mm.classify("amb", "_exported") is model.Kind.OBJECT


def test_a_star_import_through_a_dynamic_all_is_undetermined(tmp_path: pathlib.Path) -> None:
    mm = _star_chain(tmp_path, '__all__ = ["helper"]\n__all__ += ["other"]\ndef helper(): ...\n')
    reason = _undetermined(mm, "amb", "helper")
    assert "star-imports from 'amb.core'" in reason
    assert "whose __all__ cannot be read without running it" in reason


def test_a_star_import_of_a_submodule_named_in_all_is_that_module(tmp_path: pathlib.Path) -> None:
    """``from pkg import *`` imports the submodules its ``__all__`` lists."""
    mm = _map(
        tmp_path,
        {
            "amb/__init__.py": "from .inner import *\n",
            "amb/inner/__init__.py": '__all__ = ["leaf"]\n',
            "amb/inner/leaf.py": "",
        },
    )
    assert mm.classify("amb", "leaf") is model.Kind.MODULE


def test_a_name_all_lists_but_nothing_binds_is_not_absent(tmp_path: pathlib.Path) -> None:
    """The star import would try to import ``inner.ghost`` as a submodule."""
    mm = _map(
        tmp_path,
        {
            "amb/__init__.py": "from .inner import *\n",
            "amb/inner/__init__.py": '__all__ = ["ghost"]\n',
        },
    )
    assert "__all__ lists 'ghost' without binding it" in _undetermined(mm, "amb", "ghost")


def test_a_third_party_star_import_leaves_the_name_undetermined(tmp_path: pathlib.Path) -> None:
    """Even beside a direct binding: which of the two runs last is not read here."""
    mm = _map(tmp_path, {"amb/__init__.py": "from os.path import *\ndef mine(): ...\n"})
    for name in ("join", "mine"):
        assert "'os.path', which is not first-party" in _undetermined(mm, "amb", name)


def test_a_star_import_cycle_terminates(tmp_path: pathlib.Path) -> None:
    mm = _map(
        tmp_path,
        {
            "amb/__init__.py": "from .a import *\n",
            "amb/a.py": "from .b import *\nA = 1\n",
            "amb/b.py": "from .a import *\nB = 2\n",
        },
    )
    assert mm.classify("amb", "A") is model.Kind.OBJECT
    assert mm.classify("amb", "B") is model.Kind.OBJECT
    assert "neither on disk" in _undetermined(mm, "amb", "C")


def test_a_diamond_of_star_imports_is_walked_once_per_module(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each layer star-imports both modules of the next: 2**depth paths, depth modules.

    Walked naively this is exponential -- depth 20 took over twenty seconds.
    The call counter is the real assertion; the clock only guards against a
    regression bad enough to hang the suite.
    """
    depth = 24
    files = {"amb/__init__.py": "from .l0a import *\nfrom .l0b import *\n"}
    for level in range(depth):
        nxt = (
            f"from .l{level + 1}a import *\nfrom .l{level + 1}b import *\n"
            if level + 1 < depth
            else "deep = 1\n"
        )
        files[f"amb/l{level}a.py"] = nxt
        files[f"amb/l{level}b.py"] = nxt
    mm = _map(tmp_path, files)
    calls = 0
    evaluate = firstparty.ModuleMap._evaluate

    def counting(self: firstparty.ModuleMap, module: str, name: str) -> firstparty._Evidence:
        nonlocal calls
        calls += 1
        return evaluate(self, module, name)

    monkeypatch.setattr(firstparty.ModuleMap, "_evaluate", counting)
    start = time.perf_counter()
    assert mm.classify("amb", "deep") is model.Kind.OBJECT
    assert mm.classify("amb", "absent") is model.Kind.UNDETERMINED
    assert time.perf_counter() - start < 2
    assert calls <= 2 * (2 * depth + 1)


def test_every_claimant_must_bind_the_name(tmp_path: pathlib.Path) -> None:
    """``amb/`` beside a stale ``amb.py``: which one imports is not this map's call."""
    mm = _map(tmp_path, {"amb/__init__.py": "Q = 1\n", "amb.py": "R = 2\n"})
    assert "disagree" in _undetermined(mm, "amb", "Q")


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("X = 1\n", (None, False)),
        ('__all__ = ["a", "b"]\n', (frozenset({"a", "b"}), False)),
        ('__all__: tuple[str, ...] = ("a",)\n', (frozenset({"a"}), False)),
        ('__all__ = ["a"]\n__all__ += ["b"]\n', (None, True)),
        ('__all__ = ["a"]\n__all__.extend(["b"])\n', (None, True)),
        ('if X:\n    __all__ = ["a"]\n', (None, True)),
        ("__all__ = [name for name in dir()]\n", (None, True)),
        ('__all__ = ["a", NAME]\n', (None, True)),
    ],
)
def test_only_a_single_literal_all_is_static(
    tmp_path: pathlib.Path, source: str, expected: tuple[frozenset[str] | None, bool]
) -> None:
    path = tmp_path / "m.py"
    path.write_text(source, encoding="utf-8", newline="\n")
    ns = _bindings.namespace(str(path))
    assert ns is not None
    assert (ns.all_names, ns.all_dynamic) == expected


def test_namespace_records_every_origin_of_a_name(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "m.py"
    source = "try:\n    from ._fast import f\nexcept ImportError:\n    from .slow import g as f\n"
    path.write_text(source, encoding="utf-8", newline="\n")
    ns = _bindings.namespace(str(path))
    assert ns is not None
    assert ns.from_imports["f"] == ((1, "_fast", "f"), (1, "slow", "g"))


# -- the directories the scan skips --------------------------------------------


def test_the_scan_skips_artifact_directories_at_a_root(tmp_path: pathlib.Path) -> None:
    root = _pkg(tmp_path)
    _write(root, {"build/lib/amb/__init__.py": "", "node_modules/thing/__init__.py": ""})
    mm = firstparty.ModuleMap([root])
    assert not mm.is_first_party("build")
    assert not mm.is_first_party("node_modules")
    assert mm.classify("build", "lib") is None


def test_a_package_named_build_below_a_root_is_scanned(tmp_path: pathlib.Path) -> None:
    """pip has ``pip/_internal/operations/build/``; it is a package like any other."""
    root = _pkg(tmp_path)
    _write(
        root,
        {
            "amb/ops/__init__.py": "",
            "amb/ops/build/__init__.py": "",
            "amb/ops/build/metadata.py": "def generate(): ...\n",
            "amb/dist/__init__.py": "VERSION = 1\n",
        },
    )
    mm = firstparty.ModuleMap([root])
    assert mm.classify("amb.ops", "build") is model.Kind.MODULE
    assert mm.classify("amb.ops.build", "metadata") is model.Kind.MODULE
    assert mm.classify("amb.ops.build.metadata", "generate") is model.Kind.OBJECT
    assert mm.classify("amb.dist", "VERSION") is model.Kind.OBJECT
    assert "build" in mm.submodules("amb.ops")


def test_a_directory_holding_no_python_is_still_first_party(tmp_path: pathlib.Path) -> None:
    root = _pkg(tmp_path)
    (root / "assets").mkdir()
    (root / "assets" / "logo.svg").write_text("<svg/>", encoding="utf-8", newline="\n")
    mm = firstparty.ModuleMap([root])
    assert mm.is_first_party("amb.anything")
    assert mm.is_first_party("assets")
    assert not mm.is_first_party("elsewhere")

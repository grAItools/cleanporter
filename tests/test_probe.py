"""Classifier behaviour against the real standard library."""

from __future__ import annotations

import importlib
import io
import json
import os
import pathlib
import subprocess
import sys

import pytest

from cleanporter import _probe, firstparty
from cleanporter import resolver as resolver_module


def test_submodule_of_package_is_module():
    # os.path is a module attribute of the (non-package) os module
    assert _probe.classify("os", "path") is True
    # collections.abc is a real submodule of the collections package
    assert _probe.classify("collections", "abc") is True


def test_object_from_module_is_violation():
    assert _probe.classify("functools", "partial") is False
    assert _probe.classify("collections", "OrderedDict") is False
    assert _probe.classify("os", "getcwd") is False
    assert _probe.classify("os.path", "join") is False


def test_unimportable_parent_is_unknown():
    assert _probe.classify("this_module_does_not_exist_xyz", "thing") is None


_LEAF = "def leaf():\n    return 1\n"


def _package_on_path(tmp_path, monkeypatch, name: str, init: str, leaf: str = _LEAF) -> None:
    """Install a two-file package under *name* and forget it again afterwards."""
    pkg = tmp_path / name
    pkg.mkdir()
    (pkg / "__init__.py").write_text(init, encoding="utf-8")
    (pkg / "leaf.py").write_text(leaf, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    for dotted in (name, f"{name}.leaf"):
        monkeypatch.delitem(sys.modules, dotted, raising=False)
    importlib.invalidate_caches()


def test_a_submodule_the_package_rebinds_to_an_object_is_ambiguous(tmp_path, monkeypatch):
    """A spec proves the submodule exists, not that the import reaches it.

    ``from .leaf import leaf`` leaves a *function* on the package, so
    ``from probe_shadow_pkg import leaf`` yields that -- and the fixer would
    qualify every use site against it. gt4py's
    ``concat_where/transform_to_as_fieldop`` is this shape, and it is why
    the third-party layer must answer it the way the filesystem layer does.
    """
    _package_on_path(tmp_path, monkeypatch, "probe_shadow_pkg", "from .leaf import leaf\n")
    assert _probe.classify("probe_shadow_pkg", "leaf") == _probe.AMBIGUOUS


def test_a_package_that_imports_its_own_submodule_is_still_a_module(tmp_path, monkeypatch):
    """``from . import leaf`` binds the module itself: nothing is shadowed."""
    _package_on_path(tmp_path, monkeypatch, "probe_plain_pkg", "from . import leaf\n")
    assert _probe.classify("probe_plain_pkg", "leaf") is True


def test_the_ambiguous_answer_survives_the_json_bridge(tmp_path, monkeypatch):
    _package_on_path(tmp_path, monkeypatch, "probe_wire_pkg", "from .leaf import leaf\n")
    flat = _probe.classify_many([("probe_wire_pkg", "leaf")])
    assert json.loads(json.dumps(flat)) == {"probe_wire_pkg\x00leaf": _probe.AMBIGUOUS}


def test_the_resolver_reports_a_probe_ambiguity_as_such(tmp_path, monkeypatch):
    """Both layers give the same verdict *and* the same reason for this shape."""
    _package_on_path(tmp_path, monkeypatch, "probe_reason_pkg", "from .leaf import leaf\n")
    r = resolver_module.Resolver(firstparty.ModuleMap([]))
    assert r.is_module("probe_reason_pkg", "leaf") is None
    assert "both a submodule" in r.reason("probe_reason_pkg", "leaf")


def test_a_batch_classifies_ancestors_before_their_descendants(tmp_path, monkeypatch):
    """Importing ``P.S`` replaces the very binding that makes ``P.S`` ambiguous.

    The import system's last act when loading ``P.S`` is
    ``setattr(P, "S", <module>)``, so classifying the pair the file *has*
    (``P.S`` -> ``X``) before the pair the fixer would *write* (``P`` ->
    ``S``) hides the shadow -- and the verdict would depend on which other
    files the run happened to include. Listing the leaf first must not
    change the answer.
    """
    _package_on_path(
        tmp_path,
        monkeypatch,
        "probe_order_pkg",
        "def leaf():\n    return 1\n",
        leaf="X = 2\n",
    )
    flat = _probe.classify_many([("probe_order_pkg.leaf", "X"), ("probe_order_pkg", "leaf")])
    assert flat["probe_order_pkg\x00leaf"] == _probe.AMBIGUOUS
    assert flat["probe_order_pkg.leaf\x00X"] is False


def test_a_lazy_module_getattr_is_never_asked(tmp_path, monkeypatch):
    """Reading the shadow must not run the package's code or import the leaf.

    A module-level ``__getattr__`` (PEP 562) is the standard lazy-submodule
    hook; asking it for the name imports the leaf, which is the one thing
    this module promises never to do -- on a scientific stack that means
    pulling in optional and GPU-backed subpackages for a verdict that does
    not change.
    """
    _package_on_path(
        tmp_path,
        monkeypatch,
        "probe_lazy_pkg",
        "import importlib\n\nASKED = []\n\n\n"
        "def __getattr__(name):\n"
        "    ASKED.append(name)\n"
        "    return importlib.import_module(f'probe_lazy_pkg.{name}')\n",
    )
    assert _probe.classify("probe_lazy_pkg", "leaf") is True
    assert sys.modules["probe_lazy_pkg"].ASKED == []
    assert "probe_lazy_pkg.leaf" not in sys.modules


def test_a_module_getattr_that_raises_does_not_take_the_run_down(tmp_path, monkeypatch):
    """``lazy_loader.attach`` raises for a leaf whose optional dep is absent.

    That is the very situation the undetermined answer exists for, so it
    must not become a traceback out of `classify` -- which is not wrapped by
    any caller, and in-process would end the run with the exit code that
    means "violations".
    """
    _package_on_path(
        tmp_path,
        monkeypatch,
        "probe_raise_pkg",
        "def __getattr__(name):\n    raise ImportError(name)\n",
    )
    assert _probe.classify("probe_raise_pkg", "leaf") is True


# -- the out-of-process bridge (final review, Important 3) ------------------


def _fake_interpreter(tmp_path: pathlib.Path, body: str) -> pathlib.Path:
    script = tmp_path / "fake-python"
    script.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    script.chmod(0o755)
    return script


def _resolver(python: pathlib.Path) -> resolver_module.Resolver:
    return resolver_module.Resolver(firstparty.ModuleMap([]), python=str(python))


def test_keys_are_nul_separated_so_the_map_is_json_safe():
    flat = _probe.classify_many([("os", "path"), ("functools", "partial")])
    assert flat == {"os\x00path": True, "functools\x00partial": False}
    assert json.loads(json.dumps(flat)) == flat


def test_main_reads_json_pairs_from_stdin_and_writes_the_map_to_stdout(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps([["os", "path"]])))
    assert _probe._main() == 0
    assert json.loads(capsys.readouterr().out) == {"os\x00path": True}


def test_main_accepts_empty_stdin(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("  \n"))
    assert _probe._main() == 0
    assert json.loads(capsys.readouterr().out) == {}


def test_the_real_subprocess_bridge_round_trips():
    """The whole wire protocol, in a separate interpreter."""
    pairs = [("os", "path"), ("functools", "partial"), ("no_such_module_xyz", "z")]
    proc = subprocess.run(
        [sys.executable, str(pathlib.Path(_probe.__file__).resolve())],
        input=json.dumps(pairs),
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(proc.stdout) == {
        "os\x00path": True,
        "functools\x00partial": False,
        "no_such_module_xyz\x00z": None,
    }


def test_the_probe_module_never_imports_cleanporter():
    source = pathlib.Path(_probe.__file__).read_text(encoding="utf-8")
    assert "cleanporter" not in source
    # ... and it really runs standalone, with the package off sys.path.
    proc = subprocess.run(
        [sys.executable, "-I", str(pathlib.Path(_probe.__file__).resolve())],
        input="[]",
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr


@pytest.mark.skipif(os.name != "posix", reason="uses a /bin/sh stub interpreter")
def test_the_ambiguous_answer_crosses_the_real_subprocess_bridge(tmp_path, monkeypatch):
    """`--python` pointing elsewhere must not lose the ambiguity.

    The stub is this very interpreter behind a different path, which is all
    it takes to make `Resolver` run the probe out of process -- so this
    exercises the JSON encoding of `_probe.AMBIGUOUS` *and*
    `Resolver._probe`'s translation of it back into "undetermined, and here
    is why", which the in-process path never touches.
    """
    _package_on_path(tmp_path, monkeypatch, "probe_bridge_pkg", "from .leaf import leaf\n")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    stub = _fake_interpreter(tmp_path, f'exec {sys.executable} "$@"')
    resolver = _resolver(stub)
    resolver.warm([("probe_bridge_pkg", "leaf")])
    assert resolver.is_module("probe_bridge_pkg", "leaf") is None
    assert "both a submodule" in resolver.reason("probe_bridge_pkg", "leaf")


@pytest.mark.skipif(os.name != "posix", reason="uses a /bin/sh stub interpreter")
def test_a_probe_that_exits_nonzero_makes_every_pair_unknown(tmp_path):
    resolver = _resolver(_fake_interpreter(tmp_path, "exit 1"))
    assert resolver.is_module("os", "path") is None
    resolver.warm([("collections", "abc"), ("functools", "partial")])
    assert resolver.is_module("collections", "abc") is None
    assert resolver.is_module("functools", "partial") is None


@pytest.mark.skipif(os.name != "posix", reason="uses a /bin/sh stub interpreter")
def test_a_probe_that_prints_garbage_makes_every_pair_unknown(tmp_path):
    resolver = _resolver(_fake_interpreter(tmp_path, "echo not-json"))
    assert resolver.is_module("os", "path") is None


@pytest.mark.skipif(os.name != "posix", reason="uses a /bin/sh stub interpreter")
def test_a_probe_that_hangs_is_killed_and_makes_every_pair_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(resolver_module, "_PROBE_TIMEOUT", 0.3)
    resolver = _resolver(_fake_interpreter(tmp_path, "sleep 30"))
    assert resolver.is_module("os", "path") is None

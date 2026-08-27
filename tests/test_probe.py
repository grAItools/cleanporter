"""Classifier behaviour against the real standard library."""

from __future__ import annotations

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

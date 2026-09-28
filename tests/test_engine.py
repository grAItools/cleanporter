"""The library API: `project.build`, `engine.run` and what they return."""

from __future__ import annotations

import dataclasses
import pathlib

import pytest

import cleanporter
from cleanporter import _source, config, engine, firstparty, model, project
from cleanporter import resolver as resolver_lib

_CONSUMER = "from demo.helpers import THING\ntotal = THING\n"
_FIXED = "from demo import helpers\ntotal = helpers.THING\n"


@pytest.fixture
def tree(tmp_path: pathlib.Path) -> pathlib.Path:
    pkg = tmp_path / "demo"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "helpers.py").write_text("THING = 42\n", encoding="utf-8")
    (pkg / "consumer.py").write_text(_CONSUMER, encoding="utf-8")
    return tmp_path


class _Recorder(engine.Listener):
    def __init__(self) -> None:
        self.events: list[tuple[str, object]] = []

    def warning(self, message: str) -> None:
        self.events.append(("warning", message))

    def error(self, finding: model.Finding) -> None:
        self.events.append(("error", finding))

    def patch(self, patch: engine.FilePatch) -> None:
        self.events.append(("patch", patch))


def test_check_reports_and_touches_nothing(tree: pathlib.Path) -> None:
    result = engine.run([tree / "demo"], config.Config(root=tree))
    assert result.mode is engine.Mode.CHECK
    assert result.files_checked == 3
    assert [(f.path.name, f.code) for f in result.findings] == [("consumer.py", "CP001")]
    assert (result.violations, result.skipped, result.unresolved) == (1, 0, 0)
    assert result.patches == ()
    assert not result.wrote
    assert result.errors == ()


def test_diff_makes_a_patch_and_writes_nothing(tree: pathlib.Path, monkeypatch) -> None:
    monkeypatch.chdir(tree)
    result = engine.run([pathlib.Path("demo")], config.Config(root=tree), engine.Mode.DIFF)
    [patch] = result.patches
    assert not patch.written
    assert not result.wrote
    assert result.changed == 1
    assert patch.before == _CONSUMER.encode()
    assert patch.after == _FIXED.encode()
    assert patch.diff.startswith(b"--- a/demo/consumer.py\n+++ b/demo/consumer.py\n")
    assert (tree / "demo" / "consumer.py").read_text(encoding="utf-8") == _CONSUMER
    # Reported against the file as it still is.
    assert [f.code for f in result.findings] == ["CP001"]


def test_fix_writes_and_reports_what_is_now_on_disk(tree: pathlib.Path) -> None:
    result = engine.run([tree / "demo"], config.Config(root=tree), engine.Mode.FIX)
    [patch] = result.patches
    assert patch.written
    assert result.wrote
    assert (tree / "demo" / "consumer.py").read_text(encoding="utf-8") == _FIXED
    assert result.findings == ()


def test_a_write_failure_is_an_error_and_keeps_its_patch(tree: pathlib.Path, monkeypatch) -> None:
    def refuse(path: pathlib.Path, data: bytes) -> None:
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(_source, "write_atomic", refuse)
    recorder = _Recorder()
    result = engine.run(
        [tree / "demo"], config.Config(root=tree), engine.Mode.FIX, listener=recorder
    )
    [error] = result.errors
    assert error.path.name == "consumer.py"
    assert "cannot write file" in error.detail
    assert result.write_errors == (error,)
    # The rewrite is not lost: the patch is kept, marked, and still applies.
    [patch] = result.patches
    assert patch.write_error == error
    assert not patch.written
    assert patch.after == _FIXED.encode()
    assert (result.changed, result.wrote) == (0, False)
    # The listener hears the error alone, which is all the command prints.
    assert recorder.events == [("error", error)]
    assert [f.code for f in result.findings] == ["CP001"]
    assert result.exit_code() == 2


def test_a_load_error_is_not_a_write_error(tree: pathlib.Path) -> None:
    (tree / "demo" / "broken.py").write_bytes(b"x = '\xff'\n")
    result = engine.run([tree / "demo"], config.Config(root=tree), engine.Mode.FIX)
    [error] = result.errors
    assert error.path.name == "broken.py"
    assert result.write_errors == ()
    assert result.exit_code() == 2


@pytest.mark.parametrize(
    ("source", "strict", "expected"),
    [
        ("from demo import helpers\ntotal = helpers.THING\n", False, 0),
        (_CONSUMER, False, 1),
        ("from nowhere_at_all_xyz import thing\n", False, 0),
        ("from nowhere_at_all_xyz import thing\n", True, 1),
        ("from demo.helpers import *\n", False, 1),
    ],
)
def test_exit_code(tree: pathlib.Path, source: str, *, strict: bool, expected: int) -> None:
    (tree / "demo" / "consumer.py").write_text(source, encoding="utf-8")
    result = engine.run([tree / "demo"], config.Config(root=tree))
    assert result.errors == ()
    assert result.exit_code(strict=strict) == expected


def test_the_listener_hears_everything_the_result_holds_in_order(tree: pathlib.Path) -> None:
    (tree / "demo" / "broken.py").write_bytes(b"x = '\xff'\n")
    recorder = _Recorder()
    result = engine.run(
        [tree / "demo", tree / "missing"],
        config.Config(root=tree),
        engine.Mode.DIFF,
        listener=recorder,
    )
    kinds = [kind for kind, _ in recorder.events]
    assert kinds == ["warning"] * len(result.warnings) + ["error", "patch"]
    assert [m for k, m in recorder.events if k == "warning"] == list(result.warnings)
    assert any("missing" in w for w in result.warnings)
    assert [e for k, e in recorder.events if k == "error"] == list(result.errors)
    assert [p for k, p in recorder.events if k == "patch"] == list(result.patches)


def test_findings_are_sorted(tree: pathlib.Path) -> None:
    (tree / "demo" / "a.py").write_text(
        "from demo.helpers import THING\nfrom os.path import join\nx = THING, join\n",
        encoding="utf-8",
    )
    result = engine.run([tree / "demo"], config.Config(root=tree))
    keys = [(str(f.path), f.line, f.column, f.code) for f in result.findings]
    assert keys == sorted(keys)
    assert len(keys) == 3


def test_a_project_is_immutable_and_has_its_evidence(tree: pathlib.Path) -> None:
    (tree / "demo" / "reexport.py").write_text("from demo.helpers import THING\n")
    (tree / "demo" / "user.py").write_text("from demo.reexport import THING\nx = THING\n")
    built = project.build([tree / "demo"], config.Config(root=tree))
    for field in dataclasses.fields(built):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(built, field.name, None)
    assert isinstance(built.records, tuple)
    # The evidence went in at construction: nobody had to remember to add it.
    assert built.resolver.is_load_bearing("demo.reexport", "THING")


def test_a_standalone_resolver_has_no_evidence(tree: pathlib.Path) -> None:
    (tree / "demo" / "reexport.py").write_text("from demo.helpers import THING\n")
    module_map = firstparty.ModuleMap([tree])
    bare = resolver_lib.Resolver(module_map, evidence=resolver_lib.NO_EVIDENCE)
    assert not bare.is_load_bearing("demo.reexport", "THING")
    evidence = resolver_lib.Evidence(uses=frozenset({("demo.reexport", "THING")}))
    with_uses = resolver_lib.Resolver(module_map, evidence=evidence)
    assert with_uses.is_load_bearing("demo.reexport", "THING")


def test_the_api_is_exported() -> None:
    assert cleanporter.run is engine.run
    assert cleanporter.Mode is engine.Mode
    assert cleanporter.RunResult is engine.RunResult
    assert cleanporter.Project is project.Project
    assert cleanporter.build is project.build


def test_dunder_all_is_accurate() -> None:
    for name in cleanporter.__all__:
        assert hasattr(cleanporter, name), name

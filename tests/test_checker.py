"""Checker behaviour tests."""

from __future__ import annotations

import libcst as cst

from cleanporter.checker import CP001, CP002, check_module
from cleanporter.config import Config

from .conftest import discover, make_resolver


def run_check(config: Config, source: str):
    resolver = make_resolver(config)
    module = cst.parse_module(source)
    findings, _ = check_module(
        module,
        config.root / "target.py",
        resolver,
        config,
        discover(config),
    )
    return findings


def codes(findings) -> list[str]:
    return [f.code for f in findings]


def test_plain_import_not_flagged(config):
    src = "import collections\nfrom mypkg import helpers\n"
    assert CP001 not in codes(run_check(config, src))


def test_object_import_flagged_with_position(config):
    src = "from mypkg.helpers import THING\n"
    findings = run_check(config, src)
    assert len([f for f in findings if f.code == CP001]) == 1
    assert findings[0].line == 1


def test_module_import_and_mixed_statement(config):
    src = "from mypkg.sub import data\nfrom mypkg.helpers import Widget\n"
    findings = run_check(config, src)
    cp001s = [f for f in findings if f.code == CP001]
    assert len(cp001s) == 1
    assert cp001s[0].line == 2


def test_alias_local_name_in_message(config):
    src = "from mypkg.helpers import Widget as Wg\n"
    (finding,) = [f for f in run_check(config, src) if f.code == CP001]
    assert "'Wg'" in finding.message


def test_relative_import_resolves(config):
    src = (
        "from .helpers import THING\n"  # anchored at target.py's containing pkg? no roots match file root...
    )
    # File is at project root, outside mypkg; anchoring fails -> CP002 warning.
    findings = run_check(config, src)
    assert all(f.code == CP002 for f in findings)


def test_relative_import_anchored_inside_package(tmp_path, make_project):
    base = make_project()
    config = Config(root=base)
    inner = base / "src" / "mypkg" / "consumer.py"
    inner.write_text("from .helpers import THING\n", encoding="utf-8")
    from cleanporter.checker import check_module

    resolver = make_resolver(config)
    module = cst.parse_module(inner.read_text(encoding="utf-8"))
    findings, _ = check_module(module, inner, resolver, config, discover(config))
    assert any(f.code == CP001 for f in findings)


def test_wildcard_skipped(config):
    src = "from mypkg.helpers import *\n"
    assert run_check(config, src) == []


def test_scope_first_party_ignores_third_party(make_project):
    base = make_project()
    config = Config(root=base, scope="first-party")
    src = "from collections import OrderedDict\n"
    assert run_check(config, src) == []


def test_unresolvable_is_cp002_warning(config):
    src = "from definitely_missing_pkg import thing\n"
    findings = run_check(config, src)
    assert codes(findings) == [CP002]

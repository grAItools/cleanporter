"""Alias conventions: parsing, matching, `CP006`, and the names ``--fix`` allocates."""

from __future__ import annotations

import json
import pathlib

import libcst as cst
import pytest
from libcst import metadata

from cleanporter import aliases, cli, engine, model, rewrite
from cleanporter import config as config_lib

HELPERS = "THING = 42\n\n\ndef go():\n    return 1\n\n\nclass Widget:\n    pass\n"


# -- parsing and validation ------------------------------------------------------


def _parse(rules: list[dict[str, object]], ruff: object = None) -> config_lib.Config:
    return config_lib._parse_table({"alias": rules}, pathlib.Path("/project"), ruff)


def test_a_rule_is_parsed_with_its_keys() -> None:
    (rule,) = _parse(
        [
            {
                "module": "gt4py.next",
                "as": "gtx",
                "importer": r"tests\..*",
                "file": "tests/.*",
                "reason": "house style",
            }
        ]
    ).alias
    assert rule == aliases.Rule(
        module="gt4py.next",
        alias="gtx",
        importer=r"tests\..*",
        file="tests/.*",
        reason="house style",
        origin="alias rule #1",
    )


def test_as_false_is_the_own_name() -> None:
    (rule,) = _parse([{"module": "numpy", "as": False}]).alias
    assert rule.alias is None


@pytest.mark.parametrize(
    ("rules", "message"),
    [
        ("numpy", "must be a list of tables"),
        (["numpy"], r"alias\[1\] must be a table"),
        ([{"module": "numpy"}], "needs a 'as' key"),
        ([{"as": "np"}], "needs a 'module' key"),
        ([{"module": "numpy", "as": "np", "alias": "x"}], "unknown keys: \\['alias'\\]"),
        ([{"module": 1, "as": "np"}], r"alias\[1\]\.module must be a string"),
        ([{"module": "numpy", "as": True}], "must be a string or false"),
        ([{"module": "numpy", "as": 3}], "must be a string or false"),
        ([{"module": "numpy", "as": "n p"}], "does not make a name"),
        ([{"module": "numpy", "as": "class"}], "does not make a name"),
        ([{"module": "numpy", "as": ""}], "does not make a name"),
        ([{"module": "numpy", "as": "__debug__"}], "does not make a name"),
        ([{"module": "numpy", "as": "{name}"}], "field other than"),
        ([{"module": "numpy", "as": "{leaf!r}"}], "field other than"),
        ([{"module": "numpy", "as": "{leaf:>3}"}], "field other than"),
        ([{"module": "numpy", "as": "np{"}], "not a valid name template"),
        ([{"module": "numpy", "as": "np}"}], "not a valid name template"),
        ([{"module": "numpy", "as": "{{leaf}}"}], "stray brace"),
        ([{"module": "num*", "as": "np"}], "not a module pattern"),
        ([{"module": "", "as": "np"}], "not a module pattern"),
        ([{"module": "a..b", "as": "np"}], "not a module pattern"),
        ([{"module": "a.***", "as": "np"}], "not a module pattern"),
        ([{"module": "numpy", "as": "np", "file": "("}], r"\.file is not a valid regex"),
        ([{"module": "numpy", "as": "np", "importer": "["}], r"\.importer is not a valid regex"),
        ([{"module": "numpy", "as": "np", "reason": 1}], r"\.reason must be a string"),
        (
            [{"module": "numpy", "as": "np"}, {"module": "numpy", "as": "npy"}],
            r"alias\[2\] repeats the unconditional module 'numpy' of tool.cleanporter.alias\[1\]",
        ),
    ],
)
def test_a_malformed_rule_is_a_configuration_error(rules: object, message: str) -> None:
    with pytest.raises(config_lib.ConfigError, match=message):
        config_lib._parse_table({"alias": rules}, pathlib.Path("/project"))


def test_a_scoped_rule_may_repeat_a_module() -> None:
    cfg = _parse(
        [
            {"module": "numpy", "as": False, "importer": "numpy.*"},
            {"module": "numpy", "as": "np", "file": "tests/.*"},
            {"module": "numpy", "as": "np"},
        ]
    )
    assert [r.origin for r in cfg.alias] == ["alias rule #1", "alias rule #2", "alias rule #3"]


@pytest.mark.parametrize("template", ["gtx_{leaf}", "{leaf}", "{leaf}_mod", "np"])
def test_a_valid_template_passes(template: str) -> None:
    aliases.check_template(template)


# -- matching -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pattern", "module", "matched"),
    [
        ("numpy", "numpy", True),
        ("numpy", "numpy.linalg", False),
        ("numpy", "numpyx", False),
        ("gt4py.next.*", "gt4py.next.ffront", True),
        ("gt4py.next.*", "gt4py.next", False),
        ("gt4py.next.*", "gt4py.next.ffront.decorator", False),
        ("gt4py.**", "gt4py.next", True),
        ("gt4py.**", "gt4py.next.ffront.decorator", True),
        ("gt4py.**", "gt4py", False),
        ("**.testing", "numpy.testing", True),
        ("**.testing", "a.b.testing", True),
        ("**.testing", "testing", False),
        ("*.linalg", "numpy.linalg", True),
        ("*.linalg", "scipy.sparse.linalg", False),
        ("a.**.z", "a.b.c.z", True),
        ("a.**.z", "a.z", False),
    ],
)
def test_module_patterns(pattern: str, module: str, *, matched: bool) -> None:
    compiled = aliases.compile_module_pattern(pattern)
    assert (compiled.fullmatch(module) is not None) is matched


def _conventions(*rules: dict[str, object]) -> aliases.Conventions:
    return _parse(list(rules)).conventions


def test_the_first_matching_rule_wins() -> None:
    conventions = _conventions(
        {"module": "gt4py.next", "as": False, "importer": r"gt4py\.next(\..*)?"},
        {"module": "gt4py.next.*", "as": "gtx_{leaf}"},
        {"module": "gt4py.next", "as": "gtx"},
    )
    inside = conventions.expected("gt4py.next", "gt4py.next.ffront.foo", "src/x.py")
    assert inside is not None
    assert inside.own_name
    assert inside.binding == "next"
    outside = conventions.expected("gt4py.next", "app.main", "src/app/main.py")
    assert outside is not None
    assert (outside.name, outside.rule.origin) == ("gtx", "alias rule #3")
    leaf = conventions.expected("gt4py.next.ffront", "app.main", "src/app/main.py")
    assert leaf is not None
    assert leaf.name == "gtx_ffront"
    assert conventions.expected("numpy", "app.main", "src/app/main.py") is None


def test_importer_and_file_must_both_match() -> None:
    conventions = _conventions(
        {"module": "numpy", "as": "np", "importer": r"app\..*", "file": "src/.*"}
    )
    assert conventions.expected("numpy", "app.x", "src/app/x.py") is not None
    assert conventions.expected("numpy", "app.x", "tests/x.py") is None
    assert conventions.expected("numpy", "lib.x", "src/lib/x.py") is None


def test_an_unknown_importer_never_matches_an_importer_pattern() -> None:
    conventions = _conventions({"module": "numpy", "as": "np", "importer": ".*"})
    assert conventions.expected("numpy", "", "script.py") is None


def test_no_rules_is_falsy_and_expects_nothing() -> None:
    conventions = aliases.Conventions()
    assert not conventions
    assert conventions.expected("numpy", "a", "a.py") is None


# -- ruff defaults ----------------------------------------------------------------


def _ruff(table: dict[str, object], *, legacy: bool = False) -> dict[str, object]:
    conventions: dict[str, object] = {"flake8-import-conventions": table}
    return conventions if legacy else {"lint": conventions}


def test_ruff_aliases_become_rules_after_cleanporters() -> None:
    cfg = _parse(
        [{"module": "numpy", "as": "numpy_"}],
        _ruff({"aliases": {"numpy": "np", "pandas": "pd"}, "extend-aliases": {"polars": "pl"}}),
    )
    assert [(r.module, r.alias, r.origin) for r in cfg.alias] == [
        ("numpy", "numpy_", "alias rule #1"),
        ("numpy", "np", "ruff alias"),
        ("pandas", "pd", "ruff alias"),
        ("polars", "pl", "ruff alias"),
    ]
    expectation = cfg.conventions.expected("numpy", "a", "a.py")
    assert expectation is not None
    assert expectation.name == "numpy_", "a cleanporter rule outranks a ruff alias"
    pandas = cfg.conventions.expected("pandas", "a", "a.py")
    assert pandas is not None
    assert "tool.ruff.lint.flake8-import-conventions.aliases" in pandas.rule.describe()


def test_extend_aliases_overrides_aliases_for_the_same_module() -> None:
    cfg = _parse([], _ruff({"aliases": {"numpy": "np"}, "extend-aliases": {"numpy": "nump"}}))
    assert [(r.module, r.alias) for r in cfg.alias] == [("numpy", "nump")]


def test_a_ruff_module_is_matched_exactly() -> None:
    cfg = _parse([], _ruff({"aliases": {"numpy": "np"}}))
    assert cfg.conventions.expected("numpy.linalg", "a", "a.py") is None


def test_the_legacy_ruff_table_is_read() -> None:
    cfg = _parse([], _ruff({"aliases": {"numpy": "np"}}, legacy=True))
    assert [(r.module, r.alias) for r in cfg.alias] == [("numpy", "np")]


def test_the_lint_table_outranks_the_legacy_one() -> None:
    ruff = {
        "flake8-import-conventions": {"aliases": {"numpy": "legacy"}},
        "lint": {"flake8-import-conventions": {"aliases": {"pandas": "pd"}}},
    }
    cfg = _parse([], ruff)
    assert [(r.module, r.alias) for r in cfg.alias] == [("pandas", "pd")]


def test_ruff_builtin_defaults_are_not_invented() -> None:
    assert _parse([], {"lint": {"select": ["ICN"]}}).alias == ()


@pytest.mark.parametrize(
    ("table", "message"),
    [
        ({"aliases": {"numpy": 1}}, r"tool\.ruff\.lint\.flake8-import-conventions\.aliases"),
        ({"aliases": {"numpy": "n p"}}, "must be an identifier"),
        ({"extend-aliases": {"numpy": "def"}}, r"extend-aliases\.'numpy' must be an identifier"),
        ({"aliases": {"numpy": "__debug__"}}, "must be an identifier"),
        ({"aliases": {"num*py": "np"}}, "is not a dotted module name"),
        ({"aliases": ["numpy"]}, "must be a table of module = alias"),
    ],
)
def test_a_malformed_ruff_table_is_a_configuration_error(
    table: dict[str, object], message: str
) -> None:
    with pytest.raises(config_lib.ConfigError, match=message):
        _parse([], _ruff(table))


def test_a_ruff_conventions_table_that_is_not_a_table_is_an_error() -> None:
    with pytest.raises(config_lib.ConfigError, match="must be a table"):
        _parse([], {"lint": {"flake8-import-conventions": "np"}})


def test_ruff_aliases_false_ignores_ruff_even_when_malformed() -> None:
    cfg = config_lib._parse_table(
        {"ruff_aliases": False}, pathlib.Path("/project"), _ruff({"aliases": {"numpy": 1}})
    )
    assert cfg.alias == ()
    assert cfg.ruff_aliases is False


def test_load_config_reads_ruff_from_the_same_pyproject(tmp_path: pathlib.Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.ruff.lint.flake8-import-conventions.extend-aliases]\n"numpy" = "np"\n',
        encoding="utf-8",
        newline="\n",
    )
    cfg = config_lib.load_config(tmp_path)
    assert [(r.module, r.alias) for r in cfg.alias] == [("numpy", "np")]


# -- CP006: checking ------------------------------------------------------------


@pytest.fixture
def project(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """A one-package project probed in process, with the cwd at its root."""
    (tmp_path / "src" / "demo" / "sub").mkdir(parents=True)
    (tmp_path / "src" / "demo" / "__init__.py").write_text("", encoding="utf-8", newline="\n")
    (tmp_path / "src" / "demo" / "sub" / "__init__.py").write_text(
        "", encoding="utf-8", newline="\n"
    )
    (tmp_path / "src" / "demo" / "helpers.py").write_text(HELPERS, encoding="utf-8", newline="\n")
    (tmp_path / "src" / "demo" / "sub" / "mod.py").write_text(
        HELPERS, encoding="utf-8", newline="\n"
    )
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _configure(project: pathlib.Path, body: str) -> None:
    (project / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0"\n\n[tool.cleanporter]\npython = "self"\n' + body,
        encoding="utf-8",
        newline="\n",
    )


def _write(project: pathlib.Path, source: str, name: str = "consumer.py") -> pathlib.Path:
    target = project / "src" / "demo" / name
    target.write_text(source, encoding="utf-8", newline="\n")
    return target


def _run(
    project: pathlib.Path, mode: engine.Mode = engine.Mode.CHECK, *, path: str = "src"
) -> engine.RunResult:
    cfg = config_lib.load_config(project)
    return engine.run([pathlib.Path(path)], cfg, mode)


def _codes(result: engine.RunResult, name: str = "consumer.py") -> list[tuple[int, str, str]]:
    return [(f.line, f.code, f.name) for f in result.findings if f.path.name == name]


JSON_JS = '[[tool.cleanporter.alias]]\nmodule = "json"\nas = "js"\n'


def test_import_as_another_name_is_a_cp006(project: pathlib.Path) -> None:
    _configure(project, JSON_JS)
    _write(project, "import json as j\n\nj.dumps(1)\n")
    result = _run(project)
    (finding,) = result.findings
    assert (finding.code, finding.parent, finding.name) == ("CP006", "json", "j")
    assert finding.message == (
        "module 'json' is bound as 'j': expected 'js' by alias rule #1 (module='json')"
    )
    assert result.alias_mismatches == 1
    assert result.exit_code() == 1


def test_a_conforming_binding_is_clean(project: pathlib.Path) -> None:
    _configure(project, JSON_JS)
    _write(project, "import json as js\n\njs.dumps(1)\n")
    result = _run(project)
    assert result.findings == ()
    assert result.exit_code() == 0


def test_a_bare_import_of_a_top_level_module_is_bound_under_its_own_name(
    project: pathlib.Path,
) -> None:
    _configure(project, JSON_JS)
    _write(project, "import json\n\njson.dumps(1)\n")
    assert _codes(_run(project)) == [(1, "CP006", "json")]
    _configure(project, '[[tool.cleanporter.alias]]\nmodule = "json"\nas = false\n')
    assert _codes(_run(project)) == []


def test_as_false_rejects_an_alias(project: pathlib.Path) -> None:
    _configure(project, '[[tool.cleanporter.alias]]\nmodule = "json"\nas = false\nreason = "r"\n')
    _write(project, "import json as js\n\njs.dumps(1)\n")
    (finding,) = _run(project).findings
    assert "expected its own name 'json'" in finding.message
    assert finding.message.endswith(": r")


def test_a_dotted_import_is_a_chain(project: pathlib.Path) -> None:
    _configure(project, '[[tool.cleanporter.alias]]\nmodule = "os.path"\nas = "osp"\n')
    _write(project, "import os.path\n\nos.path.join('a')\n")
    (finding,) = _run(project).findings
    assert (finding.code, finding.parent, finding.name) == ("CP006", "os.path", "os.path")
    assert "binds it only as a chain through 'os'" in finding.message


def test_a_dotted_import_is_the_own_name_of_a_submodule(project: pathlib.Path) -> None:
    _configure(project, '[[tool.cleanporter.alias]]\nmodule = "os.path"\nas = false\n')
    _write(project, "import os.path\n\nos.path.join('a')\n")
    assert _run(project).findings == ()


def test_a_dotted_import_also_binds_its_top_level_package(project: pathlib.Path) -> None:
    _configure(project, '[[tool.cleanporter.alias]]\nmodule = "os"\nas = "o"\n')
    _write(project, "import os.path\n\nos.path.join('a')\n")
    (finding,) = _run(project).findings
    assert (finding.code, finding.parent, finding.name) == ("CP006", "os", "os")


def test_from_import_of_a_proven_module_is_checked(project: pathlib.Path) -> None:
    _configure(project, '[[tool.cleanporter.alias]]\nmodule = "demo.*"\nas = "{leaf}_m"\n')
    _write(project, "from demo import helpers\nfrom demo import sub as sub_m\n\nhelpers, sub_m\n")
    assert _codes(_run(project)) == [(1, "CP006", "helpers")]


def test_a_relative_import_is_matched_on_its_absolute_name(project: pathlib.Path) -> None:
    _configure(project, '[[tool.cleanporter.alias]]\nmodule = "demo.helpers"\nas = "h"\n')
    _write(project, "from . import helpers as hh\n\nhh.go()\n")
    (finding,) = _run(project).findings
    assert (finding.code, finding.parent, finding.name) == ("CP006", "demo.helpers", "hh")


def test_an_object_or_an_unproven_name_is_never_a_cp006(project: pathlib.Path) -> None:
    _configure(project, '[[tool.cleanporter.alias]]\nmodule = "demo.**"\nas = "x"\n')
    _write(
        project,
        "from demo.helpers import Widget as W\nfrom demo import missing as m\n\nW, m\n",
    )
    assert sorted(code for _line, code, _name in _codes(_run(project))) == ["CP001", "CP002"]


def test_future_and_star_imports_are_never_a_cp006(project: pathlib.Path) -> None:
    _configure(project, '[[tool.cleanporter.alias]]\nmodule = "**"\nas = "x"\n')
    _write(project, "from __future__ import annotations\nfrom demo.helpers import *\n")
    assert [c for _l, c, _n in _codes(_run(project))] == ["CP003"]


def test_a_package_init_is_exempt_at_module_level_only(project: pathlib.Path) -> None:
    _configure(project, JSON_JS)
    (project / "src" / "demo" / "__init__.py").write_text(
        "import json as j\n\n\ndef f():\n    import json as jj\n\n    return jj, j\n",
        encoding="utf-8",
        newline="\n",
    )
    assert _codes(_run(project), "__init__.py") == [(5, "CP006", "jj")]


def test_importer_scoped_rules_come_first(project: pathlib.Path) -> None:
    _configure(
        project,
        '[[tool.cleanporter.alias]]\nmodule = "json"\nas = false\n'
        'importer = "demo\\\\.sub\\\\..*"\n' + JSON_JS,
    )
    (project / "src" / "demo" / "sub" / "own.py").write_text(
        "import json\n", encoding="utf-8", newline="\n"
    )
    _write(project, "import json\n")
    result = _run(project)
    assert [(f.path.name, f.code) for f in result.findings] == [("consumer.py", "CP006")]


def test_a_file_scoped_rule(project: pathlib.Path) -> None:
    _configure(project, JSON_JS.replace("as =", 'file = "src/demo/consumer\\\\.py"\nas ='))
    _write(project, "import json\n")
    _write(project, "import json\n", "other.py")
    result = _run(project)
    assert [(f.path.name, f.code) for f in result.findings] == [("consumer.py", "CP006")]


def test_a_skip_rule_turns_a_cp006_into_a_cp004(project: pathlib.Path) -> None:
    _configure(project, JSON_JS + "\n[[tool.cleanporter.skip]]\nfile = '.*consumer\\.py'\n")
    _write(project, "import json as j\n")
    result = _run(project)
    assert _codes(result) == [(1, "CP004", "j")]
    assert result.exit_code() == 0


def test_a_pinned_name_turns_a_cp006_into_a_cp004(project: pathlib.Path) -> None:
    _configure(project, JSON_JS + "\n[[tool.cleanporter.skip]]\nfunction = 'dsl'\n")
    _write(project, "import json as j\n\n\ndef dsl():\n    return j\n")
    assert _codes(_run(project)) == [(1, "CP004", "j")]


def test_an_inline_suppression_takes_a_cp006(project: pathlib.Path) -> None:
    _configure(project, JSON_JS)
    _write(project, "import json as j  # cleanporter: ignore[CP006]\n")
    result = _run(project)
    (finding,) = result.findings
    assert finding.code == "CP004"
    assert "CP006 suppressed by the inline comment on line 1" in finding.message
    assert result.exit_code() == 0


def test_an_inline_suppression_on_a_from_import_takes_a_cp006(project: pathlib.Path) -> None:
    _configure(project, '[[tool.cleanporter.alias]]\nmodule = "demo.helpers"\nas = "h"\n')
    _write(project, "from demo import helpers as hh  # cleanporter: ignore[CP006]\n\nhh.go()\n")
    assert [f.code for f in _run(project).findings] == ["CP004"]


def test_a_cp006_suppression_with_nothing_to_suppress_is_a_cp005(project: pathlib.Path) -> None:
    _configure(project, JSON_JS)
    _write(project, "import json as js  # cleanporter: ignore[CP006]\n")
    (finding,) = _run(project).findings
    assert finding.code == "CP005"
    assert "no CP006 finding" in finding.message


def test_a_cp006_is_selectable_and_ignorable(project: pathlib.Path) -> None:
    _configure(project, 'ignore = ["CP006"]\n' + JSON_JS)
    _write(project, "import json as j\n")
    result = _run(project)
    assert result.findings == ()
    assert result.exit_code() == 0
    _configure(project, 'select = ["CP006"]\n' + JSON_JS)
    _write(project, "import json as j\nfrom os.path import join\n\njoin('a')\n")
    assert [f.code for f in _run(project).findings] == ["CP006"]


def test_scope_first_party_leaves_third_party_bindings_alone(project: pathlib.Path) -> None:
    _configure(
        project,
        'scope = "first-party"\n' + JSON_JS + '[[tool.cleanporter.alias]]\nmodule = "demo.helpers"'
        '\nas = "h"\n',
    )
    _write(project, "import json as j\nfrom demo import helpers\n\nhelpers.go()\n")
    assert _codes(_run(project)) == [(2, "CP006", "helpers")]


def test_a_baseline_accepts_a_cp006(
    project: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _configure(project, JSON_JS)
    _write(project, "import json as j\n")
    assert cli.main(["--write-baseline", "baseline.json", "src"]) == 0
    recorded = json.loads((project / "baseline.json").read_text(encoding="utf-8"))["findings"]
    assert recorded == [
        {"code": "CP006", "name": "j", "parent": "json", "path": "src/demo/consumer.py"}
    ]
    # Reformatting the statement does not change its identity.
    _write(project, "\n\nimport json as j  # moved\n")
    capsys.readouterr()
    assert cli.main(["--baseline", "baseline.json", "src"]) == 0
    assert "1 in the baseline" in capsys.readouterr().out


def test_the_reports_carry_cp006(project: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    _configure(project, JSON_JS)
    _write(project, "import json as j\n")
    assert cli.main(["src"]) == 1
    out = capsys.readouterr().out
    assert "CP006 module 'json' is bound as 'j'" in out
    assert "1 alias mismatch(es)" in out
    assert cli.main(["--format", "json", "src"]) == 1
    document = json.loads(capsys.readouterr().out)
    assert document["counts"]["alias_mismatches"] == 1
    assert [(f["code"], f["level"], f["status"]) for f in document["findings"]] == [
        ("CP006", "error", model.Status.ALIAS_MISMATCH.value)
    ]
    assert cli.main(["--format", "sarif", "src"]) == 1
    sarif = json.loads(capsys.readouterr().out)
    (result,) = sarif["runs"][0]["results"]
    rules = sarif["runs"][0]["tool"]["driver"]["rules"]
    assert rules[result["ruleIndex"]]["id"] == result["ruleId"] == "CP006"


def test_ruff_aliases_are_checked_and_can_be_turned_off(project: pathlib.Path) -> None:
    ruff = '\n[tool.ruff.lint.flake8-import-conventions.aliases]\njson = "js"\n'
    _configure(project, ruff)
    _write(project, "import json\n")
    (finding,) = _run(project).findings
    assert finding.code == "CP006"
    assert "ruff" in finding.message
    _configure(project, "ruff_aliases = false\n" + ruff)
    assert _run(project).findings == ()


# -- the fixer --------------------------------------------------------------------


def _fix(project: pathlib.Path, source: str, name: str = "consumer.py") -> tuple[str, list[str]]:
    """*source* written to *name*, fixed; its new text and every finding code after."""
    target = _write(project, source, name)
    result = _run(project, engine.Mode.FIX)
    return target.read_text(encoding="utf-8"), sorted(f.code for f in result.findings)


def test_a_top_level_module_gets_its_configured_alias(project: pathlib.Path) -> None:
    _configure(project, JSON_JS)
    text, codes = _fix(project, "from json import dumps\n\ndumps(1)\n")
    assert text == "import json as js\n\njs.dumps(1)\n"
    assert codes == []


def test_a_submodule_gets_its_configured_alias(project: pathlib.Path) -> None:
    _configure(project, '[[tool.cleanporter.alias]]\nmodule = "demo.sub.mod"\nas = "m"\n')
    text, codes = _fix(project, "from demo.sub.mod import Widget\n\nWidget()\n")
    assert text == "from demo.sub import mod as m\n\nm.Widget()\n"
    assert codes == []


def test_a_leaf_template_is_rendered(project: pathlib.Path) -> None:
    _configure(project, '[[tool.cleanporter.alias]]\nmodule = "demo.*"\nas = "{leaf}_m"\n')
    text, codes = _fix(project, "from demo.helpers import go\n\ngo()\n")
    assert text == "from demo import helpers as helpers_m\n\nhelpers_m.go()\n"
    assert codes == []


def test_a_relative_import_stays_relative(project: pathlib.Path) -> None:
    _configure(project, '[[tool.cleanporter.alias]]\nmodule = "demo.helpers"\nas = "h"\n')
    text, codes = _fix(project, "from .helpers import go, Widget\n\ngo(), Widget()\n")
    assert text == "from . import helpers as h\n\nh.go(), h.Widget()\n"
    assert codes == []


def test_an_alias_equal_to_the_leaf_is_written_without_as(project: pathlib.Path) -> None:
    _configure(project, '[[tool.cleanporter.alias]]\nmodule = "demo.helpers"\nas = "helpers"\n')
    text, _codes = _fix(project, "from demo.helpers import go\n\ngo()\n")
    assert text == "from demo import helpers\n\nhelpers.go()\n"


def test_a_taken_alias_declines_the_whole_file(project: pathlib.Path) -> None:
    _configure(project, JSON_JS)
    source = "from json import dumps\nfrom demo.helpers import go\n\njs = 1\ndumps(go(), js)\n"
    target = _write(project, source)
    before = target.read_bytes()
    result = _run(project, engine.Mode.FIX)
    assert target.read_bytes() == before, "a declined file is byte-identical"
    declined = [f for f in result.findings if f.code == "CP003"]
    assert [f.detail for f in declined] == [
        (
            "configured alias 'js' for json is taken in this scope (alias rule #1 "
            "(module='json')); a different name would break the convention"
        )
    ]


def test_a_taken_own_name_declines_rather_than_suffixing(project: pathlib.Path) -> None:
    source = "from demo.helpers import go\n\nhelpers = 1\ngo(helpers)\n"
    _configure(project, "")
    text, _ = _fix(project, source)
    assert text == "from demo import helpers as helpers_2\n\nhelpers = 1\nhelpers_2.go(helpers)\n"
    _configure(project, '[[tool.cleanporter.alias]]\nmodule = "demo.helpers"\nas = false\n')
    text, codes = _fix(project, source)
    assert text == source
    assert "CP003" in codes


def test_a_function_local_alias_is_taken_in_its_scope(project: pathlib.Path) -> None:
    _configure(project, JSON_JS)
    source = "def f():\n    from json import dumps\n    js = 2\n    return dumps(js)\n"
    text, codes = _fix(project, source)
    assert text == source
    assert "CP003" in codes


def test_an_existing_nonconforming_binding_is_renamed_and_reused(project: pathlib.Path) -> None:
    _configure(project, JSON_JS)
    text, codes = _fix(project, "import json as j\nfrom json import dumps\n\ndumps(j)\n")
    assert text == "import json as js\n\njs.dumps(js)\n"
    assert codes == []


def test_rules_for_other_modules_leave_the_output_unchanged(project: pathlib.Path) -> None:
    source = "from json import dumps\nfrom demo.sub.mod import go\n\njson = 1\ndumps(go(), json)\n"
    _configure(project, "")
    plain, _ = _fix(project, source)
    _configure(
        project,
        '[[tool.cleanporter.alias]]\nmodule = "numpy"\nas = "np"\n'
        '\n[tool.ruff.lint.flake8-import-conventions.aliases]\npandas = "pd"\n',
    )
    configured, _ = _fix(project, source)
    assert configured == plain
    assert "json_2" in plain


@pytest.mark.parametrize(
    ("rules", "expected"),
    [
        ("", "import json  # cleanporter: ignore[CP002]\n\njson.dumps(1)\n"),
        (JSON_JS, "import json as js  # cleanporter: ignore[CP002]\n\njs.dumps(1)\n"),
    ],
    ids=["no_rules", "rule"],
)
def test_a_suppression_carried_onto_a_new_plain_import_goes_with_it(
    project: pathlib.Path, rules: str, expected: str
) -> None:
    # As before alias rules existed: the rewrite goes ahead, and the comment,
    # now covering an import with no CP002, is reported unused.
    _configure(project, rules)
    text, codes = _fix(
        project, "from json import dumps  # cleanporter: ignore[CP002]\n\ndumps(1)\n"
    )
    assert text == expected
    assert codes == ["CP005"]


FIX_CASES = {
    "top_level": "from json import dumps, loads\n\ndumps(loads('1'))\n",
    "submodule": "from demo.sub.mod import Widget\n\nWidget()\n",
    "relative": "from .helpers import go\n\ngo()\n",
    "relative_up": "from ..helpers import go\n\ngo()\n",
    "function_scope": "def f():\n    from demo.helpers import go\n    return go()\n",
    "mixed": "from json import dumps\nfrom demo.helpers import go, THING\n\ndumps(go(), THING)\n",
    "string_annotation": (
        "from __future__ import annotations\nfrom demo.helpers import Widget\n\n\n"
        "def f(w: 'Widget') -> Widget:\n    return w\n"
    ),
}

RULES = (
    '[[tool.cleanporter.alias]]\nmodule = "json"\nas = "js"\n'
    '[[tool.cleanporter.alias]]\nmodule = "demo.sub.*"\nas = "sub_{leaf}"\n'
    '[[tool.cleanporter.alias]]\nmodule = "demo.helpers"\nas = "h"\nfile = ".*/sub/.*"\n'
    '[[tool.cleanporter.alias]]\nmodule = "demo.**"\nas = "d_{leaf}"\n'
)


@pytest.mark.parametrize("source", FIX_CASES.values(), ids=FIX_CASES.keys())
def test_fix_never_introduces_a_cp006(project: pathlib.Path, source: str) -> None:
    _configure(project, RULES)
    name = "sub/consumer.py" if "..helpers" in source else "consumer.py"
    target = _write(project, source, name)
    fixed = _run(project, engine.Mode.FIX)
    assert fixed.changed == 1, target.read_text(encoding="utf-8")
    assert fixed.alias_mismatches == 0
    # And a fresh check of what was written agrees.
    assert _run(project).alias_mismatches == 0
    assert target.read_text(encoding="utf-8") != source


# -- a new binding must not capture a builtin the scope reads -----------------------


def _list_module(project: pathlib.Path) -> None:
    (project / "src" / "demo" / "list.py").write_text(HELPERS, encoding="utf-8", newline="\n")


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "from demo.list import go\n\nx = list(go())\n",
            "from demo import list as list_2\n\nx = list(list_2.go())\n",
        ),
        (
            "from demo.list import go\n\n\ndef f():\n    return list(go())\n",
            "from demo import list as list_2\n\n\ndef f():\n    return list(list_2.go())\n",
        ),
        (
            "from demo.list import go\n\nx = undefined_list_name\nlist = go\n",
            "from demo import list as list_2\n\nx = undefined_list_name\nlist = list_2.go\n",
        ),
    ],
    ids=["same_scope", "nested_function", "assigned"],
)
def test_without_a_rule_a_read_builtin_is_suffixed(
    project: pathlib.Path, source: str, expected: str
) -> None:
    _configure(project, "")
    _list_module(project)
    text, _codes = _fix(project, source)
    assert text == expected


def test_a_builtin_read_only_outside_the_binding_scope_is_free(project: pathlib.Path) -> None:
    _configure(project, "")
    _list_module(project)
    source = "def f():\n    from demo.list import go\n    return go()\n\n\nx = list()\n"
    text, _codes = _fix(project, source)
    assert text == "def f():\n    from demo import list\n    return list.go()\n\n\nx = list()\n"


def test_an_unbound_read_is_taken_too(project: pathlib.Path) -> None:
    _configure(project, "")
    source = "from demo.helpers import go\n\n\ndef f():\n    return helpers, go()\n"
    text, _codes = _fix(project, source)
    assert text.startswith("from demo import helpers as helpers_2\n")


@pytest.mark.parametrize(
    "source",
    [
        "from json import dumps\n\nx = str(dumps(1))\n",
        "from json import dumps\n\n\ndef f():\n    return str(dumps(1))\n",
    ],
    ids=["same_scope", "nested_function"],
)
def test_with_a_rule_a_read_builtin_declines(project: pathlib.Path, source: str) -> None:
    _configure(project, '[[tool.cleanporter.alias]]\nmodule = "json"\nas = "str"\n')
    text, codes = _fix(project, source)
    assert text == source
    assert "CP003" in codes


# -- a template that renders an unusable name for one module ----------------------

TEMPLATE_IF = '[[tool.cleanporter.alias]]\nmodule = "demo.*"\nas = "i{leaf}"\n'


def _f_module(project: pathlib.Path) -> None:
    (project / "src" / "demo" / "f.py").write_text(HELPERS, encoding="utf-8", newline="\n")


def test_a_template_rendering_a_keyword_is_reported_by_check(project: pathlib.Path) -> None:
    _configure(project, TEMPLATE_IF)
    _f_module(project)
    _write(project, "from demo import f\nfrom demo import helpers as ihelpers\n\nf, ihelpers\n")
    (finding,) = _run(project).findings
    assert (finding.code, finding.parent, finding.name) == ("CP006", "demo.f", "f")
    assert "renders its template 'i{leaf}' as 'if'" in finding.message


def test_a_template_rendering_a_keyword_declines_the_fix(
    project: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _configure(project, TEMPLATE_IF)
    _f_module(project)
    source = "from demo.f import go\n\ngo()\n"
    target = _write(project, source)
    assert cli.main(["--fix", "src"]) == 1
    assert target.read_text(encoding="utf-8") == source
    err = capsys.readouterr().err
    assert "CP003" in err
    assert "as 'if' for demo.f, which no import can bind" in err


def test_a_builtin_read_in_a_string_annotation_is_suffixed(project: pathlib.Path) -> None:
    _configure(project, "")
    _list_module(project)
    source = (
        "from __future__ import annotations\nfrom demo.list import go\n\n\n"
        'def f(x: "list[int]"):\n    return go()\n'
    )
    text, _codes = _fix(project, source)
    assert text == (
        "from __future__ import annotations\nfrom demo import list as list_2\n\n\n"
        'def f(x: "list[int]"):\n    return list_2.go()\n'
    )


def test_an_undefined_name_in_a_string_annotation_is_taken(project: pathlib.Path) -> None:
    _configure(project, "")
    source = (
        "from __future__ import annotations\nfrom demo.helpers import go\n\n\n"
        'def f(x: "helpers"):\n    return go()\n'
    )
    text, _codes = _fix(project, source)
    assert "from demo import helpers as helpers_2\n" in text
    assert 'def f(x: "helpers"):' in text


def test_with_a_rule_a_builtin_in_a_string_annotation_declines(project: pathlib.Path) -> None:
    _configure(project, '[[tool.cleanporter.alias]]\nmodule = "json"\nas = "str"\n')
    source = 'from json import dumps\n\n\ndef f(x: "str"):\n    return dumps(x)\n'
    text, codes = _fix(project, source)
    assert text == source
    assert "CP003" in codes


# -- renaming a non-conforming binding (--fix on a CP006) ---------------------------

SUB_MOD = '[[tool.cleanporter.alias]]\nmodule = "demo.sub.mod"\nas = "sm"\n'
OWN_JSON = '[[tool.cleanporter.alias]]\nmodule = "json"\nas = false\n'
OWN_MOD = '[[tool.cleanporter.alias]]\nmodule = "demo.sub.mod"\nas = false\n'


def _fixed_clean(project: pathlib.Path, source: str, name: str = "consumer.py") -> str:
    """*source* fixed; asserts the fix left no finding and a re-check agrees."""
    text, codes = _fix(project, source, name)
    assert codes == [], text
    assert _run(project).findings == ()
    return text


@pytest.mark.parametrize(
    ("rules", "source", "expected"),
    [
        (JSON_JS, "import json as j\n\nj.dumps(1)\n", "import json as js\n\njs.dumps(1)\n"),
        (OWN_JSON, "import json as j\n\nj.dumps(1)\n", "import json\n\njson.dumps(1)\n"),
        (JSON_JS, "import json\n\njson.dumps(1)\n", "import json as js\n\njs.dumps(1)\n"),
        (
            SUB_MOD,
            "from demo.sub import mod as m\n\nm.go()\n",
            "from demo.sub import mod as sm\n\nsm.go()\n",
        ),
        (
            SUB_MOD,
            "from demo.sub import mod\n\nmod.go()\n",
            "from demo.sub import mod as sm\n\nsm.go()\n",
        ),
        (
            OWN_MOD,
            "from demo.sub import mod as m\n\nm.go()\n",
            "from demo.sub import mod\n\nmod.go()\n",
        ),
        (
            SUB_MOD,
            "from .sub import mod as m\n\nm.go()\n",
            "from .sub import mod as sm\n\nsm.go()\n",
        ),
        (
            '[[tool.cleanporter.alias]]\nmodule = "os.path"\nas = "osp"\n',
            "import os.path as p\n\np.join('a')\n",
            "import os.path as osp\n\nosp.join('a')\n",
        ),
        (
            '[[tool.cleanporter.alias]]\nmodule = "os.path"\nas = false\n',
            "import os.path as p\n\np.join('a')\n",
            "import os.path as path\n\npath.join('a')\n",
        ),
    ],
    ids=[
        "import_as",
        "import_as_to_own_name",
        "import",
        "from_import_as",
        "from_import",
        "from_import_as_to_own_name",
        "relative",
        "dotted_import_as",
        "dotted_import_as_to_own_leaf",
    ],
)
def test_each_statement_form_is_renamed(
    project: pathlib.Path, rules: str, source: str, expected: str
) -> None:
    _configure(project, rules)
    assert _fixed_clean(project, source) == expected


def _declined(project: pathlib.Path, source: str, name: str = "consumer.py") -> list[str]:
    """*source* under ``--fix``: asserts it is byte-identical; the CP003 reasons."""
    target = _write(project, source, name)
    before = target.read_bytes()
    result = _run(project, engine.Mode.FIX)
    assert target.read_bytes() == before, target.read_text(encoding="utf-8")
    return [f.detail for f in result.findings if f.code == "CP003"]


def test_a_dotted_import_without_as_is_declined(project: pathlib.Path) -> None:
    _configure(project, '[[tool.cleanporter.alias]]\nmodule = "os"\nas = "osm"\n')
    (reason,) = _declined(project, "import os.path\nfrom json import dumps\n\ndumps(os.sep)\n")
    assert "`import os.path` binds 'os', which every other 'os.*' use" in reason


def test_only_the_one_alias_of_a_multi_name_statement_changes(project: pathlib.Path) -> None:
    _configure(project, JSON_JS)
    source = "import os, json as j, sys  # keep me\n\nj.dumps(os.sep, sys.argv)\n"
    assert _fixed_clean(project, source) == (
        "import os, json as js, sys  # keep me\n\njs.dumps(os.sep, sys.argv)\n"
    )


def test_a_parenthesised_from_import_keeps_its_comments(project: pathlib.Path) -> None:
    _configure(project, '[[tool.cleanporter.alias]]\nmodule = "demo.helpers"\nas = "hp"\n')
    source = (
        "from demo import (  # the modules\n"
        "    sub,  # one\n"
        "    helpers as h,  # two\n"
        ")\n\nsub, h.go()\n"
    )
    assert _fixed_clean(project, source) == source.replace("helpers as h", "helpers as hp").replace(
        "h.go", "hp.go"
    )


def test_dropping_an_as_across_a_line_break_is_declined(project: pathlib.Path) -> None:
    _configure(project, OWN_MOD)
    source = "from demo.sub import (\n    mod\n    as m,\n)\n\nm.go()\n"
    (reason,) = _declined(project, source)
    assert "dropping `as m`" in reason


def test_references_in_every_nested_scope_are_renamed(project: pathlib.Path) -> None:
    _configure(project, JSON_JS)
    source = (
        "import json as j\n\n\n"
        "def outer():\n"
        "    def inner():\n"
        "        return j.dumps(1)\n"
        "    return inner, lambda: j\n\n\n"
        "class C:\n"
        "    x = j.dumps(2)\n\n"
        "    def m(self):\n"
        "        return [j.loads(s) for s in ()], {k: j for k in ()}\n\n\n"
        "y = f'{j.dumps(3)}'\n"
    )
    assert _fixed_clean(project, source) == source.replace("j.", "js.").replace(
        "import json as j\n", "import json as js\n"
    ).replace(": j\n", ": js\n").replace(": j for", ": js for")


def test_a_function_local_binding_and_its_closure_are_renamed(project: pathlib.Path) -> None:
    _configure(project, JSON_JS)
    source = (
        "def f():\n"
        "    import json as j\n\n"
        "    def g():\n"
        "        return j.dumps(1)\n\n"
        "    return g\n"
    )
    assert _fixed_clean(project, source) == source.replace(" j\n", " js\n").replace(" j.", " js.")


def test_an_unread_function_local_binding_is_renamed(project: pathlib.Path) -> None:
    _configure(project, JSON_JS)
    source = "def f():\n    import json as j\n"
    assert _fixed_clean(project, source) == "def f():\n    import json as js\n"


def test_string_annotations_are_renamed_under_future_annotations(
    project: pathlib.Path,
) -> None:
    _configure(project, JSON_JS)
    source = (
        "from __future__ import annotations\n\nimport json as j\n\n\n"
        'def f(x: "j.JSONDecoder", y: "list[j.JSONEncoder]") -> j.JSONEncoder:\n'
        "    return j.JSONEncoder()\n"
    )
    assert _fixed_clean(project, source) == (
        "from __future__ import annotations\n\nimport json as js\n\n\n"
        'def f(x: "js.JSONDecoder", y: "list[js.JSONEncoder]") -> js.JSONEncoder:\n'
        "    return js.JSONEncoder()\n"
    )


def test_a_string_annotation_without_future_annotations_is_declined(
    project: pathlib.Path,
) -> None:
    _configure(project, JSON_JS)
    source = 'import json as j\n\n\ndef f(x: "j.JSONDecoder"):\n    return j.dumps(x)\n'
    (reason,) = _declined(project, source)
    assert reason == "name 'j' appears in a string literal"


def test_a_string_annotation_seeing_a_different_binding_is_left_alone(
    project: pathlib.Path,
) -> None:
    # The annotation in `g`'s body reads g's own `j`, not the import: renaming
    # it would point it at the wrong binding. Left alone, the guard declines.
    _configure(project, JSON_JS)
    source = (
        "from __future__ import annotations\n\nimport json as j\n\n\n"
        'def g(j: int):\n    x: "j" = j\n    return x\n\n\ny: "j.JSONDecoder" = j.dumps(1)\n'
    )
    (reason,) = _declined(project, source)
    assert reason == "name 'j' appears in a string literal"


TC_SOURCE = (
    "from typing import TYPE_CHECKING\n\n"
    "if TYPE_CHECKING:\n    import json as j\n\n\n"
    "def f(x: j.JSONDecoder) -> None:\n    return None\n"
)


def test_a_type_checking_binding_is_renamed_under_future_annotations(
    project: pathlib.Path,
) -> None:
    _configure(project, JSON_JS)
    source = "from __future__ import annotations\n\n" + TC_SOURCE
    assert _fixed_clean(project, source) == source.replace(" j\n", " js\n").replace(" j.", " js.")


def test_a_type_checking_binding_without_future_annotations_is_declined(
    project: pathlib.Path,
) -> None:
    _configure(project, JSON_JS)
    (reason,) = _declined(project, TC_SOURCE)
    assert reason.startswith("TYPE_CHECKING-gated import")


def test_a_suppressed_cp006_is_kept_and_other_renames_go_ahead(project: pathlib.Path) -> None:
    _configure(project, JSON_JS + SUB_MOD)
    source = (
        "import json as j  # cleanporter: ignore[CP006]\n"
        "from demo.sub import mod as m\n\nj.dumps(m.go())\n"
    )
    text, codes = _fix(project, source)
    assert text == (
        "import json as j  # cleanporter: ignore[CP006]\n"
        "from demo.sub import mod as sm\n\nj.dumps(sm.go())\n"
    )
    assert codes == ["CP004"]


def test_a_suppression_on_the_renamed_statement_keeps_its_coverage(
    project: pathlib.Path,
) -> None:
    _configure(project, SUB_MOD)
    (project / "src" / "demo" / "sub" / "__init__.py").write_text(
        "THING = 1\n", encoding="utf-8", newline="\n"
    )
    source = "from demo.sub import mod as m, THING  # cleanporter: ignore[CP001]\n\nm.go(), THING\n"
    text, codes = _fix(project, source)
    assert text == (
        "from demo.sub import mod as sm, THING  # cleanporter: ignore[CP001]\n\nsm.go(), THING\n"
    )
    assert codes == ["CP004"]


def test_a_skip_rule_pinning_the_name_leaves_it_and_other_renames_go_ahead(
    project: pathlib.Path,
) -> None:
    _configure(project, JSON_JS + SUB_MOD + '\n[[tool.cleanporter.skip]]\nfunction = "keep"\n')
    source = (
        "import json as j\nfrom demo.sub import mod as m\n\n\n"
        "def keep():\n    return j.dumps(1)\n\n\nm.go()\n"
    )
    text, codes = _fix(project, source)
    assert text == source.replace("mod as m", "mod as sm").replace("m.go", "sm.go")
    assert codes == ["CP004"]


# The interaction with the from-import fixer.


def test_a_renamed_binding_is_reused_by_a_rewritten_from_import(project: pathlib.Path) -> None:
    _configure(project, JSON_JS)
    source = "import json as j\nfrom json import dumps, loads\n\nj.JSONDecoder, dumps(loads('1'))\n"
    assert _fixed_clean(project, source) == (
        "import json as js\n\njs.JSONDecoder, js.dumps(js.loads('1'))\n"
    )


def test_a_rewritten_from_import_of_a_submodule_beside_a_rename(project: pathlib.Path) -> None:
    _configure(project, '[[tool.cleanporter.alias]]\nmodule = "os"\nas = "osm"\n')
    source = "import os as o\nfrom os.path import join\n\njoin(o.sep)\n"
    assert _fixed_clean(project, source) == (
        "import os as osm\nfrom os import path\n\npath.join(osm.sep)\n"
    )


def test_a_rename_and_a_rewrite_on_one_line(project: pathlib.Path) -> None:
    _configure(project, SUB_MOD)
    (project / "src" / "demo" / "sub" / "__init__.py").write_text(
        "THING = 1\n", encoding="utf-8", newline="\n"
    )
    source = "from demo.sub import mod as m, THING\n\nm.go(), THING\n"
    assert _fixed_clean(project, source) == (
        "from demo import sub\nfrom demo.sub import mod as sm\n\nsm.go(), sub.THING\n"
    )


def test_a_new_binding_does_not_take_a_name_a_rename_claimed(project: pathlib.Path) -> None:
    # The rename claims `helpers`; the rewritten from-import's leaf would be
    # `helpers` too, and has to be suffixed rather than collide.
    _configure(project, '[[tool.cleanporter.alias]]\nmodule = "json"\nas = "helpers"\n')
    source = "import json as j\nfrom demo.helpers import go\n\nj.dumps(go())\n"
    assert _fixed_clean(project, source) == (
        "import json as helpers\nfrom demo import helpers as helpers_2\n\n"
        "helpers.dumps(helpers_2.go())\n"
    )


def test_a_function_local_rename_beside_a_rewrite_in_its_scope_declines(
    project: pathlib.Path,
) -> None:
    # Only a module-level binding is reused by a rewritten from-import. In a
    # function the rewrite would bind a new `js`, the name the rename claims.
    _configure(project, JSON_JS)
    source = "def f():\n    import json as j\n    from json import dumps\n    return j, dumps\n"
    (reason,) = _declined(project, source)
    assert "configured alias 'js' for json is taken" in reason


def test_two_renames_to_one_name_decline(project: pathlib.Path) -> None:
    _configure(project, JSON_JS)
    source = "import json as a\n\n\ndef f():\n    import json as b\n    return a, b\n"
    (reason,) = _declined(project, source)
    assert "configured alias 'js' for json is taken" in reason


# Each guard, with the counterexample that declines the whole file.

GUARD_CASES = {
    "taken_same_scope": ("import json as j\n\njs = 1\nj.dumps(js)\n", "'j' cannot be renamed"),
    "taken_module_for_function": (
        "js = 1\n\n\ndef f():\n    import json as j\n    return j, js\n",
        "'j' cannot be renamed",
    ),
    "taken_enclosing_function": (
        (
            "def f():\n    js = 1\n\n    def g():\n        import json as j\n        return j\n\n"
            "    return g, js\n"
        ),
        "'j' cannot be renamed",
    ),
    "taken_below": (
        "import json as j\n\n\ndef f():\n    js = 2\n    return j.dumps(js)\n",
        "'j' cannot be renamed",
    ),
    "free_read_below": (
        "import json as j\n\n\ndef f():\n    return js, j\n",
        "'j' cannot be renamed",
    ),
    "rebound": ("import json as j\n\nj = j.dumps(1)\n", "rebound in the same scope"),
    "augmented": ("import json as j\n\nj += 1\n", "rebound in the same scope"),
    "loop_target": ("import json as j\n\nfor j in ():\n    pass\nj\n", "rebound in the same scope"),
    "with_target": (
        "import json as j\n\nwith open(j) as j:\n    pass\n",
        "rebound in the same scope",
    ),
    "except_target": (
        "import json as j\n\ntry:\n    pass\nexcept ValueError as j:\n    pass\nj\n",
        "rebound in the same scope",
    ),
    "walrus": ("import json as j\n\nif (j := 1):\n    pass\n", "rebound in the same scope"),
    "del": ("import json as j\n\nj.dumps(1)\ndel j\n", "unbound with `del`"),
    "global": (
        "import json as j\n\n\ndef f():\n    global j\n    return j\n\n\nj.dumps(1)\n",
        "declared global",
    ),
    "nonlocal": (
        (
            "def f():\n    import json as j\n\n"
            "    def g():\n        nonlocal j\n        return j\n\n    return g\n"
        ),
        "declared nonlocal",
    ),
    "dunder_all": ('import json as j\n\n__all__ = ["j"]\nj.dumps(1)\n', "string literal"),
    "getattr_string": (
        'import json as j\nimport sys\n\nj.dumps(getattr(sys.modules[__name__], "j"))\n',
        "string literal",
    ),
    "doctest": (
        'import json as j\n\n\ndef f():\n    """>>> j.loads(s)"""\n    return j\n',
        "string literal",
    ),
    "unread_module_level": ("import json as j\n", "never read in this file"),
    "class_body": ("class C:\n    import json as j\n\n    x = j.dumps(1)\n", "class attribute"),
    "match_capture": (
        "import json as j\n\nmatch 1:\n    case j:\n        pass\n",
        "match capture pattern",
    ),
}


@pytest.mark.parametrize(("source", "reason"), GUARD_CASES.values(), ids=list(GUARD_CASES.keys()))
def test_a_guard_hit_declines_the_whole_file(
    project: pathlib.Path, source: str, reason: str
) -> None:
    _configure(project, JSON_JS)
    # A from-import the file would otherwise have rewritten proves the decline
    # is whole-file.
    reasons = _declined(project, "from demo.helpers import go\n" + source + "go()\n")
    assert any(reason in r for r in reasons), reasons


def test_a_binding_another_file_imports_is_declined(project: pathlib.Path) -> None:
    _configure(project, JSON_JS)
    _write(project, "from demo.consumer import j\n\nj.dumps(1)\n", "other.py")
    reasons = _declined(project, "import json as j\n\nj.dumps(1)\n")
    assert any("another file imports 'j' from 'demo.consumer'" in r for r in reasons), reasons


def test_a_binding_named_by_a_dotted_string_elsewhere_is_declined(
    project: pathlib.Path,
) -> None:
    _configure(project, JSON_JS)
    _write(project, 'TARGET = "demo.consumer.j"\n', "other.py")
    reasons = _declined(project, "import json as j\n\nj.dumps(1)\n")
    assert any("is named by the string 'demo.consumer.j'" in r for r in reasons), reasons


def test_an_unusable_expectation_declines(project: pathlib.Path) -> None:
    _configure(project, TEMPLATE_IF)
    _f_module(project)
    reasons = _declined(project, "from demo import f as g\n\ng.go()\n")
    assert any("renders its template 'i{leaf}' as 'if'" in r for r in reasons), reasons


def test_an_ambiguous_read_is_detected() -> None:
    # libcst gives such a read only alongside a second assignment in the
    # binding's own scope, which the rename refuses first, so the read is built
    # by hand: two module-level assignments and a read from a function.
    scope = metadata.GlobalScope()
    ours = metadata.Assignment("j", scope, cst.Name("j"), 0)
    other = metadata.Assignment("j", scope, cst.Name("j"), 1)
    function = metadata.FunctionScope(scope, cst.Name("f"))
    access = metadata.Access(cst.Name("j"), function, is_annotation=False, is_type_hint=False)
    ours.record_access(access)
    assert not rewrite._ambiguous_reads(ours)
    access.record_assignment(ours)
    access.record_assignment(other)
    assert rewrite._ambiguous_reads(ours)


def test_a_package_init_module_level_binding_is_never_renamed(project: pathlib.Path) -> None:
    _configure(project, JSON_JS)
    init = project / "src" / "demo" / "sub" / "__init__.py"
    source = "import json as j\n\nj.dumps(1)\n"
    init.write_text(source, encoding="utf-8", newline="\n")
    result = _run(project, engine.Mode.FIX)
    assert init.read_text(encoding="utf-8") == source
    assert result.findings == ()


# The invariants.

RENAME_CASES = {
    **FIX_CASES,
    "import_as": "import json as j\n\nj.dumps(1)\n",
    "import_plain": "import json\n\njson.dumps(1)\n",
    "from_module": "from demo.sub import mod as m\n\nm.go()\n",
    "relative_module": "from .sub import mod\n\nmod.go()\n",
    "with_rewrite": "import json as j\nfrom json import dumps\nfrom demo.sub.mod import go\n\n"
    "j.loads(dumps(go()))\n",
    "function_rename": "def f():\n    import json as j\n    return j.dumps(1)\n",
}


@pytest.mark.parametrize("source", RENAME_CASES.values(), ids=RENAME_CASES.keys())
def test_a_renaming_fix_leaves_nothing_behind(project: pathlib.Path, source: str) -> None:
    _configure(project, RULES)
    name = "sub/consumer.py" if "..helpers" in source else "consumer.py"
    target = _write(project, source, name)
    before = _run(project)
    fixed = _run(project, engine.Mode.FIX)
    assert fixed.changed == 1, target.read_text(encoding="utf-8")
    assert fixed.findings == ()
    # A fresh check of what was written agrees, and reports nothing the
    # original did not.
    after = _run(project)
    assert after.findings == ()
    assert before.findings or source in FIX_CASES.values()


@pytest.mark.parametrize("source", RENAME_CASES.values(), ids=RENAME_CASES.keys())
def test_without_rules_nothing_is_renamed(project: pathlib.Path, source: str) -> None:
    # Byte-identical to the output of the fixer before renaming existed: no
    # CP006 exists without a rule, so the rename path contributes nothing.
    _configure(project, "")
    name = "sub/consumer.py" if "..helpers" in source else "consumer.py"
    target = _write(project, source, name)
    _run(project, engine.Mode.FIX)
    text = target.read_text(encoding="utf-8")
    for binding in ("js", "sm", "d_", "sub_"):
        assert f" as {binding}" not in text


# The from-import fixer's ordering, conditionality and wildcard guards, on the
# rename path.

UNLINKED = "is not tied to this import by the scope analysis"


@pytest.mark.parametrize(
    "source",
    [
        (
            "def f():\n    out = []\n    for i in range(2):\n        if i:\n"
            "            out.append(j.dumps(1))\n        else:\n            import json as j\n"
            "    return out\n"
        ),
        (
            "out = []\nfor i in range(2):\n    if i:\n        out.append(j.dumps(1))\n"
            "    else:\n        import json as j\nprint(out, j.dumps(2))\n"
        ),
        (
            "def f():\n    i = 0\n    while True:\n        if i:\n            return j.dumps(1)\n"
            "        import json as j\n        i = 1\n"
        ),
        "def f():\n    x = j\n    import json as j\n    return x, j\n\n\nj = 3\n",
    ],
    ids=["loop_in_function", "loop_at_module_level", "while_in_function", "read_before_local"],
)
def test_a_read_the_scope_analysis_does_not_tie_to_the_binding_declines(
    project: pathlib.Path, source: str
) -> None:
    _configure(project, JSON_JS)
    reasons = _declined(project, source)
    assert any(f"a read of 'j' {UNLINKED}" in r for r in reasons), reasons


def test_a_later_read_in_a_nested_function_is_still_renamed(project: pathlib.Path) -> None:
    _configure(project, JSON_JS)
    source = "def g():\n    return j.dumps(1)\n\n\nimport json as j\n\nprint(g())\n"
    assert _fixed_clean(project, source) == source.replace("j", "js").replace("jsson", "json")


def test_a_renamed_binding_below_the_reads_is_not_reused(project: pathlib.Path) -> None:
    # Reusing it would put `js.dumps(1)` above `import json as js`; binding a
    # fresh one needs the name the rename claims, so the file is declined.
    _configure(project, JSON_JS)
    reasons = _declined(
        project, 'from json import dumps\nx = dumps(1)\nimport json as j\nprint(x, j.loads("2"))\n'
    )
    assert any("configured alias 'js' for json is taken" in r for r in reasons), reasons


def test_a_conditional_binding_is_renamed_but_not_reused(project: pathlib.Path) -> None:
    _configure(project, JSON_JS)
    reasons = _declined(
        project,
        "import sys\nfrom json import dumps\n"
        "if sys.version_info < (3, 0):\n    import json as j\n    j.dumps(0)\nprint(dumps(1))\n",
    )
    assert any("configured alias 'js' for json is taken" in r for r in reasons), reasons


@pytest.mark.parametrize(
    "source",
    [
        "import json as j\nfrom demo.helpers import *\n\nprint(j.dumps(1))\n",
        "from demo.helpers import *\nimport json as j\n\nprint(j.dumps(1))\n",
    ],
    ids=["star_after", "star_before"],
)
def test_a_wildcard_import_declines_a_module_level_rename(
    project: pathlib.Path, source: str
) -> None:
    _configure(project, JSON_JS)
    reasons = _declined(project, source)
    assert any(r.startswith("the file has a wildcard import") for r in reasons), reasons


def test_a_wildcard_import_leaves_a_function_local_rename_alone(project: pathlib.Path) -> None:
    _configure(project, JSON_JS)
    source = (
        "from demo.helpers import *\n\n\ndef f():\n    import json as j\n    return j.dumps(1)\n"
    )
    text, codes = _fix(project, source)
    assert text == source.replace("as j\n", "as js\n").replace("j.dumps", "js.dumps")
    assert "CP006" not in codes

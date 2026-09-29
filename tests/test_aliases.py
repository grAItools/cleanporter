"""Alias conventions: parsing, matching, `CP006`, and the names ``--fix`` allocates."""

from __future__ import annotations

import json
import pathlib

import pytest

from cleanporter import aliases, cli, engine, model
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


def test_an_existing_nonconforming_binding_is_reused(project: pathlib.Path) -> None:
    _configure(project, JSON_JS)
    text, codes = _fix(project, "import json as j\nfrom json import dumps\n\ndumps(j)\n")
    assert text == "import json as j\n\nj.dumps(j)\n"
    assert codes == ["CP006"], "the existing binding is reported, not renamed"


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

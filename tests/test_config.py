# Copyright (c) 2026 grAItools
# SPDX-License-Identifier: BSD-3-Clause
# See LICENSE for the full license text.

"""[tool.cleanporter] loading and validation."""

from __future__ import annotations

import pathlib
import sys

import pytest

from cleanporter import config, model


def _project(tmp_path: pathlib.Path, table: str = "") -> pathlib.Path:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0"\n' + table, encoding="utf-8", newline="\n"
    )
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8", newline="\n")
    return tmp_path


def test_defaults_when_no_table(tmp_path):
    cfg = config.load_config(_project(tmp_path))
    assert cfg.root == tmp_path
    assert cfg.exclude == ()
    assert cfg.scope == "all"
    assert cfg.treat_unresolved_as_error is False
    assert "typing" in cfg.exempt_modules


def test_defaults_when_no_pyproject_at_all(tmp_path):
    cfg = config.load_config(tmp_path)
    assert cfg.root == tmp_path
    assert cfg.scope == "all"


def test_search_walks_upward_from_a_file(tmp_path):
    _project(tmp_path)
    deep = tmp_path / "pkg" / "deep" / "mod.py"
    deep.parent.mkdir(parents=True)
    deep.write_text("", encoding="utf-8", newline="\n")
    assert config.find_pyproject(deep) == tmp_path / "pyproject.toml"
    assert config.load_config(deep).root == tmp_path


def test_reads_every_key(tmp_path):
    cfg = config.load_config(
        _project(
            tmp_path,
            """
[tool.cleanporter]
exclude = ["tests/", "src/generated_*.py"]
scope = "first-party"
treat_unresolved_as_error = true
source_roots = ["src"]
exempt_modules = ["attrs"]
exempt_names = ["annotations"]
python = "python3"
skip = [{ decorator = 'gtx\\.field_operator', reason = "DSL body" }]
select = ["CP001", "cp003"]
ignore = ["CP002"]
baseline = "ci/baseline.json"
""",
        )
    )
    assert cfg.exclude == ("tests/", "src/generated_*.py")
    assert cfg.scope == "first-party"
    assert cfg.treat_unresolved_as_error is True
    assert cfg.source_roots == ("src",)
    assert cfg.python == "python3"
    assert cfg.exempt_names == frozenset({"annotations"})
    assert [(r.index, r.decorator, r.reason) for r in cfg.skip] == [
        (1, r"gtx\.field_operator", "DSL body")
    ]
    assert cfg.select == frozenset({"CP001", "CP003"})
    assert cfg.ignore == frozenset({"CP002"})
    assert cfg.baseline == tmp_path / "ci" / "baseline.json"


def test_exempt_modules_extends_rather_than_replaces_defaults(tmp_path):
    cfg = config.load_config(_project(tmp_path, '[tool.cleanporter]\nexempt_modules = ["attrs"]\n'))
    assert "attrs" in cfg.exempt_modules
    assert "typing" in cfg.exempt_modules
    assert cfg.is_exempt("attrs.validators", "instance_of") is True
    assert cfg.is_exempt("collections", "OrderedDict") is False


@pytest.mark.parametrize(
    ("table", "message"),
    [
        ('[tool.cleanporter]\nexclude = "tests/"\n', "must be a list of strings"),
        ('[tool.cleanporter]\nscope = "mine"\n', "must be one of"),
        ('[tool.cleanporter]\ntreat_unresolved_as_error = "yes"\n', "must be a boolean"),
        ("[tool.cleanporter]\nnonsense = 1\n", "unknown"),
    ],
)
def test_malformed_config_raises(tmp_path, table, message):
    with pytest.raises(config.ConfigError, match=message):
        config.load_config(_project(tmp_path, table))


def test_plain_config_still_constructs_with_no_arguments():
    assert config.Config().scope == "all"


# -- python ------------------------------------------------------------------


def _python_value(tmp_path: pathlib.Path, value: str) -> str:
    """*value* as a TOML literal string, so a backslash stays one backslash."""
    cfg = config.load_config(_project(tmp_path, f"[tool.cleanporter]\npython = '{value}'\n"))
    assert cfg.python is not None
    return cfg.python


def test_a_relative_python_path_is_read_against_the_pyproject_directory(tmp_path, monkeypatch):
    """Not against the cwd, which would name a different file from a subdirectory."""
    sub = _project(tmp_path, "[tool.cleanporter]\npython = '.venv/bin/python'\n") / "pkg"
    monkeypatch.chdir(sub)
    assert config.load_config(pathlib.Path()).python == str(tmp_path / ".venv" / "bin" / "python")


def test_a_relative_python_path_keeps_its_symlinks_and_dotdots(tmp_path):
    """A venv interpreter is a symlink whose location matters; nothing is resolved."""
    got = _python_value(tmp_path, "../envs/py/bin/python")
    assert got == str(tmp_path / ".." / "envs" / "py" / "bin" / "python")


def test_a_bare_python_command_is_left_for_the_path_lookup(tmp_path):
    """No separator means a command name: anchoring it would change its meaning."""
    assert _python_value(tmp_path, "python3") == "python3"


def test_an_absolute_python_path_is_unchanged(tmp_path):
    absolute = str(tmp_path / "venv" / "bin" / "python")
    assert _python_value(tmp_path, absolute) == absolute


def test_a_leading_tilde_in_python_is_expanded(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    assert _python_value(tmp_path, "~/venv/bin/python") == str(home / "venv" / "bin" / "python")


def test_environment_variables_in_python_are_not_expanded(tmp_path, monkeypatch):
    monkeypatch.setenv("CP_TEST_VENV", "/elsewhere")
    got = _python_value(tmp_path, "$CP_TEST_VENV/bin/python")
    assert got == str(tmp_path / "$CP_TEST_VENV" / "bin" / "python")


@pytest.mark.parametrize("value", [r"C:\venv\Scripts\python.exe", r"\\server\share\python.exe"])
def test_a_python_path_with_a_drive_and_a_root_is_unchanged(tmp_path, value):
    """Recognised on every platform: a separator test alone misses ``\\`` off Windows."""
    assert _python_value(tmp_path, value) == value


@pytest.mark.parametrize(
    ("table", "message"),
    [
        ("[tool.cleanporter]\npython = 3\n", "must be a string"),
        ('[tool.cleanporter]\npython = ""\n', "must not be empty"),
        ("[tool.cleanporter]\npython = 'C:python.exe'\n", "relative to its drive"),
        pytest.param(
            "[tool.cleanporter]\npython = '~no-such-user-cp/bin/python'\n",
            "cannot expand",
            marks=pytest.mark.skipif(
                sys.platform == "win32",
                # ntpath.expanduser guesses a sibling of the current home
                # directory for any ~user, without asking whether it exists.
                reason="Windows expands an unknown ~user instead of failing",
            ),
        ),
    ],
)
def test_malformed_python_raises(tmp_path, table, message):
    with pytest.raises(config.ConfigError, match=message):
        config.load_config(_project(tmp_path, table))


# -- paths from different projects ---------------------------------------------


def test_no_mismatch_warning_for_paths_in_one_project(tmp_path):
    _project(tmp_path)
    (tmp_path / "tests").mkdir()
    assert config.mismatch_warning([tmp_path / "pkg", tmp_path / "tests"]) is None
    assert config.mismatch_warning([]) is None


def test_mismatch_warning_names_the_config_used_and_the_paths_it_ignores(tmp_path):
    (tmp_path / "one").mkdir()
    (tmp_path / "two").mkdir()
    first = _project(tmp_path / "one")
    second = _project(tmp_path / "two")
    nested = first / "pkg" / "inner"
    nested.mkdir()
    (nested / "pyproject.toml").write_text("", encoding="utf-8", newline="\n")
    warning = config.mismatch_warning([first / "pkg", second / "pkg", nested, first])
    assert warning is not None
    assert str(first / "pyproject.toml") in warning
    assert f"{second / 'pkg'} (nearest: {second / 'pyproject.toml'})" in warning
    assert f"{nested} (nearest: {nested / 'pyproject.toml'})" in warning
    assert f"{first} (nearest" not in warning  # the path that agrees is not listed


def test_two_spellings_of_one_pyproject_do_not_warn(tmp_path, monkeypatch):
    """As on a case-insensitive filesystem, where ``Proj`` and ``proj`` are one file."""
    (tmp_path / "real").mkdir()
    real = _project(tmp_path / "real")
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(real, target_is_directory=True)
    except OSError:  # pragma: no cover - e.g. Windows without the privilege
        pytest.skip("cannot create a symlink here")
    spellings = {"a": real / "pyproject.toml", "b": alias / "pyproject.toml"}
    monkeypatch.setattr(config, "find_pyproject", lambda path, **_: spellings[path.name])
    assert config.mismatch_warning([pathlib.Path("a"), pathlib.Path("b")]) is None


def test_mismatch_warning_when_the_first_path_has_no_pyproject(tmp_path):
    bare = tmp_path / "bare"
    bare.mkdir()
    (tmp_path / "other").mkdir()
    other = _project(tmp_path / "other")
    if config.find_pyproject(bare) is not None:  # pragma: no cover - a pyproject above tmp
        pytest.skip("a pyproject.toml above the temporary directory")
    warning = config.mismatch_warning([bare, other])
    assert warning is not None
    assert "built-in defaults" in warning
    assert f"{other} (nearest: {other / 'pyproject.toml'})" in warning


#: One non-default value per known key. The test below fails if a key is added
#: without one, which is the point: the sample is what proves the parser does
#: something with the key rather than merely accepting it.
_SAMPLES = {
    "exclude": ["build/**"],
    "source_roots": ["src"],
    "exempt_modules": ["attrs"],
    "exempt_names": ["annotations"],
    "scope": "first-party",
    "python": "python3",
    "treat_unresolved_as_error": True,
    "skip": [{"decorator": "gtx\\.field_operator"}],
    "select": ["CP001"],
    "ignore": ["CP002"],
    "baseline": "cleanporter-baseline.json",
    "alias": [{"module": "numpy", "as": "np"}],
    "ruff_aliases": False,
}


def test_every_known_key_reaches_the_config(tmp_path):
    """A key that is validated but never applied would be silently ignored.

    `_KNOWN_KEYS` is what `_parse_table` accepts; the constructor call is what
    it applies. Nothing in the language ties the two together -- a key can be
    range-checked, found valid, and then left out of the `Config`, and no type
    checker sees it because the field has a default. This is that tie.

    It proves each key reaches *a* field, not the right one; two same-typed
    fields cross-wired would pass here. `test_reads_every_key` is what pins the
    mapping.
    """
    assert set(_SAMPLES) == set(config._KNOWN_KEYS), "every known key needs a sample above"

    defaults = config.Config(root=tmp_path)
    for key, value in _SAMPLES.items():
        parsed = config._parse_table({key: value}, tmp_path)
        assert getattr(parsed, key) != getattr(defaults, key), (
            f"tool.cleanporter.{key} is accepted by the parser but never reaches Config"
        )


# -- skip rules --------------------------------------------------------------


def _skip(tmp_path, table_body: str):
    return config.load_config(_project(tmp_path, "[tool.cleanporter]\n" + table_body))


def test_skip_defaults_to_no_rules(tmp_path):
    assert config.load_config(_project(tmp_path)).skip == ()


def test_skip_accepts_the_array_of_tables_spelling(tmp_path):
    cfg = _skip(
        tmp_path,
        "\n[[tool.cleanporter.skip]]\ndecorator = 'program'\nreason = 'DSL'\n",
    )
    assert [(r.index, r.decorator, r.reason) for r in cfg.skip] == [(1, "program", "DSL")]


def test_rules_are_numbered_from_one_in_order(tmp_path):
    cfg = _skip(tmp_path, "skip = [{ file = 'a' }, { file = 'b' }, { file = 'c' }]\n")
    assert [(r.index, r.file) for r in cfg.skip] == [(1, "a"), (2, "b"), (3, "c")]


def test_skip_must_be_a_list(tmp_path):
    with pytest.raises(config.ConfigError, match="must be a list of tables"):
        _skip(tmp_path, "skip = 'everything'\n")


def test_a_skip_element_must_be_a_table(tmp_path):
    with pytest.raises(config.ConfigError, match=r"skip\[1\] must be a table"):
        _skip(tmp_path, "skip = ['everything']\n")


def test_an_empty_rule_is_rejected(tmp_path):
    """It constrains nothing, so it would take the whole project."""
    with pytest.raises(config.ConfigError, match="sets no matcher"):
        _skip(tmp_path, "skip = [{}]\n")


def test_a_rule_with_only_a_reason_is_rejected(tmp_path):
    """`reason` is not a matcher, so this would skip everything silently."""
    with pytest.raises(config.ConfigError, match="sets no matcher"):
        _skip(tmp_path, "skip = [{ reason = 'just a note' }]\n")


def test_an_unknown_rule_key_is_rejected(tmp_path):
    with pytest.raises(config.ConfigError, match=r"unknown keys: \['module'\]"):
        _skip(tmp_path, "skip = [{ module = 'pkg' }]\n")


def test_a_non_string_rule_value_is_rejected(tmp_path):
    with pytest.raises(config.ConfigError, match="file must be a string"):
        _skip(tmp_path, "skip = [{ file = 3 }]\n")


def test_two_name_keys_in_one_rule_are_rejected(tmp_path):
    """They select mutually exclusive kinds, so the rule could never fire."""
    with pytest.raises(config.ConfigError, match="more than one of"):
        _skip(tmp_path, "skip = [{ class = 'X', method = 'y' }]\n")


def test_a_name_key_may_be_combined_with_file_and_decorator(tmp_path):
    cfg = _skip(tmp_path, "skip = [{ file = 'a', method = 'y', decorator = 'd' }]\n")
    assert (cfg.skip[0].file, cfg.skip[0].name, cfg.skip[0].decorator) == ("a", "y", "d")
    assert cfg.skip[0].name_key == "method"


def test_an_uncompilable_pattern_is_rejected(tmp_path):
    with pytest.raises(config.ConfigError, match="not a valid regex"):
        _skip(tmp_path, "skip = [{ decorator = '(' }]\n")


def test_a_reason_is_not_treated_as_a_pattern(tmp_path):
    """It is free text, so an unbalanced bracket in it must not be an error."""
    cfg = _skip(tmp_path, "skip = [{ file = 'a', reason = 'because ( of reasons' }]\n")
    assert cfg.skip[0].reason == "because ( of reasons"


def test_a_file_only_rule_takes_whole_files(tmp_path):
    assert _skip(tmp_path, "skip = [{ file = 'a' }]\n").skip[0].whole_file


def test_a_rule_naming_definitions_does_not_take_whole_files(tmp_path):
    rules = _skip(tmp_path, "skip = [{ file = 'a', symbol = 's' }, { decorator = 'd' }]\n").skip
    assert not rules[0].whole_file
    assert not rules[1].whole_file


# -- select / ignore / baseline ----------------------------------------------


def test_reports_follows_select_then_ignore(tmp_path: pathlib.Path) -> None:
    cfg = config.Config(
        root=tmp_path, select=frozenset({"CP001", "CP003"}), ignore=frozenset({"CP003"})
    )
    assert [c for c in ("CP001", "CP002", "CP003", "CP004") if cfg.reports(c)] == ["CP001"]
    assert all(config.Config(root=tmp_path).reports(c) for c in config.known_codes())


@pytest.mark.parametrize("key", ["select", "ignore"])
def test_an_unknown_code_is_a_config_error(tmp_path: pathlib.Path, key: str) -> None:
    match = rf"tool\.cleanporter\.{key}: unknown finding code\(s\) CP999"
    with pytest.raises(config.ConfigError, match=match):
        config.load_config(_project(tmp_path, f'[tool.cleanporter]\n{key} = ["CP001", "CP999"]\n'))


@pytest.mark.parametrize("value", ["[]", '[""]', '[" "]', '[","]', '[" , "]'])
def test_an_empty_select_is_a_config_error(tmp_path: pathlib.Path, value: str) -> None:
    """Blank entries name no code: an empty select would hide everything and exit 0."""
    with pytest.raises(config.ConfigError, match="at least one code"):
        config.load_config(_project(tmp_path, f"[tool.cleanporter]\nselect = {value}\n"))


def test_an_empty_baseline_is_a_config_error(tmp_path: pathlib.Path) -> None:
    with pytest.raises(config.ConfigError, match="baseline must be a non-empty string"):
        config.load_config(_project(tmp_path, '[tool.cleanporter]\nbaseline = ""\n'))


def test_known_codes_are_one_per_status() -> None:
    assert len(config.known_codes()) == len(model.Status)
    assert {"CP001", "CP002", "CP003", "CP004", "CP005"} <= config.known_codes()

"""[tool.cleanporter] loading and validation."""

from __future__ import annotations

import pathlib

import pytest

from cleanporter import config


def _project(tmp_path: pathlib.Path, table: str = "") -> pathlib.Path:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0"\n' + table, encoding="utf-8"
    )
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
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
    deep.write_text("", encoding="utf-8")
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
python = "/usr/bin/python3"
skip = [{ decorator = 'gtx\\.field_operator', reason = "DSL body" }]
""",
        )
    )
    assert cfg.exclude == ("tests/", "src/generated_*.py")
    assert cfg.scope == "first-party"
    assert cfg.treat_unresolved_as_error is True
    assert cfg.source_roots == ("src",)
    assert cfg.python == "/usr/bin/python3"
    assert cfg.exempt_names == frozenset({"annotations"})
    assert [(r.index, r.decorator, r.reason) for r in cfg.skip] == [
        (1, r"gtx\.field_operator", "DSL body")
    ]


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


#: One non-default value per known key. The test below fails if a key is added
#: without one, which is the point: the sample is what proves the parser does
#: something with the key rather than merely accepting it.
_SAMPLES = {
    "exclude": ["build/**"],
    "source_roots": ["src"],
    "exempt_modules": ["attrs"],
    "exempt_names": ["annotations"],
    "scope": "first-party",
    "python": "/usr/bin/python3",
    "treat_unresolved_as_error": True,
    "skip": [{"decorator": "gtx\\.field_operator"}],
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

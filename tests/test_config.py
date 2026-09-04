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
""",
        )
    )
    assert cfg.exclude == ("tests/", "src/generated_*.py")
    assert cfg.scope == "first-party"
    assert cfg.treat_unresolved_as_error is True
    assert cfg.source_roots == ("src",)
    assert cfg.python == "/usr/bin/python3"
    assert cfg.exempt_names == frozenset({"annotations"})


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

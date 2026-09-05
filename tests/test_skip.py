"""What a `[tool.cleanporter.skip]` rule matches, and what it takes with it."""

from __future__ import annotations

import pathlib

import libcst as cst
from libcst import metadata

from cleanporter import config as config_lib
from cleanporter import skip

ROOT = pathlib.Path("/project")


def regions(
    source: str, table: dict[str, str], *, path: str = "pkg/mod.py", module: str = ""
) -> skip.Skipped:
    """Regions for *source* under a single rule table, validated as config would."""
    cfg = config_lib._parse_table({"skip": [table]}, ROOT)
    tree = cst.parse_module(source)
    positions = metadata.MetadataWrapper(tree, unsafe_skip_copy=True).resolve(
        metadata.PositionProvider
    )
    return skip.regions(
        tree,
        positions,
        cfg.skip,
        skip.file_candidates(ROOT / path, ROOT),
        module,
    )


# -- file rules --------------------------------------------------------------


def test_a_file_rule_takes_the_whole_file():
    result = regions("import os\n", {"file": r"pkg/.*\.py"})
    assert result.whole_file
    assert result.covers(1) is not None
    assert result.pin("anything") is not None


def test_a_file_pattern_is_matched_in_full_not_searched():
    # `conftest` alone must not match `pkg/conftest.py`: patterns are
    # `re.fullmatch`, so a partial pattern is a rule that does not fire.
    assert not regions("import os\n", {"file": "conftest"}, path="pkg/conftest.py").whole_file
    assert regions("import os\n", {"file": r".*conftest\.py"}, path="pkg/conftest.py").whole_file


def test_a_file_outside_the_project_root_offers_its_absolute_path():
    candidates = skip.file_candidates(pathlib.Path("/elsewhere/mod.py"), ROOT)
    assert candidates == ("/elsewhere/mod.py",)


def test_a_file_inside_the_root_offers_only_the_relative_path():
    assert skip.file_candidates(ROOT / "pkg" / "mod.py", ROOT) == ("pkg/mod.py",)


# -- symbol kinds ------------------------------------------------------------

_NESTED = """\
def loose():
    def inner():
        pass


class Holder:
    def method(self):
        pass
"""


def test_function_does_not_match_a_method():
    assert regions(_NESTED, {"function": "method"}).spans == ()


def test_method_does_not_match_a_module_level_function():
    assert regions(_NESTED, {"method": "loose"}).spans == ()


def test_a_def_nested_in_a_function_is_a_function_not_a_method():
    assert regions(_NESTED, {"function": "inner"}).spans != ()
    assert regions(_NESTED, {"method": "inner"}).spans == ()


def test_symbol_matches_every_kind():
    for name in ("loose", "inner", "method", "Holder"):
        assert regions(_NESTED, {"symbol": name}).spans != (), name


def test_class_matches_only_the_class():
    (span,) = regions(_NESTED, {"class": "Holder"}).spans
    assert (span[0], span[1]) == (6, 8)


def test_the_qualified_name_is_a_candidate():
    assert regions(_NESTED, {"method": r"Holder\.method"}).spans != ()
    assert regions(_NESTED, {"method": r"Other\.method"}).spans == ()


def test_the_module_qualname_address_is_a_candidate():
    """`pkgutil.resolve_name`'s spelling, for naming one symbol precisely."""
    assert regions(_NESTED, {"method": r"pkg\.mod:Holder\.method"}, module="pkg.mod").spans != ()
    # Without a module name for the file there is no address to match.
    assert regions(_NESTED, {"method": r"pkg\.mod:Holder\.method"}).spans == ()


# -- decorators --------------------------------------------------------------

_DECORATED = """\
import gt4py.next as gtx


@gtx.program(backend=run_gtfn)
@other
def prog():
    pass


@registry["late"]
def dynamic():
    pass
"""


def test_a_decorator_matches_by_its_last_component():
    assert regions(_DECORATED, {"decorator": "program"}).spans != ()


def test_a_decorator_matches_by_its_full_dotted_name():
    assert regions(_DECORATED, {"decorator": r"gtx\.program"}).spans != ()
    assert regions(_DECORATED, {"decorator": r"other\.program"}).spans == ()


def test_a_decorator_call_is_stripped_to_its_name():
    # The pattern must not have to know about `(backend=run_gtfn)`.
    assert regions(_DECORATED, {"decorator": r"gtx\.program\(.*"}).spans == ()


def test_a_decorator_that_is_not_a_dotted_name_matches_nothing():
    assert regions(_DECORATED, {"decorator": ".*"}).spans[0][0] == 4  # only `prog`
    assert len(regions(_DECORATED, {"decorator": ".*"}).spans) == 1


def test_the_span_starts_at_the_first_decorator():
    """libCST's range for a `def` starts at the `def`; the decorators are earlier."""
    (start, end, _rule) = regions(_DECORATED, {"decorator": "program"}).spans[0]
    assert (start, end) == (4, 7)


# -- combining ---------------------------------------------------------------


def test_keys_in_one_rule_are_anded():
    assert regions(_DECORATED, {"decorator": "program", "function": "prog"}).spans != ()
    assert regions(_DECORATED, {"decorator": "program", "function": "other"}).spans == ()


def test_a_file_key_narrows_a_definition_rule():
    table = {"file": r"pkg/mod\.py", "decorator": "program"}
    assert regions(_DECORATED, table).spans != ()
    assert regions(_DECORATED, table, path="pkg/other.py").spans == ()


def test_rules_across_the_list_are_ored():
    table: dict[str, object] = {"skip": [{"function": "nothing_matches"}, {"decorator": "program"}]}
    cfg = config_lib._parse_table(table, ROOT)
    tree = cst.parse_module(_DECORATED)
    positions = metadata.MetadataWrapper(tree, unsafe_skip_copy=True).resolve(
        metadata.PositionProvider
    )
    result = skip.regions(tree, positions, cfg.skip, ("pkg/mod.py",))
    assert [rule.index for _s, _e, rule in result.spans] == [2]


# -- pins --------------------------------------------------------------------

_PINNING = """\
from gt4py.next import broadcast, minimum
from pkg.sub.mod import Thing


@field_operator
def op(a):
    return broadcast(a, ())


def plain():
    return Thing(minimum)
"""


def test_a_name_used_inside_a_region_is_pinned():
    result = regions(_PINNING, {"decorator": "field_operator"})
    assert result.pin("broadcast") is not None


def test_a_name_used_only_outside_a_region_is_not_pinned():
    result = regions(_PINNING, {"decorator": "field_operator"})
    assert result.pin("Thing") is None
    assert result.pin("minimum") is None


def test_a_name_a_string_in_the_region_refers_to_is_pinned():
    """A lazy annotation is not a `Name` node, and must still pin."""
    source = (
        "from pkg.sub.mod import Field\n\n\n"
        "@field_operator\ndef op(a: 'Field') -> 'Field':\n    return a\n"
    )
    assert regions(source, {"decorator": "field_operator"}).pin("Field") is not None


def test_an_implicitly_concatenated_annotation_pins_the_joined_name():
    """`"Fie" "ld"` is one string to Python and two `SimpleString` nodes.

    Harvesting the parts finds `Fie` and `ld` and never `Field`, which is the
    reference the annotation actually makes.
    """
    source = (
        "from pkg.sub.mod import Field\n\n\n"
        "@field_operator\ndef op(a: 'Fie' 'ld') -> None:\n    return None\n"
    )
    result = regions(source, {"decorator": "field_operator"})
    assert result.pin("Field") is not None


def test_prose_in_a_region_does_not_pin():
    """The same line `guards` already draws: prose cannot be a reference."""
    source = (
        "from pkg.sub.mod import Field\n\n\n"
        '@field_operator\ndef op(a):\n    """Returns a Field, roughly."""\n    return a\n'
    )
    assert regions(source, {"decorator": "field_operator"}).pin("Field") is None


def test_an_attribute_leaf_in_the_region_pins():
    source = "from pkg.sub import mod\n\n\n@field_operator\ndef op(a):\n    return mod.go(a)\n"
    assert regions(source, {"decorator": "field_operator"}).pin("go") is not None


def test_the_pin_covers_every_identifier_in_the_region_not_just_calls():
    # Over-collecting is the safe direction: each pinned name is one the fixer
    # will decline to rewrite.
    result = regions(_PINNING, {"decorator": "field_operator"})
    assert result.pin("a") is not None


def test_covers_is_bounded_by_the_span():
    result = regions(_PINNING, {"decorator": "field_operator"})
    assert result.covers(1) is None  # the import line
    assert result.covers(5) is not None  # the decorator
    assert result.covers(7) is not None  # the body
    assert result.covers(11) is None  # the plain function


# -- reporting ---------------------------------------------------------------


def test_describe_names_the_rule_and_its_matchers():
    rule = config_lib._parse_table({"skip": [{"decorator": "program"}]}, ROOT).skip[0]
    assert rule.describe() == "skip rule #1 (decorator='program')"


def test_describe_carries_the_reason():
    table: dict[str, object] = {
        "skip": [{"file": "a"}, {"decorator": "program", "reason": "DSL bodies"}]
    }
    rule = config_lib._parse_table(table, ROOT).skip[1]
    assert rule.describe() == "skip rule #2 (decorator='program'): DSL bodies"


# -- nothing configured ------------------------------------------------------


def test_no_rules_means_no_walk():
    assert skip.regions(cst.parse_module("import os\n"), {}, (), ("pkg/mod.py",)) is skip.EMPTY


def test_rules_that_do_not_apply_to_this_file_mean_no_walk():
    cfg = config_lib._parse_table({"skip": [{"file": "other/.*", "function": ".*"}]}, ROOT)
    assert skip.regions(cst.parse_module("def f(): pass\n"), {}, cfg.skip, ("pkg/mod.py",)) is (
        skip.EMPTY
    )


def test_spans_alone_are_enough_to_index_lines():
    """`lines` is derived, so a `Skipped` cannot carry spans it does not index."""
    rule = config_lib._parse_table({"skip": [{"decorator": "d"}]}, ROOT).skip[0]
    built = skip.Skipped(spans=((4, 6, rule),))
    assert built.covers(5) is rule
    assert built.covers(7) is None


def test_region_spans_skips_the_pin_harvest():
    source = (
        "from pkg.sub.mod import Field\n\n\n"
        "@field_operator\ndef op(a: 'Field') -> None:\n    return None\n"
    )
    tree = cst.parse_module(source)
    positions = metadata.MetadataWrapper(tree, unsafe_skip_copy=True).resolve(
        metadata.PositionProvider
    )
    rules = config_lib._parse_table({"skip": [{"decorator": "field_operator"}]}, ROOT).skip
    spans_only = skip.region_spans(tree, positions, rules, ("pkg/mod.py",))
    full = skip.regions(tree, positions, rules, ("pkg/mod.py",))
    assert spans_only.spans == full.spans
    assert spans_only.covers(5) is not None, "the line index is still built"
    assert spans_only.names == {}
    assert full.pin("Field") is not None

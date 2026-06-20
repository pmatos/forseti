"""Hermetic tests for the C-frontend counterexample parser.

Each test feeds `parse_counterexample` a golden raw-cex fixture captured from
real ESBMC 8.3.0 output (tests/esbmc/fixtures/counterexamples/*.txt, generated
as `verify(...).raw_counterexample`) and asserts the typed model. No esbmc
binary required.
"""

import json
from pathlib import Path

import pytest

from forseti.esbmc.cex_parser import parse_counterexample

CEX = Path(__file__).parent / "fixtures" / "counterexamples"


def load(name: str) -> str:
    return (CEX / f"{name}.txt").read_text()


def test_parses_int_overflow_into_typed_model() -> None:
    cex = parse_counterexample(load("int_overflow"))
    assert cex is not None
    assert len(cex.steps) >= 1
    assert cex.violated_property.description == "abs is non-negative"
    assert cex.violated_property.expression == "r >= 0"


def test_scalar_assignment_splits_value_and_binary() -> None:
    cex = parse_counterexample(load("int_overflow"))
    assert cex is not None
    first = cex.steps[0].assignments[0]
    assert first.lhs == "x"
    assert first.value == "-2147483648"
    assert first.binary == "10000000 00000000 00000000 00000000"


def test_pointer_assignment_keeps_raw_value_and_no_binary() -> None:
    cex = parse_counterexample(load("null_deref"))
    assert cex is not None
    a = cex.steps[0].assignments[0]
    assert a.lhs == "p"
    assert a.value == "(signed int *)0"
    assert a.binary is None


def test_inputs_is_first_write_per_base_symbol() -> None:
    cex = parse_counterexample(load("int_overflow"))
    assert cex is not None
    assert {a.lhs: a.value for a in cex.inputs} == {
        "x": "-2147483648",
        "r": "-2147483648",
    }


def test_inputs_dedup_by_base_strips_ssa_and_subscripts() -> None:
    raw = (
        "[Counterexample]\n\n"
        "State 1 file f.c line 1 column 1 function main thread 0\n"
        "----------------------------------------------------\n"
        "  n = 7 (00000111)\n"
        "State 2 file f.c line 2 column 1 function f thread 0\n"
        "----------------------------------------------------\n"
        "  return_value$_f$1 = 7 (00000111)\n"
        "  return_value$_f$1 = 9 (00001001)\n"
        "  a[3] = 1 (00000001)\n"
        "  a[4] = 2 (00000010)\n"
        "Violated property:\n"
        "  file f.c line 2 column 1 function main\n"
        "  boom\n"
        "  x > 0\n"
    )
    cex = parse_counterexample(raw)
    assert cex is not None
    assert [(a.lhs, a.value) for a in cex.inputs] == [
        ("n", "7"),
        ("return_value$_f$1", "7"),
        ("a[3]", "1"),
    ]


def test_state_numbers_preserved_when_non_contiguous() -> None:
    # ESBMC's slicer drops states; numbers must survive verbatim, not re-indexed.
    cex = parse_counterexample(load("array_oob"))
    assert cex is not None
    assert [s.number for s in cex.steps] == [1, 4]


def test_state_without_function_field_parses() -> None:
    # Library/init states omit `function`; aggregate values carry no bit-string.
    raw = (
        "[Counterexample]\n"
        "State 1 file esbmc_intrinsics.h line 17 column 1 thread 0\n"
        "----------------------------------------------------\n"
        "  alloc = ARRAY_OF(0)\n"
        "State 2 file f.c line 3 column 3 function main thread 0\n"
        "----------------------------------------------------\n"
        "  x = 0 (00000000)\n"
        "Violated property:\n"
        "  file f.c line 3 column 3 function main\n"
        "  boom\n"
        "  x > 0\n"
    )
    cex = parse_counterexample(raw)
    assert cex is not None
    assert cex.steps[0].loc.function is None
    assert cex.steps[1].loc.function == "main"
    assert cex.steps[0].assignments[0].value == "ARRAY_OF(0)"
    assert cex.steps[0].assignments[0].binary is None


def test_array_oob_property_has_cwe_list_and_expression() -> None:
    cex = parse_counterexample(load("array_oob"))
    assert cex is not None
    vp = cex.violated_property
    assert vp.description == "array bounds violated: array `a' upper bound"
    assert vp.expression == "(signed long int)i < 4"
    assert vp.cwe == (
        "CWE-121",
        "CWE-125",
        "CWE-129",
        "CWE-131",
        "CWE-193",
        "CWE-787",
    )


def test_null_deref_property_has_cwe_but_no_expression() -> None:
    cex = parse_counterexample(load("null_deref"))
    assert cex is not None
    vp = cex.violated_property
    assert vp.description == "dereference failure: NULL pointer"
    assert vp.expression is None
    assert vp.cwe == ("CWE-476",)
    assert vp.loc.function == "main"
    assert vp.loc.file.endswith("null_deref.c")


def test_to_dict_round_trips_through_json() -> None:
    cex = parse_counterexample(load("array_oob"))
    assert cex is not None
    data = json.loads(json.dumps(cex.to_dict()))
    assert data["steps"][0]["assignments"][0]["lhs"] == "i"
    assert data["steps"][0]["assignments"][0]["binary"] is not None
    assert data["violated_property"]["cwe"][0] == "CWE-121"
    assert data["violated_property"]["expression"] == "(signed long int)i < 4"


def test_malformed_or_empty_input_returns_none_never_raises() -> None:
    assert parse_counterexample("") is None
    assert parse_counterexample("garbage with no markers at all") is None
    # a state but no `Violated property:` block is not a usable counterexample
    incomplete = (
        "[Counterexample]\n"
        "State 1 file f.c line 1 column 1 function main thread 0\n"
        "----\n"
        "  x = 0 (00000000)\n"
    )
    assert parse_counterexample(incomplete) is None


@pytest.mark.parametrize(
    "name",
    ["int_overflow", "array_oob", "div_by_zero", "null_deref", "assert_simple"],
)
def test_every_fixture_parses_to_a_counterexample(name: str) -> None:
    cex = parse_counterexample(load(name))
    assert cex is not None
    assert cex.steps  # at least one trace step
    assert cex.violated_property.description  # an outcome description is always present


def test_multiple_counterexample_blocks_return_none() -> None:
    # Some flag combinations (e.g. --assertion-coverage) make ESBMC emit several
    # [Counterexample] blocks before one terminal banner. Merging their states
    # under the last block's property would be a synthetic, misleading trace, so
    # the parser declines (None) and the caller keeps raw_counterexample.
    raw = (
        "[Counterexample]\n"
        "State 1 file f.c line 1 column 1 function main thread 0\n"
        "----------------------------------------------------\n"
        "  x = 1 (00000001)\n"
        "Violated property:\n"
        "  file f.c line 1 column 1 function main\n"
        "  first property\n"
        "  x != 1\n"
        "\n"
        "[Counterexample]\n"
        "State 2 file f.c line 2 column 1 function main thread 0\n"
        "----------------------------------------------------\n"
        "  y = 2 (00000010)\n"
        "Violated property:\n"
        "  file f.c line 2 column 1 function main\n"
        "  second property\n"
        "  y != 2\n"
    )
    assert parse_counterexample(raw) is None


def test_div_by_zero_property() -> None:
    cex = parse_counterexample(load("div_by_zero"))
    assert cex is not None
    vp = cex.violated_property
    assert vp.description == "division by zero"
    assert vp.expression == "y != 0"
    assert vp.cwe == ("CWE-369",)

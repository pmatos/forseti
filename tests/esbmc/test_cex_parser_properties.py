"""Property-based tests for the C-frontend counterexample parser.

The golden-fixture tests in `test_cex_parser.py` pin exact outputs for real
ESBMC traces; these check the parser's *invariants* over synthesized input —
the properties fixtures can't enumerate:

* it never raises (the documented contract), whatever the text;
* whatever it returns serializes cleanly through JSON (`to_dict`);
* a well-formed synthesized trace round-trips into the structure that built it.

No esbmc binary required.
"""

from __future__ import annotations

import json

from hypothesis import given
from hypothesis import strategies as st

from forseti.esbmc.cex_parser import parse_counterexample
from forseti.esbmc.counterexample import Counterexample

# --- robustness: `parse_counterexample` never raises ----------------------

# Realistic ESBMC trace lines; shuffling them into arbitrary orders exercises
# the parser's block-boundary handling (a Violated block with no states, states
# with no Violated block, stray CWE lines, ...) far more than free text does.
_ESBMC_LINES = st.sampled_from(
    [
        "[Counterexample]",
        "Violated property:",
        "State 1 file f.c line 1 column 1 function main thread 0",
        "State 42 file esbmc_intrinsics.h line 17 column 1 thread 0",
        "----------------------------------------------------",
        "  x = 0 (00000000)",
        "  p = (signed int *)0",
        "  alloc = ARRAY_OF(0)",
        "  file f.c line 1 column 1 function main",
        "  CWE: CWE-476, CWE-121",
        "  dereference failure: NULL pointer",
        "  x > 0",
        "",
    ]
)


@given(st.text())
def test_never_raises_on_arbitrary_text(raw: str) -> None:
    result = parse_counterexample(raw)
    assert result is None or isinstance(result, Counterexample)


@given(st.lists(_ESBMC_LINES).map("\n".join))
def test_never_raises_on_shuffled_esbmc_lines(raw: str) -> None:
    result = parse_counterexample(raw)
    assert result is None or isinstance(result, Counterexample)


@given(st.integers(min_value=2, max_value=6))
def test_multiple_counterexample_markers_always_declined(n: int) -> None:
    # >1 `[Counterexample]` marker => decline (merging blocks would mislead).
    block = "\n".join(
        [
            "[Counterexample]",
            "State 1 file f.c line 1 column 1 function main thread 0",
            "  x = 1 (00000001)",
            "Violated property:",
            "  file f.c line 1 column 1 function main",
            "  boom",
            "  x != 1",
        ]
    )
    assert parse_counterexample("\n\n".join([block] * n)) is None


# --- structural round-trip over a synthesized well-formed trace -----------

_ident = st.from_regex(r"[A-Za-z_][A-Za-z0-9_]{0,12}", fullmatch=True)
_fname = st.from_regex(r"[A-Za-z_][A-Za-z0-9_]{0,10}\.c", fullmatch=True)
_value = st.integers(min_value=-(2**31), max_value=2**31 - 1)


# A one-line description/expression: non-empty after strip, not a `CWE:` line,
# and not itself parseable as a `State ...` header.
def _usable_phrase(s: str) -> bool:
    return bool(s) and not s.startswith("CWE") and not s.startswith("State ")


_phrase = (
    st.from_regex(r"[A-Za-z][A-Za-z0-9 _'>=<!-]{0,38}[A-Za-z0-9]", fullmatch=True)
    .map(str.strip)
    .filter(_usable_phrase)
)


@st.composite
def _synthesized_trace(draw: st.DrawFn) -> tuple[str, dict[str, object]]:
    """Build a well-formed C counterexample and the spec it should parse back to."""
    states = draw(
        st.lists(
            st.tuples(
                st.integers(min_value=0, max_value=99999),  # state number
                _fname,
                st.integers(min_value=1, max_value=99999),  # line
                st.integers(min_value=1, max_value=99999),  # column
                st.none() | _ident,  # optional function
                st.lists(st.tuples(_ident, _value), max_size=3),  # assignments
            ),
            min_size=1,
            max_size=4,
        )
    )
    description = draw(_phrase)
    expression = draw(st.none() | _phrase)

    lines = ["[Counterexample]"]
    for number, fname, line, col, fn, assigns in states:
        fn_part = f" function {fn}" if fn is not None else ""
        lines.append(
            f"State {number} file {fname} line {line} column {col}{fn_part} thread 0"
        )
        lines.append("-" * 40)
        lines += [f"  {lhs} = {value}" for lhs, value in assigns]
    lines.append("Violated property:")
    lines.append("  file f.c line 7 column 3 function main")
    lines.append(f"  {description}")
    if expression is not None:
        lines.append(f"  {expression}")

    spec: dict[str, object] = {
        "states": states,
        "description": description,
        "expression": expression,
    }
    return "\n".join(lines), spec


@given(_synthesized_trace())
def test_synthesized_trace_round_trips(case: tuple[str, dict[str, object]]) -> None:
    raw, spec = case
    cex = parse_counterexample(raw)
    assert cex is not None

    states = spec["states"]
    assert isinstance(states, list)
    assert [step.number for step in cex.steps] == [s[0] for s in states]
    for step, (_, _, _, _, fn, assigns) in zip(cex.steps, states, strict=True):
        assert step.loc.function == fn
        assert [(a.lhs, a.value, a.binary) for a in step.assignments] == [
            (lhs, str(value), None) for lhs, value in assigns
        ]

    assert cex.violated_property.description == spec["description"]
    assert cex.violated_property.expression == spec["expression"]

    # Whatever parsed serializes cleanly (the observability-trace contract).
    restored = json.loads(json.dumps(cex.to_dict()))
    assert restored["violated_property"]["description"] == spec["description"]


@given(st.lists(_ESBMC_LINES).map("\n".join))
def test_any_parsed_result_serializes_through_json(raw: str) -> None:
    cex = parse_counterexample(raw)
    if cex is None:
        return
    # asdict -> json must not raise and must preserve the discriminating fields.
    restored = json.loads(json.dumps(cex.to_dict()))
    assert "violated_property" in restored
    assert isinstance(restored["steps"], list)

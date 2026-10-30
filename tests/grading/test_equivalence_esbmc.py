"""Equivalence harness end to end against the real ESBMC.

Skipped when `esbmc` is not on PATH. Assertions avoid ESBMC's printed value
spelling (it varies across versions) and pin witnesses by bit string or by the
numeric value of a small integer.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest

from forseti.grading.equivalence import (
    EquivalenceRefused,
    EquivalenceUnknown,
    EquivalenceVerified,
    EquivalenceViolated,
    UnknownReason,
    check_equivalence,
    filter_equivalent,
)

pytestmark = pytest.mark.skipif(
    shutil.which("esbmc") is None, reason="esbmc binary not on PATH"
)

ONE_OFF = "int f(int a) { return a == 7 ? 0 : a * 2; }"
TIMES_TWO = "int f(int a) { return a * 2; }"
PLUS_SELF = "int f(int a) { return a + a; }"
BITS_NEG_ZERO = "1" + "0" * 31


def bits(value: object) -> str:
    return (getattr(value, "bits", None) or "").replace(" ", "")


def test_one_input_difference_is_violated_with_that_input() -> None:
    got = check_equivalence(reference=TIMES_TWO, candidate=ONE_OFF, symbol="f", k=2)
    assert isinstance(got, EquivalenceViolated)
    assert got.k == 2
    assert got.witness.diverging == "return"
    inputs = dict(got.witness.inputs)
    assert int(inputs["a"].value) == 7
    assert int(dict(got.witness.reference_output)["return"].value) == 14
    assert int(dict(got.witness.candidate_output)["return"].value) == 0


def test_semantics_preserving_rewrite_is_verified_up_to_k() -> None:
    got = check_equivalence(reference=TIMES_TWO, candidate=PLUS_SELF, symbol="f", k=2)
    assert isinstance(got, EquivalenceVerified)
    assert got.k == 2
    assert got.length_bounds == ()


def test_negative_zero_differs_from_positive_zero() -> None:
    got = check_equivalence(
        reference="float f(float x) { return x; }",
        candidate="float f(float x) { return x == 0.0f ? 0.0f : x; }",
        symbol="f",
        k=2,
    )
    assert isinstance(got, EquivalenceViolated)
    assert bits(dict(got.witness.inputs)["x"]) == BITS_NEG_ZERO
    assert bits(dict(got.witness.reference_output)["return"]) == BITS_NEG_ZERO
    assert bits(dict(got.witness.candidate_output)["return"]) == "0" * 32


def test_nan_payload_is_not_a_difference() -> None:
    got = check_equivalence(
        reference="float f(float x) { return x; }",
        candidate='float f(float x) { return x != x ? __builtin_nanf("1") : x; }',
        symbol="f",
        k=2,
    )
    assert isinstance(got, EquivalenceVerified)


def test_nan_versus_a_number_is_a_difference() -> None:
    got = check_equivalence(
        reference="float f(float x) { return x; }",
        candidate="float f(float x) { return x != x ? 0.0f : x; }",
        symbol="f",
        k=2,
    )
    assert isinstance(got, EquivalenceViolated)


def test_double_negative_zero_is_a_difference() -> None:
    got = check_equivalence(
        reference="double f(double x) { return x; }",
        candidate="double f(double x) { return x + 0.0; }",
        symbol="f",
        k=2,
    )
    assert isinstance(got, EquivalenceViolated)


SORT_REF = (
    "void s(unsigned char *a, int n) { for (int i = 0; i + 1 < n; i++)"
    " if (a[i] > a[i + 1]) { unsigned char t = a[i]; a[i] = a[i + 1]; a[i + 1] = t; } }"
)
SORT_EQ = SORT_REF.replace("a[i] > a[i + 1]", "a[i] >= a[i + 1]")
SORT_BAD = SORT_REF.replace("a[i] > a[i + 1]", "a[i] < a[i + 1]")


def test_in_place_buffer_rewrite_is_verified_and_reports_length_bound() -> None:
    got = check_equivalence(
        reference=SORT_REF, candidate=SORT_EQ, symbol="s", k=5, max_len=3
    )
    assert isinstance(got, EquivalenceVerified)
    assert got.length_bounds == (("n", 3),)


def test_in_place_buffer_difference_is_found_in_the_buffer() -> None:
    got = check_equivalence(
        reference=SORT_REF, candidate=SORT_BAD, symbol="s", k=5, max_len=3
    )
    assert isinstance(got, EquivalenceViolated)
    assert got.witness.diverging.startswith("a[")
    assert got.assumptions == ()
    assert any(label.startswith("a[") for label, _ in got.witness.inputs)


def test_divergence_beyond_the_length_bound_is_not_seen() -> None:
    beyond = "int g(const unsigned char *a, unsigned n) { return n == 5 ? 1 : (int)n; }"
    base = "int g(const unsigned char *a, unsigned n) { return (int)n; }"
    got = check_equivalence(
        reference=base, candidate=beyond, symbol="g", k=3, max_len=3
    )
    assert isinstance(got, EquivalenceVerified)
    assert got.length_bounds == (("n", 3),)
    wider = check_equivalence(
        reference=base, candidate=beyond, symbol="g", k=3, max_len=5
    )
    assert isinstance(wider, EquivalenceViolated)


def test_under_unwound_loop_is_unknown_not_verified() -> None:
    loop = "int f(int n) { int s = 0; for (int i = 0; i < n; i++) s++; return s; }"
    got = check_equivalence(reference=loop, candidate=loop, symbol="f", k=2)
    assert isinstance(got, EquivalenceUnknown)
    assert got.reason is UnknownReason.UNDER_UNWOUND


def test_scalar_backed_output_is_compared() -> None:
    ref = (
        "int d(const unsigned char *b, unsigned len, unsigned *cp)"
        " { *cp = len; return 0; }"
    )
    cand = ref.replace("*cp = len", "*cp = len + 1")
    got = check_equivalence(reference=ref, candidate=cand, symbol="d", k=3, max_len=2)
    assert isinstance(got, EquivalenceViolated)
    assert got.witness.diverging == "cp"


def test_shared_static_helper_collision_is_renamed() -> None:
    ref = "static int h(int v) { return v + 1; }\nint f(int x) { return h(x) * 2; }"
    same = "static int h(int v) { return v + 1; }\nint f(int x) { return x * 2 + 2; }"
    other = "static int h(int v) { return v + 2; }\nint f(int x) { return h(x) * 2; }"
    assert isinstance(
        check_equivalence(reference=ref, candidate=same, symbol="f", k=2),
        EquivalenceVerified,
    )
    assert isinstance(
        check_equivalence(reference=ref, candidate=other, symbol="f", k=2),
        EquivalenceViolated,
    )


def test_recursive_reference_compares_against_its_own_recursion() -> None:
    ref = "int f(int x) { return x <= 0 ? 0 : 1 + f(x - 1); }"
    cand = "int f(int x) { return x <= 0 ? 0 : 2 + f(x - 1); }"
    got = check_equivalence(reference=ref, candidate=cand, symbol="f", k=4)
    assert isinstance(got, EquivalenceViolated)


def test_candidate_that_does_not_compile_is_unknown() -> None:
    got = check_equivalence(
        reference=TIMES_TWO, candidate="int f(int a) { return a +; }", symbol="f", k=2
    )
    assert isinstance(got, EquivalenceUnknown)
    assert got.reason is UnknownReason.ERROR


def test_reference_fault_is_a_refusal() -> None:
    ref = "int g(const int *a, int n) { return a[n]; }"
    cand = "int g(const int *a, int n) { return n; }"
    got = check_equivalence(reference=ref, candidate=cand, symbol="g", k=2)
    assert isinstance(got, EquivalenceRefused)


@dataclass(frozen=True)
class Mutant:
    id: str
    source: str


def test_filter_reports_skipped_equivalent_mutants_separately() -> None:
    mutants = [
        Mutant("rewrite", PLUS_SELF),
        Mutant("one-off", ONE_OFF),
        Mutant("broken", "int f(int a) { return a +; }"),
        Mutant("stateful", "int g;\nint f(int a) { return a * 2; }"),
    ]
    got = filter_equivalent(TIMES_TWO, mutants, 2, symbol="f")
    assert [m.id for m in got.skipped_equivalent] == ["rewrite"]
    assert got.skipped == 1
    assert [m.id for m in got.kept] == ["one-off", "broken", "stateful"]
    assert got.undecided == ("broken", "stateful")
    assert got.to_dict()["skipped"] == 1


def test_write_through_a_const_buffer_is_a_divergence() -> None:
    writes = (
        "int f(const int *a, int n) {"
        " int *p = (int *)a; if (n > 0) p[0] = 7; return 0; }"
    )
    quiet = "int f(const int *a, int n) { return 0; }"
    got = check_equivalence(
        reference=writes, candidate=quiet, symbol="f", k=2, max_len=2
    )
    assert isinstance(got, EquivalenceViolated)


def test_faulting_original_is_refused_once_by_the_filter() -> None:
    original = "int f(int a, int b) { return a / b; }"
    got = filter_equivalent(
        original, [Mutant("m1", "int f(int a, int b) { return a; }")], 2, symbol="f"
    )
    assert got.skipped == 0
    assert got.original_refusal is not None


def _example_kernel(name: str) -> str:
    text = (Path(__file__).resolve().parents[2] / "examples" / name).read_text()
    return text[: text.index("int main(void)")]


def test_merge_sort_against_itself_is_verified_up_to_k() -> None:
    src = _example_kernel("merge_sort.c")
    got = check_equivalence(
        reference=src, candidate=src, symbol="msort", k=4, max_len=2, timeout_s=120
    )
    assert isinstance(got, EquivalenceVerified)
    assert got.length_bounds == (("hi", 2),)
    assert got.assumptions == ("0 <= lo <= hi",)


def test_merge_sort_without_its_tail_drain_is_violated() -> None:
    got = check_equivalence(
        reference=_example_kernel("merge_sort.c"),
        candidate=_example_kernel("merge_sort_bug.c"),
        symbol="msort",
        k=4,
        max_len=2,
        timeout_s=120,
    )
    assert isinstance(got, EquivalenceViolated)
    assert got.witness.diverging.startswith("a[")
    assert got.assumptions == ("0 <= lo <= hi",)

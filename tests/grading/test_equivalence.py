"""Equivalence harness: renderer text, refusals, classification and the filter.

No ESBMC needed: the renderer is pure and the verdict path runs against a fake
`VerifyPort`. The ESBMC-gated checks live in `test_equivalence_esbmc.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from forseti.esbmc import (
    Assignment,
    Counterexample,
    Error,
    EsbmcResult,
    RunMeta,
    SourceLoc,
    Step,
    Unknown,
    UnknownReason,
    Verified,
    Violated,
    ViolatedProperty,
)
from forseti.grading.equivalence import (
    EquivalenceChecker,
    EquivalenceFilter,
    EquivalenceRefusal,
    EquivalenceRefused,
    EquivalenceResult,
    EquivalenceUnknown,
    EquivalenceVerified,
    EquivalenceViolated,
    Witness,
    check_equivalence,
    filter_equivalent,
    render_equivalence_harness,
)
from forseti.grading.equivalence import UnknownReason as EqUnknownReason

REPO = Path(__file__).resolve().parents[2]
META = RunMeta("8.3.0", ("esbmc",), 0, 0.0, "", "")

DOUBLE = "int f(int x) { return x * 2; }"
DOUBLE_ADD = "int f(int x) { return x + x; }"
SUM = (
    "int sum(const unsigned char *a, unsigned n) {"
    " int s = 0; for (unsigned i = 0; i < n; i++) s += a[i]; return s; }"
)
FILL = "void fill(unsigned char *a, int n, unsigned char v) { }"
UTF = (
    "int dec(const unsigned char *b, unsigned len, unsigned *cp) { *cp = 1; return 0; }"
)


def render(ref: str, cand: str, symbol: str = "f", max_len: int = 4) -> str:
    return render_equivalence_harness(
        reference_source=ref,
        candidate_source=cand,
        symbol=symbol,
        max_len=max_len,
    ).source_text


def refusal(ref: str, cand: str | None = None, symbol: str = "f") -> str:
    with pytest.raises(EquivalenceRefusal) as info:
        render(ref, ref if cand is None else cand, symbol)
    return str(info.value)


def example_slice(name: str) -> str:
    text = (REPO / "examples" / name).read_text()
    cut = text.find("int main(void)")
    return text if cut < 0 else text[:cut]


def test_scalar_harness_shape() -> None:
    text = render(DOUBLE, DOUBLE_ADD)
    assert "int f__ref(int x)" in text
    assert "int f(int x)" in text
    assert "int x = nondet_int();" in text
    assert "int __forseti_ret_ref = f__ref(x);" in text
    assert "int __forseti_ret = f(x);" in text
    assert '"forseti:equivalence:return"' in text
    assert "__forseti_same_float" not in text
    assert "malloc" not in text


def test_reference_precedes_candidate() -> None:
    text = render(DOUBLE, DOUBLE_ADD)
    assert text.index("x * 2") < text.index("x + x")


def test_void_return_asserts_no_return_value() -> None:
    text = render(FILL, FILL, "fill")
    assert "forseti:equivalence:return" not in text
    assert "fill__ref(__forseti_ref_a, n, v);" in text


def test_buffer_has_private_copies_and_unrolled_fill() -> None:
    text = render(SUM, SUM, "sum", max_len=2)
    assert "__ESBMC_assume((n >= 0 && n <= 2));" in text
    assert "unsigned char *a = malloc" in text
    assert "unsigned char *__forseti_ref_a = malloc" in text
    assert "__forseti_in_a_0" in text
    assert "__forseti_in_a_1" in text
    assert "__forseti_in_a_2" not in text
    assert "sum__ref(__forseti_ref_a, n)" in text
    assert text.count("free(") == 2


def test_const_buffer_is_not_compared_but_mutable_one_is() -> None:
    assert "forseti:equivalence:a[" not in render(SUM, SUM, "sum")
    assert "forseti:equivalence:a[1]" in render(FILL, FILL, "fill")


def test_scalar_backed_output_is_passed_by_address_and_compared() -> None:
    text = render(UTF, UTF, "dec")
    assert "unsigned __forseti_in_cp = nondet_unsigned();" in text
    assert "dec__ref(__forseti_ref_b, len, &__forseti_ref_cp)" in text
    assert '"forseti:equivalence:cp"' in text


def test_float_return_uses_bitwise_relation_helper() -> None:
    text = render("float f(float x) { return x; }", "float f(float x) { return x; }")
    assert "static int __forseti_same_float(float p, float q)" in text
    assert "(p != p && q != q) || __forseti_x.u == __forseti_y.u" in text
    assert "__forseti_same_double" not in text


def test_double_uses_64_bit_union() -> None:
    src = "double f(double x) { return x; }"
    assert "uint64_t u;" in render(src, src)


def test_helpers_are_renamed_in_the_reference_only() -> None:
    ref = "static int h(int v) { return v + 1; } int f(int x) { return h(x); }"
    cand = "static int h(int v) { return v + 2; } int f(int x) { return h(x); }"
    text = render(ref, cand)
    assert "static int h__ref(int v)" in text
    assert "return h__ref(x);" in text
    assert "static int h(int v)" in text
    assert text.count("return h(x);") == 1


def test_recursion_calls_the_renamed_reference() -> None:
    src = "int f(int x) { return x <= 0 ? 0 : f(x - 1); }"
    text = render(src, src)
    assert "x <= 0 ? 0 : f__ref(x - 1)" in text


def test_literal_length_buffer_unrolls_to_that_length() -> None:
    src = "void g(unsigned char a[3]) { a[0] = 1; }"
    text = render(src, src, "g")
    assert "__forseti_in_a_2" in text
    assert "__forseti_in_a_3" not in text


def test_length_bound_is_reported() -> None:
    harness = render_equivalence_harness(
        reference_source=SUM, candidate_source=SUM, symbol="sum", max_len=3
    )
    assert harness.length_bounds == (("n", 3),)


def test_negative_max_len_is_refused() -> None:
    with pytest.raises(EquivalenceRefusal, match="max_len"):
        render_equivalence_harness(
            reference_source=DOUBLE,
            candidate_source=DOUBLE,
            symbol="f",
            max_len=-1,
        )


def test_struct_pointer_argument_is_refused() -> None:
    src = "typedef struct ring ring_t;\nint rb_push(ring_t *r, int v) { return v; }\n"
    assert "non-arithmetic type `ring_t`" in refusal(src, symbol="rb_push")


def test_ring_buffer_example_is_refused() -> None:
    reason = refusal(example_slice("ring_buffer.c"), symbol="rb_push")
    assert "file-scope" in reason


def test_merge_sort_example_is_refused_for_its_mistyped_output() -> None:
    reason = refusal(example_slice("merge_sort.c"), symbol="msort")
    assert "`tmp`" in reason
    assert "dereference" in reason


@pytest.mark.parametrize(
    ("source", "fragment"),
    [
        ("int g;\nint f(int x) { return x + g; }", "file-scope object"),
        ("const int T[2] = {1, 2};\nint f(int x) { return T[0]; }", "file-scope"),
        ("struct s { int a; };\nint f(int x) { return x; }", "file-scope"),
        ("extern int g;\nint f(int x) { return g; }", "file-scope object"),
        ("int f(int x) { static int n; return x + n++; }", "static or extern"),
        ("int main(void) { return 0; }\nint f(int x) { return x; }", "main()"),
        ("int main(void);\nint f(int x) { return x; }", "main()"),
        ("int h(int x) { return x; }", "does not define `f`"),
        ("struct s f(int x);\nstruct s f(int x) { return g(x); }", "return type"),
        ("int *f(int x) { return 0; }", "return type"),
        ("long double f(long double x) { return x; }", "plain arithmetic"),
        ("int f(int x, ...) { return x; }", "not modelled"),
        ("int f(char **argv, int n) { return n; }", "multi-level"),
        ("int f(void (*cb)(int), int x) { return x; }", "not modelled"),
        ("int f(struct s v) { return 0; }", "plain arithmetic"),
        ("int f(int __forseti_x) { return __forseti_x; }", "reserved prefix"),
        ("int f(int a[2 * 3]) { return a[0]; }", "not an integer parameter"),
        ("int f(int a[x], float x) { return a[0]; }", "not an integer parameter"),
        ("int f(int a[1000]) { return a[0]; }", "literal length 1000"),
        ("int f(int x) { return x; } int f__ref(int x);", "`__ref` name"),
    ],
)
def test_unmodelled_sources_are_refused_with_a_reason(
    source: str, fragment: str
) -> None:
    assert fragment in refusal(source)


def test_candidate_state_is_refused_too() -> None:
    reason = refusal(DOUBLE, "int g;\nint f(int x) { return x; }")
    assert reason.startswith("candidate source")


def test_function_pointer_use_of_a_reference_helper_is_refused() -> None:
    ref = "int h(int v) { return v; }\nint f(int x) { int (*p)(int) = h; return p(x); }"
    assert "other than by a direct call" in refusal(ref, DOUBLE)


def test_mismatched_signatures_are_refused() -> None:
    assert "signatures differ" in refusal(DOUBLE, "long f(int x) { return x; }")
    assert "signatures differ" in refusal(DOUBLE, "int f(int x, int y) { return x; }")


def test_renamed_parameters_with_the_same_shape_are_accepted() -> None:
    render(DOUBLE, "int f(int y) { return y * 2; }")


def test_comments_and_strings_do_not_trigger_refusals() -> None:
    src = (
        '/* int g; */\n#define N 4\n// main(\nint f(int x) { puts("main("); return x; }'
    )
    assert "int f(int x)" in render(src, src)


def counterexample(description: str, function: str | None) -> Counterexample:
    loc = SourceLoc("h.c", 1, 1, function)
    steps = (
        Step(
            1,
            loc,
            (
                Assignment("x", "7", "111"),
                Assignment("__forseti_ret_ref", "14", None),
                Assignment("__forseti_ret", "15", None),
            ),
        ),
    )
    return Counterexample(steps, ViolatedProperty(loc, description, None, ()))


class RecordingPort:
    def __init__(self, result: EsbmcResult) -> None:
        self.result = result
        self.calls: list[tuple[Path, int]] = []

    def __call__(self, source: Path, /, *, unwind: int) -> EsbmcResult:
        self.calls.append((source, unwind))
        assert source.read_text().startswith("#include")
        return self.result


def run(result: EsbmcResult) -> EquivalenceResult:
    return check_equivalence(
        reference=DOUBLE,
        candidate=DOUBLE_ADD,
        symbol="f",
        k=3,
        verify_port=RecordingPort(result),
    )


def test_verified_carries_both_bounds() -> None:
    got = check_equivalence(
        reference=SUM,
        candidate=SUM,
        symbol="sum",
        k=5,
        max_len=3,
        verify_port=RecordingPort(Verified(META)),
    )
    assert isinstance(got, EquivalenceVerified)
    assert (got.k, got.length_bounds) == (5, (("n", 3),))


def test_violated_divergence_becomes_a_witness() -> None:
    cex = counterexample("forseti:equivalence:return", "main")
    got = run(Violated(META, "raw", cex))
    assert isinstance(got, EquivalenceViolated)
    assert got.witness.diverging == "return"
    assert got.witness.inputs[0][0] == "x"
    assert got.witness.inputs[0][1].value == "7"
    assert got.witness.inputs[0][1].bits == "111"
    assert got.witness.reference_output[0][1].value == "14"
    assert got.witness.candidate_output[0][1].value == "15"


def test_unparsed_divergence_is_still_violated() -> None:
    got = run(Violated(META, "... forseti:equivalence:return ...", None))
    assert isinstance(got, EquivalenceViolated)
    assert got.witness.inputs == ()


def test_fault_in_candidate_is_unknown_not_violated() -> None:
    cex = counterexample("dereference failure: NULL pointer", "f")
    got = run(Violated(META, "raw", cex))
    assert isinstance(got, EquivalenceUnknown)
    assert got.reason is EqUnknownReason.FAULT
    assert "in f" in got.detail


def test_fault_in_reference_is_a_refusal() -> None:
    cex = counterexample("array bounds violated", "f__ref")
    got = run(Violated(META, "raw", cex))
    assert isinstance(got, EquivalenceRefused)
    assert "reference itself" in got.reason


def test_unparsed_fault_is_unknown() -> None:
    got = run(Violated(META, "some other failure", None))
    assert isinstance(got, EquivalenceUnknown)
    assert got.reason is EqUnknownReason.FAULT


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        (UnknownReason.TIMEOUT, EqUnknownReason.TIMEOUT),
        (UnknownReason.MEMOUT, EqUnknownReason.MEMOUT),
        (UnknownReason.UNCLASSIFIED, EqUnknownReason.UNCLASSIFIED),
        (UnknownReason.UNDER_UNWOUND, EqUnknownReason.UNDER_UNWOUND),
    ],
)
def test_unknown_is_carried_never_verified(
    reason: UnknownReason, expected: EqUnknownReason
) -> None:
    got = run(Unknown(META, reason))
    assert isinstance(got, EquivalenceUnknown)
    assert got.reason is expected
    assert got.k == 3


def test_esbmc_error_is_unknown() -> None:
    got = run(Error(META, "PARSING ERROR"))
    assert isinstance(got, EquivalenceUnknown)
    assert got.reason is EqUnknownReason.ERROR
    assert got.detail == "PARSING ERROR"


def test_refusal_short_circuits_before_esbmc() -> None:
    port = RecordingPort(Verified(META))
    got = check_equivalence(
        reference=DOUBLE,
        candidate="int g;\nint f(int x) { return x; }",
        symbol="f",
        k=1,
        verify_port=port,
    )
    assert isinstance(got, EquivalenceRefused)
    assert port.calls == []


def test_unparseable_signature_is_refused_not_raised() -> None:
    got = check_equivalence(
        reference="int f(int x, ...) { return x; }",
        candidate="int f(int x, ...) { return x; }",
        symbol="f",
        k=1,
        verify_port=RecordingPort(Verified(META)),
    )
    assert isinstance(got, EquivalenceRefused)


def test_harness_is_written_under_work_dir(tmp_path: Path) -> None:
    port = RecordingPort(Verified(META))
    check_equivalence(
        reference=DOUBLE,
        candidate=DOUBLE_ADD,
        symbol="f",
        k=2,
        work_dir=tmp_path / "w",
        verify_port=port,
    )
    written = port.calls[0][0]
    assert written.parent == tmp_path / "w"
    assert written.exists()


@dataclass(frozen=True)
class FakeMutant:
    id: str
    source: str


def scripted(table: dict[str, EquivalenceResult]) -> EquivalenceChecker:
    def checker(**kwargs: object) -> EquivalenceResult:
        assert kwargs["reference"] == DOUBLE
        assert kwargs["k"] == 4
        return table[str(kwargs["candidate"])]

    return checker


def test_filter_skips_only_verified_mutants() -> None:
    verified = EquivalenceVerified(4, (), META)
    unknown = EquivalenceUnknown(4, (), EqUnknownReason.TIMEOUT, "timeout", META)
    refused = EquivalenceRefused("nope")
    violated_src = "int f(int x) { return x; }"
    table: dict[str, EquivalenceResult] = {
        "eq": verified,
        "unk": unknown,
        "ref": refused,
        violated_src: EquivalenceVerified(4, (), META),
    }
    mutants = [
        FakeMutant("m1", "eq"),
        FakeMutant("m2", "unk"),
        FakeMutant("m3", "ref"),
    ]
    got = filter_equivalent(
        DOUBLE,
        mutants,
        4,
        symbol="f",
        checker=scripted(table),
    )
    assert isinstance(got, EquivalenceFilter)
    assert [m.id for m in got.skipped_equivalent] == ["m1"]
    assert got.skipped == 1
    assert [m.id for m in got.kept] == ["m2", "m3"]
    assert got.undecided == ("m2", "m3")
    assert [i for i, _ in got.results] == ["m1", "m2", "m3"]
    assert got.original_refusal is None
    report = got.to_dict()
    assert report["skipped"] == 1
    assert report["skipped_equivalent"] == ["m1"]
    assert report["undecided"] == ["m2", "m3"]
    assert report["verdicts"] == {
        "m1": "verified",
        "m2": "unknown",
        "m3": "refused",
    }
    assert (report["k"], report["max_len"]) == (4, 8)


def test_filter_keeps_divergent_mutants() -> None:
    violated = EquivalenceViolated(4, (), counterexample_witness(), "raw", META)
    got = filter_equivalent(
        DOUBLE,
        [FakeMutant("m", "x")],
        4,
        symbol="f",
        checker=scripted({"x": violated}),
    )
    assert got.skipped == 0
    assert [m.id for m in got.kept] == ["m"]
    assert got.undecided == ()


def counterexample_witness() -> Witness:
    return Witness("return", (), (), ())


def test_filter_with_unmodelled_original_skips_nothing() -> None:
    original = "int g;\nint f(int x) { return x; }"

    def never(**_: object) -> EquivalenceResult:
        raise AssertionError("checker must not run")

    got = filter_equivalent(
        original,
        [FakeMutant("m", original)],
        2,
        symbol="f",
        checker=never,
    )
    assert got.skipped == 0
    assert got.original_refusal is not None
    assert "file-scope" in got.original_refusal
    assert got.undecided == ("m",)


def test_filter_forwards_check_kwargs() -> None:
    seen: dict[str, object] = {}

    def checker(**kwargs: object) -> EquivalenceResult:
        seen.update(kwargs)
        return EquivalenceRefused("x")

    filter_equivalent(
        DOUBLE,
        [FakeMutant("m", DOUBLE_ADD)],
        2,
        symbol="f",
        max_len=5,
        checker=checker,
        timeout_s=9.0,
    )
    assert seen["timeout_s"] == 9.0
    assert seen["max_len"] == 5
    assert seen["symbol"] == "f"

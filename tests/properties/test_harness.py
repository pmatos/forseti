"""Tests for the harness writer: pure text synthesis + signature parsing.

No esbmc here -- these assert the *shape* of the emitted C and the fail-loud
guards. The end-to-end "does esbmc verify/violate it" checks live in
test_harness_integration.py (skipped without the binary).
"""

from __future__ import annotations

import pytest

from forseti.properties import (
    BufferParam,
    HarnessError,
    Property,
    PropertyKind,
    PropertyStatus,
    Provenance,
    ScalarParam,
    SemanticSpec,
    UnitSignature,
    extract_signature,
    make_property_id,
    render_semantic_harness,
    spec_from_property,
)

ABS_SLICE = "int64_t my_abs(int64_t x) { return (x < 0) ? -x : x; }"
ABS_SIG = UnitSignature("my_abs", "int64_t", (ScalarParam("int64_t", "x"),))


def test_scalar_harness_shape() -> None:
    out = render_semantic_harness(
        unit_source=ABS_SLICE,
        signature=ABS_SIG,
        spec=SemanticSpec("result >= 0", ("x > INT64_MIN",)),
    )
    assert "#include <stdint.h>" in out
    assert ABS_SLICE in out  # the unit is inlined verbatim
    assert "int64_t nondet_int64_t(void);" in out
    assert out.count("__ESBMC_assume(") == 1
    assert "__ESBMC_assume((x > INT64_MIN));" in out
    assert "int64_t x = nondet_int64_t();" in out
    assert "int64_t result = my_abs(x);" in out
    assert '__ESBMC_assert((result >= 0), "forseti:semantic");' in out
    assert out.rstrip().endswith("}")


def test_nondet_prototype_deduped() -> None:
    out = render_semantic_harness(
        unit_source="int add(int a, int b) { return a + b; }",
        signature=UnitSignature(
            "add", "int", (ScalarParam("int", "a"), ScalarParam("int", "b"))
        ),
        spec=SemanticSpec("result == a + b"),
    )
    assert out.count("int nondet_int(void);") == 1  # one prototype for the type
    assert out.count("= nondet_int();") == 2  # but one call site per param


def test_void_return_omits_result_binding() -> None:
    out = render_semantic_harness(
        unit_source="void consume(int x) { (void)x; }",
        signature=UnitSignature("consume", "void", (ScalarParam("int", "x"),)),
        spec=SemanticSpec("x == x"),
    )
    assert "    consume(x);" in out  # bare call, no binding
    assert "= consume(" not in out
    assert "result" not in out


def test_buffer_param_vla_fill() -> None:
    out = render_semantic_harness(
        unit_source="int sum(const int *a, unsigned n) { return 0; }",
        signature=UnitSignature(
            "sum",
            "int",
            (
                BufferParam("int", "a", "n", const=True),
                ScalarParam("unsigned", "n"),
            ),
        ),
        spec=SemanticSpec("result >= result", ("n <= 4",)),
    )
    assert "int a[n];" in out
    assert "for (size_t _i = 0; _i < (n); _i++) a[_i] = nondet_int();" in out
    # the length scalar and its assumption precede the VLA that depends on them
    assert out.index("unsigned n = nondet_unsigned();") < out.index("int a[n];")
    assert out.index("__ESBMC_assume((n <= 4));") < out.index("int a[n];")
    assert "sum(a, n)" in out


def test_buffer_content_precondition_ordered_around_buffer() -> None:
    out = render_semantic_harness(
        unit_source="int first(const int *a, unsigned n) { return a[0]; }",
        signature=UnitSignature(
            "first",
            "int",
            (BufferParam("int", "a", "n", const=True), ScalarParam("unsigned", "n")),
        ),
        spec=SemanticSpec("result == a[0]", ("n <= 4", "a[0] >= 0")),
    )
    # a length-only assume precedes the VLA it sizes; a buffer-content assume
    # follows the declaration/fill so it references an in-scope identifier.
    fill = "a[_i] = nondet_int();"
    content_assume = "__ESBMC_assume((a[0] >= 0));"
    assert out.index("__ESBMC_assume((n <= 4));") < out.index("int a[n];")
    assert out.index(fill) < out.index(content_assume)
    assert out.index(content_assume) < out.index("first(a, n)")


def test_output_buffer_captured() -> None:
    out = render_semantic_harness(
        unit_source="int decode(const unsigned char *b, unsigned len, uint32_t *cp)"
        " { *cp = 0; return 1; }",
        signature=UnitSignature(
            "decode",
            "int",
            (
                BufferParam("unsigned char", "b", "len", const=True),
                ScalarParam("unsigned", "len"),
                BufferParam("uint32_t", "cp", "1", out=True),
            ),
        ),
        spec=SemanticSpec("result <= 0 || cp <= 0x10FFFF", ("len >= 1 && len <= 4",)),
    )
    assert "uint32_t cp;" in out  # scalar-backed output, not an array
    assert "int result = decode(b, len, &cp);" in out  # passed by address
    assert "cp[_i]" not in out  # output is not nondet-filled
    assert "nondet_uint32_t" not in out  # ... so it needs no generator


def test_extract_signature_scalar() -> None:
    assert extract_signature(ABS_SLICE, "my_abs") == ABS_SIG


def test_extract_signature_buffer_and_output() -> None:
    sig = extract_signature(
        "int utf8_decode(const unsigned char *b, unsigned len, uint32_t *cp)"
        " { return 0; }",
        "utf8_decode",
    )
    assert sig == UnitSignature(
        "utf8_decode",
        "int",
        (
            BufferParam("unsigned char", "b", "len", const=True, out=False),
            ScalarParam("unsigned", "len"),
            BufferParam("uint32_t", "cp", "1", const=False, out=True),
        ),
    )


def test_extract_signature_strips_storage_class() -> None:
    sig = extract_signature(
        "static uint32_t murmur3_32(const uint8_t *key, size_t len, uint32_t seed)"
        " { return 0; }",
        "murmur3_32",
    )
    assert sig == UnitSignature(
        "murmur3_32",
        "uint32_t",  # "static" dropped
        (
            BufferParam("uint8_t", "key", "len", const=True),
            ScalarParam("size_t", "len"),
            ScalarParam("uint32_t", "seed"),
        ),
    )


def test_extract_signature_missing_symbol_is_error() -> None:
    with pytest.raises(HarnessError):
        extract_signature(ABS_SLICE, "not_there")


def test_spec_from_semantic_property() -> None:
    prop = _semantic_property("result >= 0", ("x > INT64_MIN",))
    assert spec_from_property(prop) == SemanticSpec("result >= 0", ("x > INT64_MIN",))


def test_spec_from_reachability_property_is_error() -> None:
    prop = _semantic_property("some_label", (), kind=PropertyKind.REACHABILITY)
    with pytest.raises(HarnessError):
        spec_from_property(prop)


def test_empty_postcondition_is_error() -> None:
    with pytest.raises(HarnessError):
        render_semantic_harness(
            unit_source=ABS_SLICE, signature=ABS_SIG, spec=SemanticSpec("   ")
        )


def test_result_var_clashing_param_is_error() -> None:
    with pytest.raises(HarnessError):
        render_semantic_harness(
            unit_source=ABS_SLICE,
            signature=ABS_SIG,
            spec=SemanticSpec("x >= 0", result_var="x"),
        )


def test_void_return_referencing_result_is_error() -> None:
    with pytest.raises(HarnessError):
        render_semantic_harness(
            unit_source="void consume(int x) { (void)x; }",
            signature=UnitSignature("consume", "void", (ScalarParam("int", "x"),)),
            spec=SemanticSpec("result >= 0"),
        )


def test_unit_source_with_main_is_error() -> None:
    with pytest.raises(HarnessError):
        render_semantic_harness(
            unit_source=ABS_SLICE + "\nint main(void) { return 0; }",
            signature=ABS_SIG,
            spec=SemanticSpec("result >= 0"),
        )


class _FakeParam:
    """Not a Scalar/BufferParam, but shaped enough to reach the subtype guard."""

    name = "z"


def test_unknown_param_subtype_is_error() -> None:
    bogus = UnitSignature("f", "int", (_FakeParam(),))  # type: ignore[arg-type]
    with pytest.raises(HarnessError):
        render_semantic_harness(
            unit_source="int f(void) { return 0; }",
            signature=bogus,
            spec=SemanticSpec("result >= 0"),
        )


def _semantic_property(
    expression: str,
    domain: tuple[str, ...],
    *,
    kind: PropertyKind = PropertyKind.SEMANTIC,
) -> Property:
    unit_id = "examples/abs.c::my_abs"
    return Property(
        property_id=make_property_id(unit_id, kind, expression, domain),
        unit_id=unit_id,
        kind=kind,
        expression=expression,
        status=PropertyStatus.CANDIDATE,
        provenance=Provenance("proposer-v1", "1"),
        domain=domain,
    )

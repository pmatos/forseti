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
    build_property_harness,
    extract_signature,
    make_property_id,
    plan_length_bounds,
    render_property_harness,
    render_semantic_harness,
    renderability_reason,
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
    # limits.h ships by default so <limits.h> macros (INT_MIN, ...) that the
    # proposer allows are actually declared in the harness (#81).
    assert "#include <limits.h>" in out
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


_ALLOC_A_N = "int *a = malloc((n) * sizeof(int));"


def test_buffer_param_malloc_fill() -> None:
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
    # malloc, not a stack VLA: a VLA's size must be > 0 under ESBMC's model, so
    # an unconstrained nondet `n` (no domain excludes 0) made every property
    # over this buffer spuriously VIOLATE at n == 0 regardless of what it
    # actually asserted (#297); malloc(0) is well-defined C.
    assert "#include <stdlib.h>" in out
    assert _ALLOC_A_N in out
    assert "__ESBMC_assume(a != NULL);" in out
    assert "for (size_t _i = 0; _i < (n); _i++) a[_i] = nondet_int();" in out
    # the length scalar and its assumption precede the allocation that depends
    # on them
    assert out.index("unsigned n = nondet_unsigned();") < out.index(_ALLOC_A_N)
    assert out.index("__ESBMC_assume((n <= 4));") < out.index(_ALLOC_A_N)
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
    # a length-only assume precedes the allocation it sizes; a buffer-content
    # assume follows the declaration/fill so it references an in-scope
    # identifier.
    fill = "a[_i] = nondet_int();"
    content_assume = "__ESBMC_assume((a[0] >= 0));"
    assert out.index("__ESBMC_assume((n <= 4));") < out.index(_ALLOC_A_N)
    assert out.index(fill) < out.index(content_assume)
    assert out.index(content_assume) < out.index("first(a, n)")


def test_mixed_length_and_buffer_precondition_is_error() -> None:
    # A single clause constraining both the length `n` and the buffer `a` cannot
    # be ordered correctly (the `n` bound must precede the malloc that sizes
    # `a`, the `a` predicate must follow it), so it is rejected -- the caller
    # splits it into separate domain entries (see the ordering test above).
    with pytest.raises(HarnessError):
        render_semantic_harness(
            unit_source="int first(const int *a, unsigned n) { return a[0]; }",
            signature=UnitSignature(
                "first",
                "int",
                (
                    BufferParam("int", "a", "n", const=True),
                    ScalarParam("unsigned", "n"),
                ),
            ),
            spec=SemanticSpec("result == a[0]", ("n >= 1 && n <= 2 && a[0] >= 0",)),
        )


def test_last_element_precondition_renders_after_the_buffer() -> None:
    # `a[n - 1]` names the length only as an *index*, so it is a buffer-content
    # predicate like `a[0] >= 0`: emitted after the allocation + fill, where `n`
    # is an already-bound scalar. The genuine size bound is its own domain
    # entry.
    out = render_semantic_harness(
        unit_source="int last(const int *a, unsigned n) { return a[n - 1]; }",
        signature=UnitSignature(
            "last",
            "int",
            (BufferParam("int", "a", "n", const=True), ScalarParam("unsigned", "n")),
        ),
        spec=SemanticSpec("result == a[n - 1]", ("n >= 1 && n <= 4", "a[n - 1] >= 0")),
    )
    content_assume = "__ESBMC_assume((a[n - 1] >= 0));"
    assert content_assume in out
    assert out.index("__ESBMC_assume((n >= 1 && n <= 4));") < out.index(_ALLOC_A_N)
    assert out.index("a[_i] = nondet_int();") < out.index(content_assume)
    assert out.index(content_assume) < out.index("last(a, n)")


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


def _out_sig() -> UnitSignature:
    # A trailing single-element output pointer, like utf8_decode's `uint32_t *cp`:
    # rendered as a scalar local, passed by address at the call.
    return UnitSignature(
        "decode", "int", (BufferParam("uint32_t", "cp", "1", out=True),)
    )


_OUT_SLICE = "int decode(uint32_t *cp) { *cp = 0; return 1; }"


@pytest.mark.parametrize(
    "expr",
    [
        "*cp == 0",
        "cp[0] == 0",
        "*(cp + 0) == 0",
        "*(cp) == 0",
        "(int)*cp == 0",  # a cast's `)` before `*` is a unary deref, not a multiply
        "(uint32_t) * cp == 0",
        "(char * const)*cp == 0",  # a qualified pointer cast, still a unary deref
        "sizeof *cp >= 4",  # `sizeof *cp` is `sizeof(*cp)`, a deref on a scalar
    ],
)
def test_scalar_backed_output_deref_is_error(expr: str) -> None:
    # A scalar-backed output is bound as a plain scalar; dereferencing or
    # subscripting it would not compile, so the harness refuses to emit it rather
    # than leak un-compilable C to esbmc. `*(cp + 0)`/`*(cp)` are the parenthesized
    # forms the old proposer regex silently accepted; `(int)*cp`, the qualified
    # `(char * const)*cp`, and `sizeof *cp` are the casted/keyword derefs a
    # closing-`)`-is-an-operand shortcut let slip (PR #136 review).
    with pytest.raises(HarnessError, match="scalar-backed output 'cp'"):
        render_semantic_harness(
            unit_source=_OUT_SLICE, signature=_out_sig(), spec=SemanticSpec(expr)
        )


def test_domain_over_output_param_is_error() -> None:
    # A precondition is emitted as __ESBMC_assume *before* the call, so it may not
    # constrain an output parameter -- that would preconstrain a would-be result on
    # uninitialized storage, masking a unit that never writes it.
    with pytest.raises(HarnessError, match="output parameter 'cp'"):
        render_semantic_harness(
            unit_source=_OUT_SLICE,
            signature=_out_sig(),
            spec=SemanticSpec("result >= 0", ("cp <= 0",)),
        )


def test_scalar_backed_output_named_directly_renders() -> None:
    # Naming the output directly is valid -- the unit writes it -- so the predicate
    # must not over-reject it.
    out = render_semantic_harness(
        unit_source=_OUT_SLICE,
        signature=_out_sig(),
        spec=SemanticSpec("cp <= 0x10FFFF"),
    )
    assert "uint32_t cp;" in out
    assert '__ESBMC_assert((cp <= 0x10FFFF), "forseti:semantic");' in out


def test_renderability_reason_accepts_scalar_signature() -> None:
    # No output buffers -> neither emission rule can fire.
    spec = SemanticSpec("result >= 0", ("x > 0",))
    assert renderability_reason(ABS_SIG, spec) is None


def test_renderability_reason_accepts_directly_named_output() -> None:
    assert renderability_reason(_out_sig(), SemanticSpec("cp <= 0x10FFFF")) is None


@pytest.mark.parametrize(
    "expr",
    [
        "*cp == 0",
        "cp[0] == 0",
        "*(cp + 0) == 0",
        "(int)*cp == 0",
        "(char * const)*cp == 0",
        "sizeof *cp >= 4",
    ],
)
def test_renderability_reason_flags_scalar_output_deref(expr: str) -> None:
    # `(int)*cp` casts then unary-derefs `cp`; `(char * const)*cp` is the same with
    # a qualified pointer type; `sizeof *cp` derefs the operand `sizeof` binds. The
    # reason must fire for each so the property is rejected rather than emitted as
    # an invalid `*cp` on a scalar (PR #136).
    reason = renderability_reason(_out_sig(), SemanticSpec(expr))
    assert reason is not None and "scalar-backed output 'cp'" in reason


@pytest.mark.parametrize(
    "expr", ["result * cp >= 0", "result * (cp + 1) >= 0", "cp * result >= 0"]
)
def test_renderability_reason_accepts_scalar_output_multiplication(expr: str) -> None:
    # Multiplying a scalar-backed output is valid C -- the binary `*` names `cp`,
    # it does not dereference it. Regression for #106: the deref guard must not
    # turn this into a HarnessError / per-property ERROR verdict on the render path.
    assert renderability_reason(_out_sig(), SemanticSpec(expr)) is None


def test_renderability_reason_flags_domain_over_output() -> None:
    reason = renderability_reason(_out_sig(), SemanticSpec("result >= 0", ("cp <= 0",)))
    assert reason is not None and "output parameter 'cp'" in reason


def _suffix_out_sig() -> UnitSignature:
    # An output named like a C integer suffix (`u`/`L`/`UL`): a precondition over an
    # input that carries a suffixed literal (e.g. `x < 10u`) must not be misread as
    # constraining this output.
    return UnitSignature(
        "f",
        "int",
        (ScalarParam("int64_t", "x"), BufferParam("uint32_t", "u", "1", out=True)),
    )


@pytest.mark.parametrize("pre", ["x < 10u", "x <= 10UL", "x > 0L && x < 5u"])
def test_renderability_reason_ignores_literal_suffix_named_output(pre: str) -> None:
    # A raw identifier scan matches the `u`/`L`/`UL` inside a suffixed literal as the
    # output name; boundary-aware detection (`\bu\b` misses `10u`) must not flag it.
    spec = SemanticSpec("result >= 0", (pre,))
    assert renderability_reason(_suffix_out_sig(), spec) is None


def test_renderability_reason_still_flags_genuine_suffix_output_ref() -> None:
    # Guard against over-correcting: a real reference to output `u` is still rejected.
    spec = SemanticSpec("result >= 0", ("u < 5",))
    reason = renderability_reason(_suffix_out_sig(), spec)
    assert reason is not None and "output parameter 'u'" in reason


def test_renderability_reason_flags_empty_postcondition() -> None:
    # The single static authority now owns the structural guards too, so a caller
    # (the proposer's gate) can reject an empty postcondition without rendering.
    reason = renderability_reason(ABS_SIG, SemanticSpec("   "))
    assert reason is not None and "empty postcondition" in reason


def test_renderability_reason_flags_result_var_param_clash() -> None:
    reason = renderability_reason(ABS_SIG, SemanticSpec("x >= 0", result_var="x"))
    assert reason is not None and "collides with a parameter name" in reason


def test_renderability_reason_flags_void_return_referencing_result() -> None:
    void_sig = UnitSignature("consume", "void", (ScalarParam("int", "x"),))
    reason = renderability_reason(void_sig, SemanticSpec("result >= 0"))
    assert reason is not None and "returns void" in reason


_BUF_SIG = UnitSignature(
    "first",
    "int",
    (BufferParam("int", "a", "n", const=True), ScalarParam("unsigned", "n")),
)


def test_renderability_reason_flags_mixed_buffer_length_clause() -> None:
    # A single clause constraining both the buffer `a` and its length `n` has no
    # correct emission point, so the static authority rejects it up front -- the
    # `_split_assumptions` guard is now a `(signature, spec)` rule the proposer's
    # gate can see without rendering, closing the propose/check divergence removing
    # the renderer seam would otherwise open.
    reason = renderability_reason(
        _BUF_SIG, SemanticSpec("result == a[0]", ("n >= 1 && n <= 2 && a[0] >= 0",))
    )
    assert reason is not None and "constrains both a buffer" in reason


def test_static_gate_and_renderer_agree_on_mixed_clause() -> None:
    # Parity: the same mixed clause the static authority rejects, the renderer
    # rejects with the same message -- the invariant the "single static authority"
    # refactor promises, now covering this guard too.
    spec = SemanticSpec("result == a[0]", ("n >= 1 && n <= 2 && a[0] >= 0",))
    assert renderability_reason(_BUF_SIG, spec) is not None
    with pytest.raises(HarnessError, match="constrains both a buffer"):
        render_semantic_harness(
            unit_source="int first(const int *a, unsigned n) { return a[0]; }",
            signature=_BUF_SIG,
            spec=spec,
        )


def test_renderability_reason_allows_length_used_only_as_an_index() -> None:
    # A length identifier inside a subscript is an index use, not a size
    # constraint, so `a[n - 1] >= 0` has a valid emission point (after the
    # allocation + fill) and the static gate must not discard it (#123).
    spec = SemanticSpec("result == a[n - 1]", ("n >= 1", "a[n - 1] >= 0"))
    assert renderability_reason(_BUF_SIG, spec) is None


def test_renderability_reason_flags_index_use_mixed_with_a_size_bound() -> None:
    # The index use is fine on its own, but `&& n >= 1` is a genuine size bound
    # that must precede the allocation -- one clause, two required positions.
    reason = renderability_reason(
        _BUF_SIG, SemanticSpec("result == a[n - 1]", ("a[n - 1] >= 0 && n >= 1",))
    )
    assert reason is not None and "constrains both a buffer" in reason


def test_renderability_reason_flags_constant_index_mixed_with_a_size_bound() -> None:
    # The rule is "a length identifier outside a subscript", not "any subscript
    # makes the clause renderable": a constant index plus a real bound still has
    # no single emission point.
    reason = renderability_reason(
        _BUF_SIG, SemanticSpec("result == a[0]", ("a[0] >= 0 && n <= 4",))
    )
    assert reason is not None and "constrains both a buffer" in reason


_TWO_BUF_SIG = UnitSignature(
    "pick",
    "int",
    (
        BufferParam("int", "a", "n", const=True),
        ScalarParam("unsigned", "n"),
        BufferParam("int", "b", "m", const=True),
        ScalarParam("unsigned", "m"),
    ),
)


def test_index_use_of_another_buffers_length_allowed() -> None:
    # Multi-buffer: every buffer's length ident is stripped from every buffer's
    # subscript, so indexing `a` by `b`'s length `m` is still an index use.
    spec = SemanticSpec("result == a[m - 1]", ("n >= 1", "m >= 1", "a[m - 1] >= 0"))
    assert renderability_reason(_TWO_BUF_SIG, spec) is None


def test_index_use_of_another_buffers_length_mixed_with_bound_rejected() -> None:
    reason = renderability_reason(
        _TWO_BUF_SIG, SemanticSpec("result == a[m - 1]", ("a[m - 1] >= 0 && m >= 1",))
    )
    assert reason is not None and "constrains both a buffer" in reason


def test_nested_buffer_index_collapses_to_an_index_use() -> None:
    # `a[b[0]]` needs the fixpoint strip: the inner `b[0]` goes first, then the
    # outer `a[b]`. Nothing is left outside a subscript, so it is renderable.
    spec = SemanticSpec("result >= 0", ("a[b[0]] >= 0",))
    assert renderability_reason(_TWO_BUF_SIG, spec) is None
    reason = renderability_reason(
        _TWO_BUF_SIG, SemanticSpec("result >= 0", ("a[b[0]] >= 0 && n >= 1",))
    )
    assert reason is not None and "constrains both a buffer" in reason


def test_static_gate_and_renderer_agree_on_index_use() -> None:
    # Parity in the accepting direction: what the static authority now admits, the
    # renderer emits -- the same single-authority invariant, read the other way.
    spec = SemanticSpec("result == a[n - 1]", ("n >= 1", "a[n - 1] >= 0"))
    assert renderability_reason(_BUF_SIG, spec) is None
    out = render_semantic_harness(
        unit_source="int first(const int *a, unsigned n) { return a[0]; }",
        signature=_BUF_SIG,
        spec=spec,
    )
    assert "__ESBMC_assume((a[n - 1] >= 0));" in out


_SUFFIX_OUT_SLICE = "int f(int64_t x, uint32_t *u) { *u = 0; return 1; }"


def test_literal_suffix_named_output_renders() -> None:
    # End-to-end: the writer emits the harness instead of raising on `x < 10u`.
    out = render_semantic_harness(
        unit_source=_SUFFIX_OUT_SLICE,
        signature=_suffix_out_sig(),
        spec=SemanticSpec("result >= 0", ("x < 10u",)),
    )
    assert "__ESBMC_assume((x < 10u));" in out
    assert "uint32_t u;" in out


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


def test_extract_signature_ambiguous_multibuffer_is_error() -> None:
    # An interior pointer not followed by its length (two buffers sharing a
    # trailing length) is ambiguous -- fail loud instead of inventing length 1.
    with pytest.raises(HarnessError):
        extract_signature(
            "int dot(const int *a, const int *b, size_t n) { return 0; }", "dot"
        )


def test_spec_from_semantic_property() -> None:
    prop = _semantic_property("result >= 0", ("x > INT64_MIN",))
    assert spec_from_property(prop) == SemanticSpec("result >= 0", ("x > INT64_MIN",))


def test_spec_from_reachability_property_is_error() -> None:
    prop = _semantic_property("some_label", (), kind=PropertyKind.REACHABILITY)
    with pytest.raises(HarnessError):
        spec_from_property(prop)


# render_property_harness is the "I only have source + symbol" entry point: it
# parses the signature *from the slice* (extract_signature), projects the stored
# Property onto a SemanticSpec (spec_from_property), and renders the harness
# (render_semantic_harness). These tests target that composed behaviour -- the
# signature being inferred from source, not handed in pre-parsed -- and the
# fail-loud contract that a HarnessError from any leg of the recipe surfaces.


def test_render_property_harness_parses_scalar_signature_and_renders() -> None:
    out = render_property_harness(
        unit_source=ABS_SLICE,
        symbol="my_abs",
        prop=_semantic_property("result >= 0", ("x > INT64_MIN",)),
    )
    assert ABS_SLICE in out  # the unit is inlined verbatim
    # the scalar param and its type were recovered from the slice, not supplied
    assert "int64_t x = nondet_int64_t();" in out
    assert "int64_t result = my_abs(x);" in out
    assert "__ESBMC_assume((x > INT64_MIN));" in out
    assert '__ESBMC_assert((result >= 0), "forseti:semantic");' in out


def test_render_property_harness_infers_buffer_and_output_from_source() -> None:
    # The distinguishing behaviour vs render_semantic_harness: the (buffer,
    # length) and trailing-output classification is inferred from the slice. A
    # `uint32_t *cp` with no following length becomes a scalar-backed output
    # (`uint32_t cp;`, passed by address), and `const unsigned char *b` with a
    # following `unsigned len` becomes a nondet-filled, malloc-backed buffer.
    slice_ = (
        "int decode(const unsigned char *b, unsigned len, uint32_t *cp)"
        " { *cp = 0; return 1; }"
    )
    out = render_property_harness(
        unit_source=slice_,
        symbol="decode",
        prop=_semantic_property(
            "result <= 0 || cp <= 0x10FFFF", ("len >= 1 && len <= 4",)
        ),
    )
    # (buffer, length) idiom inferred
    assert "unsigned char *b = malloc((len) * sizeof(unsigned char));" in out
    assert "uint32_t cp;" in out  # scalar-backed output, not an array
    assert "int result = decode(b, len, &cp);" in out  # output passed by address


def test_render_property_harness_unparseable_symbol_raises() -> None:
    # extract_signature can't find the symbol -> HarnessError surfaces.
    with pytest.raises(HarnessError):
        render_property_harness(
            unit_source=ABS_SLICE,
            symbol="not_there",
            prop=_semantic_property("result >= 0", ()),
        )


def test_render_property_harness_reachability_property_raises() -> None:
    # spec_from_property rejects a non-semantic kind -> HarnessError surfaces.
    with pytest.raises(HarnessError):
        render_property_harness(
            unit_source=ABS_SLICE,
            symbol="my_abs",
            prop=_semantic_property("some_label", (), kind=PropertyKind.REACHABILITY),
        )


def test_render_property_harness_unrenderable_property_raises() -> None:
    # Signature parses, but a void unit whose postcondition names `result` is
    # unrenderable -> the render leg's HarnessError surfaces.
    with pytest.raises(HarnessError):
        render_property_harness(
            unit_source="void consume(int x) { (void)x; }",
            symbol="consume",
            prop=_semantic_property("result >= 0", ()),
        )


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


def test_unit_source_with_main_prototype_is_not_error() -> None:
    """A forward-declared `main` (no body) is not a *definition* -- this
    render-level guard (which only asks "is `main` defined here?") does not
    reject it (issue #95 review).

    This does NOT by itself guarantee the rendered harness compiles: a
    prototype whose signature doesn't match the harness's own generated
    `int main(void) { ... }` (e.g. `void main(void);`) is a hard esbmc parse
    error ("conflicting types"), verified against a live esbmc run -- a
    signature-*matching* prototype like the one below is the case this render
    guard alone actually leaves safe. Guaranteeing no left-behind prototype of
    *any* signature reaches here is `Unit.from_path`'s job (it renames a
    declaration alongside every definition, `orchestrator/ports.py`'s
    `rename_all_declarations_and_definitions`), not this function's --
    covered by `tests/orchestrator/test_ports.py`."""
    render_semantic_harness(
        unit_source="int main(void);\n" + ABS_SLICE,
        signature=ABS_SIG,
        spec=SemanticSpec("result >= 0"),
    )


def test_unit_source_with_main_only_in_a_comment_is_not_error() -> None:
    """A comment mentioning `main()` with no real definition must not trip the
    guard either (issue #95 review)."""
    render_semantic_harness(
        unit_source="/* main() drives production */\n" + ABS_SLICE,
        signature=ABS_SIG,
        spec=SemanticSpec("result >= 0"),
    )


class _FakeParam:
    """Not a Scalar/BufferParam, but shaped enough to reach the subtype guard."""

    name = "z"


def test_unknown_param_subtype_is_error() -> None:
    bogus = UnitSignature("f", "int", (_FakeParam(),))  # ty: ignore[invalid-argument-type]
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


# The full macro allowlist as of the #81 consolidation, pasted as a known-good
# literal. This is an *independent* second construction of the set: the harness
# derives HARNESS_MACROS by union over the headers in DEFAULT_INCLUDES, so a
# miscategorised or typo'd macro in that header map makes the two disagree and
# this guard fails. It also pins behaviour across the proposer._MACROS move.
_EXPECTED_HARNESS_MACROS = frozenset(
    {
        "NULL",
        "CHAR_MIN",
        "CHAR_MAX",
        "SCHAR_MIN",
        "SCHAR_MAX",
        "UCHAR_MAX",
        "SHRT_MIN",
        "SHRT_MAX",
        "USHRT_MAX",
        "INT_MIN",
        "INT_MAX",
        "UINT_MAX",
        "LONG_MIN",
        "LONG_MAX",
        "ULONG_MAX",
        "LLONG_MIN",
        "LLONG_MAX",
        "ULLONG_MAX",
        "INT8_MIN",
        "INT8_MAX",
        "UINT8_MAX",
        "INT16_MIN",
        "INT16_MAX",
        "UINT16_MAX",
        "INT32_MIN",
        "INT32_MAX",
        "UINT32_MAX",
        "INT64_MIN",
        "INT64_MAX",
        "UINT64_MAX",
        "INTMAX_MIN",
        "INTMAX_MAX",
        "UINTMAX_MAX",
        "INTPTR_MIN",
        "INTPTR_MAX",
        "UINTPTR_MAX",
        "SIZE_MAX",
        "PTRDIFF_MIN",
        "PTRDIFF_MAX",
    }
)


def test_harness_macros_match_known_good_set() -> None:
    from forseti.properties.harness import HARNESS_MACROS

    assert HARNESS_MACROS == _EXPECTED_HARNESS_MACROS


def test_default_includes_are_emitted_and_named_once() -> None:
    # HARNESS_MACROS is only sound because the default harness emits these
    # headers; the derivation ties the allowlist to DEFAULT_INCLUDES so the two
    # cannot drift out of lockstep (#81). Assert render emits exactly the headers
    # DEFAULT_INCLUDES names, in order, and that representative macros from each
    # default header are in the allowlist.
    from forseti.properties.harness import DEFAULT_INCLUDES, HARNESS_MACROS

    assert DEFAULT_INCLUDES == ("stdint.h", "stddef.h", "limits.h")
    out = render_semantic_harness(
        unit_source=ABS_SLICE,
        signature=ABS_SIG,
        spec=SemanticSpec("result >= 0"),
    )
    for header in DEFAULT_INCLUDES:
        assert f"#include <{header}>" in out
    assert {"INT_MIN", "SIZE_MAX", "NULL"} <= HARNESS_MACROS


# --- UnitSignature parameter roles ---------------------------------------
# A signature is the single authority on which of its parameters are inputs and
# which are outputs; the proposer's gate and the harness renderer both consult
# it instead of re-deriving `isinstance(p, BufferParam) and p.out`. Expected
# partitions below are written out by hand, not recomputed from the model.

_MIXED_SIG = UnitSignature(
    "copy_into",
    "int",
    (
        ScalarParam("int", "x"),
        BufferParam("unsigned char", "buf", "len"),  # input buffer
        ScalarParam("size_t", "len"),
        BufferParam("int", "out", "n", out=True),  # output buffer (array)
        BufferParam("uint32_t", "cp", "1", out=True),  # scalar-backed output
    ),
)


def test_param_names_is_every_parameter() -> None:
    assert _MIXED_SIG.param_names == frozenset({"x", "buf", "len", "out", "cp"})


def test_output_param_names_are_the_out_buffers() -> None:
    # Only `out=True` buffer params are outputs -- a scalar-backed output (`cp`,
    # length "1") counts too; plain scalars and input buffers do not.
    assert _MIXED_SIG.output_param_names == frozenset({"out", "cp"})


def test_input_param_names_are_the_complement_of_outputs() -> None:
    assert _MIXED_SIG.input_param_names == frozenset({"x", "buf", "len"})


def test_all_scalar_signature_has_no_output_params() -> None:
    assert ABS_SIG.output_param_names == frozenset()
    assert ABS_SIG.input_param_names == frozenset({"x"})
    assert ABS_SIG.param_names == frozenset({"x"})


def test_input_and_output_names_partition_param_names() -> None:
    # The two role sets are disjoint and together cover every parameter.
    assert (
        _MIXED_SIG.input_param_names | _MIXED_SIG.output_param_names
        == _MIXED_SIG.param_names
    )
    assert not (_MIXED_SIG.input_param_names & _MIXED_SIG.output_param_names)


# --- default length cap (#299) ------------------------------------------------
#
# A malloc-backed buffer's length is otherwise a symbolic, effectively unbounded
# fill-loop trip count, so no finite unwind settles a property over it. The cap
# is opt-in at the renderer (`max_len=None` keeps #297/#298's genuinely
# unconstrained length) and skipped where the property's own domain already
# constrains the length, since capping on top could contradict it and turn the
# check vacuous.

_FIRST_SRC = "int first(const int *a, unsigned n) { return 0; }"
_CAP_N = "__ESBMC_assume((n) <= 8);"


def _render_first(*domain: str, max_len: int | None) -> str:
    return render_semantic_harness(
        unit_source=_FIRST_SRC,
        signature=_BUF_SIG,
        spec=SemanticSpec("result == result", domain),
        max_len=max_len,
    )


def test_max_len_none_emits_no_cap() -> None:
    assert "<= 8" not in _render_first(max_len=None)
    assert plan_length_bounds(_BUF_SIG, SemanticSpec("result == result"), None) == ()


def test_max_len_caps_an_unconstrained_length_before_the_allocation() -> None:
    out = _render_first(max_len=8)
    assert _CAP_N in out
    assert out.index("unsigned n = nondet_unsigned();") < out.index(_CAP_N)
    assert out.index(_CAP_N) < out.index(_ALLOC_A_N)


@pytest.mark.parametrize(
    "domain",
    [
        "n <= 4",
        "n >= 1 && n <= 4",
        "n == 3",
        "n < 100",
        "n >= 16",
        "n >= 0x10",
        "n > 8",
        "(n >= 1) && (n >= 2)",
        "n >= 1 || n == 20",
    ],
)
def test_domain_clause_naming_the_length_suppresses_the_cap(domain: str) -> None:
    # The domain is authoritative: `n >= 16` under a cap of 8 would assume
    # `false` and settle every property vacuously HELD, and `n <= 100` capped to
    # 8 would silently narrow an explicit bound.
    out = _render_first(domain, max_len=8)
    assert _CAP_N not in out
    assert plan_length_bounds(_BUF_SIG, SemanticSpec("result == 0", (domain,)), 8) == ()


@pytest.mark.parametrize(
    "domain",
    ["n >= 1", "n > 0", "n != 0", "1 <= n", "0 < n", "(n >= 1)", "n >= 8", "n >= 0"],
)
def test_lower_bound_the_cap_can_satisfy_still_gets_the_cap(domain: str) -> None:
    # The most common proposer domain (`len >= 1`) must not disable the cap, or
    # the property stays UNKNOWN by default; the floor still fits under 8, so the
    # two assumes are jointly satisfiable.
    assert plan_length_bounds(_BUF_SIG, SemanticSpec("result == 0", (domain,)), 8) == (
        ("n", 8),
    )
    out = _render_first(domain, max_len=8)
    assert out.index(_CAP_N) < out.index(_ALLOC_A_N)


def test_lower_bound_above_the_cap_disables_it() -> None:
    spec = SemanticSpec("result == 0", ("n >= 5",))
    assert plan_length_bounds(_BUF_SIG, spec, 8) == (("n", 8),)
    assert plan_length_bounds(_BUF_SIG, spec, 4) == ()
    assert (
        plan_length_bounds(_BUF_SIG, SemanticSpec("result == 0", ("n > 4",)), 4) == ()
    )


def test_length_used_only_as_an_index_does_not_suppress_the_cap() -> None:
    # `a[n - 1]` is a buffer-content predicate emitted after the fill, not a
    # size constraint, so the length is still unconstrained.
    spec = SemanticSpec("result == 0", ("a[n - 1] >= 0",))
    assert plan_length_bounds(_BUF_SIG, spec, 8) == (("n", 8),)


def test_cap_is_scoped_to_the_length_the_domain_leaves_open() -> None:
    sig = UnitSignature(
        "f",
        "int",
        (
            BufferParam("int", "a", "n", const=True),
            ScalarParam("unsigned", "n"),
            BufferParam("int", "b", "m", const=True),
            ScalarParam("unsigned", "m"),
        ),
    )
    spec = SemanticSpec("result == 0", ("m <= 2",))
    assert plan_length_bounds(sig, spec, 8) == (("n", 8),)


def test_shared_length_is_capped_once() -> None:
    sig = UnitSignature(
        "dot",
        "int",
        (
            BufferParam("int", "a", "n", const=True),
            BufferParam("int", "b", "n", const=True),
            ScalarParam("unsigned", "n"),
        ),
    )
    out = render_semantic_harness(
        unit_source="int dot(const int *a, const int *b, unsigned n) { return 0; }",
        signature=sig,
        spec=SemanticSpec("result == 0"),
        max_len=8,
    )
    assert out.count(_CAP_N) == 1


def test_compound_length_caps_each_scalar_param_it_names() -> None:
    sig = UnitSignature(
        "f",
        "int",
        (
            BufferParam("int", "a", "w * h", const=True),
            ScalarParam("unsigned", "w"),
            ScalarParam("unsigned", "h"),
        ),
    )
    assert plan_length_bounds(sig, SemanticSpec("result == 0"), 4) == (
        ("w", 4),
        ("h", 4),
    )


@pytest.mark.parametrize("length", ["16", "N", "1"])
def test_non_param_length_is_not_capped(length: str) -> None:
    # A literal/macro length is not nondet: assuming `N <= 8` on a macro whose
    # value is 16 would make the harness vacuous.
    sig = UnitSignature(
        "f",
        "int",
        (BufferParam("int", "a", length, const=True), ScalarParam("int", "x")),
    )
    assert plan_length_bounds(sig, SemanticSpec("result == 0"), 8) == ()


def test_scalar_backed_output_is_not_capped() -> None:
    sig = UnitSignature(
        "f",
        "int",
        (
            BufferParam("unsigned char", "b", "len", const=True),
            ScalarParam("unsigned", "len"),
            BufferParam("uint32_t", "cp", "1", out=True),
        ),
    )
    assert plan_length_bounds(sig, SemanticSpec("result == 0"), 8) == (("len", 8),)


def test_negative_max_len_is_an_error() -> None:
    # `n <= -1` on an unsigned length assumes `false`: every property vacuously HELD.
    with pytest.raises(HarnessError, match="max_len"):
        plan_length_bounds(_BUF_SIG, SemanticSpec("result == 0"), -1)
    with pytest.raises(HarnessError, match="max_len"):
        _render_first(max_len=-1)


def test_zero_max_len_is_allowed() -> None:
    assert plan_length_bounds(_BUF_SIG, SemanticSpec("result == 0"), 0) == (("n", 0),)


def test_build_property_harness_reports_the_bounds_it_applied() -> None:
    prop = _semantic_property("result >= 0", ("n >= 1",))
    built = build_property_harness(
        unit_source=_FIRST_SRC, symbol="first", prop=prop, max_len=8
    )
    assert built.length_bounds == (("n", 8),)
    assert _CAP_N in built.source_text
    assert built.source_text == render_property_harness(
        unit_source=_FIRST_SRC, symbol="first", prop=prop, max_len=8
    )


def test_build_property_harness_reports_no_bounds_when_domain_bounds_the_length() -> (
    None
):
    prop = _semantic_property("result >= 0", ("n <= 4",))
    built = build_property_harness(
        unit_source=_FIRST_SRC, symbol="first", prop=prop, max_len=8
    )
    assert built.length_bounds == ()
    assert "<= 8" not in built.source_text

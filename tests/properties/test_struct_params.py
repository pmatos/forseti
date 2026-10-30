"""Struct-pointer parameters in the semantic property path (#422), hermetic.

A function that takes ``T *`` is read as one nondet ``T`` (not a buffer), a candidate
that names ``r->field`` is resolved against the fields the unit's own definition of
``T`` declares, and every verdict over the nondet instance carries an *assumed* note.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from forseti.core._proposal_request import prepare_proposal_request
from forseti.orchestrator import SemanticHarnessWriter, Unit
from forseti.properties import (
    BufferParam,
    CandidateSpec,
    HarnessError,
    LLMClient,
    Property,
    PropertyKind,
    PropertyStatus,
    ProposalRequest,
    Provenance,
    SemanticSpec,
    StructParam,
    build_property_harness,
    extract_signature,
    make_property_id,
    propose_properties,
    render_semantic_harness,
    validate_candidate,
)
from forseti.properties.cexpr import identifiers, member_accesses, references
from forseti.properties.signature import ScalarParam, struct_definitions

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"

RING = """\
#define CAP 4
typedef struct {
    int buf[CAP];  /* ring storage */
    unsigned head, tail, count;
} ring_t;

int rb_push(ring_t *r, int v) {
    if (r->count == CAP) return 0;
    r->buf[r->tail] = v;
    r->tail = (r->tail + 1) % CAP;
    r->count++;
    return 1;
}
"""

SHA = """\
#include <stdint.h>
typedef struct {
    uint32_t state[5];
    uint64_t count;
    uint8_t buffer[64];
} sha1_ctx;

void sha1_update(sha1_ctx *ctx, const uint8_t *data, size_t len) {
    for (size_t i = 0; i < len; i++) {
        ctx->buffer[ctx->count % 64] = data[i];
        ctx->count++;
    }
}
"""


def _ring_sig() -> object:
    return extract_signature(RING, "rb_push", infer_structs=True)


def test_struct_definitions_read_typedefs_tags_and_both() -> None:
    src = """\
    typedef struct { int a; char *name; void (*cb)(int); } plain_t;
    struct node { struct node *next; unsigned flags : 3, : 2; long v[4], w; };
    typedef struct pair { int x, y; } pair_t, *pair_p;
    typedef struct { struct { int deep; } inner; int top; } nested_t;
    """
    found = struct_definitions(src)
    assert found["plain_t"] == ("a", "name", "cb")
    assert found["struct node"] == ("next", "flags", "v", "w")
    assert found["pair_t"] == found["struct pair"] == ("x", "y")
    assert "pair_p" not in found
    assert found["nested_t"] == ("inner", "top")


def test_struct_definitions_ignore_comments_and_unreadable_bodies() -> None:
    src = "/* struct ghost { int g; }; */ // struct ghost2 { int g; };\nstruct s {"
    assert struct_definitions(src) == {}


def test_a_defined_struct_pointer_is_one_instance_not_a_buffer_length_pair() -> None:
    sig = extract_signature(RING, "rb_push", infer_structs=True)
    ring, value = sig.params
    assert isinstance(ring, StructParam)
    assert ring.elem_ctype == "ring_t" and ring.name == "r"
    assert ring.fields == ("buf", "head", "tail", "count")
    assert isinstance(value, ScalarParam)


def test_without_inference_the_old_reading_is_kept() -> None:
    sig = extract_signature(RING, "rb_push")
    assert not any(isinstance(p, StructParam) for p in sig.params)


def test_struct_pointer_beside_a_buffer_pair() -> None:
    sig = extract_signature(SHA, "sha1_update", infer_structs=True)
    ctx, data, length = sig.params
    assert isinstance(ctx, StructParam) and ctx.fields == ("state", "count", "buffer")
    assert isinstance(data, BufferParam) and not isinstance(data, StructParam)
    assert data.length == "len"
    assert isinstance(length, ScalarParam)


def test_struct_pointer_followed_by_a_length_keeps_the_buffer_idiom() -> None:
    src = (
        "typedef struct { int x; } pt;\nint sum(const pt *pts, size_t n) { return 0; }"
    )
    sig = extract_signature(src, "sum", infer_structs=True)
    assert not any(isinstance(p, StructParam) for p in sig.params)


def test_an_undefined_struct_keeps_the_old_reading() -> None:
    src = "int f(opaque_t *o, int v) { return v; }"
    sig = extract_signature(src, "f", infer_structs=True)
    assert not any(isinstance(p, StructParam) for p in sig.params)


def test_member_names_are_not_variable_references() -> None:
    assert identifiers("r->count < CAP && r->buf[r->head] > v") == [
        "r",
        "CAP",
        "r",
        "r",
        "v",
    ]
    assert not references("ctx->len > 0", "len")
    assert references("ctx->len > len", "len")
    assert member_accesses("r->count + p.x + a->b->c + (*q).z") == [
        ("r", "->", "count"),
        ("p", ".", "x"),
        ("a", "->", "b"),
    ]


def _validate(expression: str, *domain: str) -> str | None:
    return validate_candidate(
        CandidateSpec(expression, domain),
        extract_signature(RING, "rb_push", infer_structs=True),
    )


def test_a_real_field_expression_is_accepted() -> None:
    assert _validate("result == 0 || r->count > 0", "r->count < 4") is None
    assert _validate("r->buf[r->tail] == v || result == 0") is None


def test_an_unknown_field_is_rejected_with_its_name() -> None:
    reason = _validate("r->size <= 4")
    assert reason is not None
    assert "'size'" in reason and "ring_t" in reason and "count" in reason
    domain_reason = _validate("result == 0 || r->count > 0", "r->bogus < 3")
    assert domain_reason is not None and "'bogus'" in domain_reason


def test_a_bare_field_name_points_to_the_pointer_spelling() -> None:
    reason = _validate("count <= 4")
    assert reason is not None
    assert "unknown identifier 'count'" in reason and "r->count" in reason


def test_a_bare_unknown_name_is_still_just_unknown() -> None:
    reason = _validate("ghost <= 4")
    assert reason is not None and "->" not in reason


def test_dot_access_through_the_pointer_is_rejected() -> None:
    reason = _validate("r.count <= 4")
    assert reason is not None and "r->count" in reason


def test_members_off_other_names_are_left_to_the_compiler() -> None:
    assert _validate("result == 0 || r->buf[0] >= 0 || v->x > 0") is None


def test_a_struct_without_a_visible_field_list_checks_no_member() -> None:
    sig = extract_signature(
        RING, "rb_push", struct_types=("ring_t",), infer_structs=False
    )
    (ring, _) = sig.params
    assert isinstance(ring, StructParam)
    bare = StructParam(ring.elem_ctype, ring.name, "1")
    open_sig = type(sig)(sig.symbol, sig.return_ctype, (bare, sig.params[1]))
    assert validate_candidate(CandidateSpec("r->anything > 0"), open_sig) is None


def _harness(expression: str, *domain: str, probe: bool = False) -> str:
    return render_semantic_harness(
        unit_source=RING,
        signature=extract_signature(RING, "rb_push", infer_structs=True),
        spec=SemanticSpec(expression, domain),
        probe=probe,
    )


def test_the_struct_is_one_nondet_object_bound_as_a_pointer() -> None:
    text = _harness("result == 0 || r->count > 0", "r->count < CAP")
    body = text.split("int main(void) {\n")[1].splitlines()
    assert "ring_t nondet_ring_t(void);" in text
    obj = body.index("    ring_t r__obj = nondet_ring_t();")
    assert body[obj + 1] == "    ring_t *r = &r__obj;"
    assume = body.index("    __ESBMC_assume((r->count < CAP));")
    assert obj < assume
    assert "    int result = rb_push(r, v);" in body
    assert "malloc" not in text


def test_a_domain_mixing_struct_and_scalar_renders() -> None:
    text = _harness("result == 0 || r->count > 0", "v > 0 && r->count < CAP")
    assert "__ESBMC_assume((v > 0 && r->count < CAP));" in text


def test_the_probe_twin_differs_only_after_the_call() -> None:
    real = _harness("r->count > 0").split("rb_push(r, v);")
    probe = _harness("r->count > 0", probe=True).split("rb_push(r, v);")
    assert real[0] == probe[0]
    assert "forseti:semantic" in real[1] and "forseti:non-vacuity" in probe[1]


def test_struct_beside_a_buffer_allocates_only_the_buffer() -> None:
    sig = extract_signature(SHA, "sha1_update", infer_structs=True)
    text = render_semantic_harness(
        unit_source=SHA,
        signature=sig,
        spec=SemanticSpec("ctx->count >= 0", ("ctx->count < 1000", "len >= 1")),
        max_len=4,
    )
    assert "sha1_ctx ctx__obj = nondet_sha1_ctx();" in text
    assert "uint8_t *data = malloc((len) * sizeof(uint8_t));" in text
    assert "free(data);" in text and "free(ctx)" not in text
    assert "__ESBMC_assume((len >= 0 && len <= 4));" in text
    assert text.count("malloc(") == 1
    assert "    sha1_update(ctx, data, len);" in text


def test_a_clause_over_a_struct_and_a_length_is_not_a_mixed_buffer_clause() -> None:
    sig = extract_signature(SHA, "sha1_update", infer_structs=True)
    spec = SemanticSpec("data[0] >= 0", ("ctx->count + len < 1000",))
    text = render_semantic_harness(unit_source=SHA, signature=sig, spec=spec)
    assert "__ESBMC_assume((ctx->count + len < 1000));" in text


def test_a_field_named_like_a_buffer_does_not_reference_the_buffer() -> None:
    sig = extract_signature(SHA, "sha1_update", infer_structs=True)
    text = render_semantic_harness(
        unit_source=SHA,
        signature=sig,
        spec=SemanticSpec("ctx->buffer[0] >= 0", ("ctx->count < len",)),
    )
    assert "__ESBMC_assume((ctx->count < len));" in text


def test_struct_pointer_writer_output_is_labelled_assumed() -> None:
    prop = _prop("rb_push", "result == 0 || r->count > 0", ("r->count < CAP",))
    harness = build_property_harness(unit_source=RING, symbol="rb_push", prop=prop)
    assert harness.assumption is not None
    assert "r (ring_t)" in harness.assumption and "nondet" in harness.assumption
    unit = Unit("ring.c::rb_push", Path("ring.c"), "rb_push", RING)
    rendered = SemanticHarnessWriter().render(unit, prop)
    assert rendered.assumption == harness.assumption


def test_a_scalar_unit_assumes_nothing() -> None:
    src = "int id(int x) { return x; }"
    harness = build_property_harness(
        unit_source=src, symbol="id", prop=_prop("id", "result == x")
    )
    assert harness.assumption is None


def test_an_empty_postcondition_is_still_refused_for_a_struct_unit() -> None:
    with pytest.raises(HarnessError):
        _harness("   ")


def _prop(symbol: str, expression: str, domain: tuple[str, ...] = ()) -> Property:
    unit_id = f"ring.c::{symbol}"
    return Property(
        property_id=make_property_id(
            unit_id, PropertyKind.SEMANTIC, expression, domain
        ),
        unit_id=unit_id,
        kind=PropertyKind.SEMANTIC,
        expression=expression,
        status=PropertyStatus.CANDIDATE,
        provenance=Provenance("test", "v1"),
        domain=domain,
    )


class _Recorder(LLMClient):
    provider = "fake"
    model = "fake"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return '{"candidates": []}'


def test_the_prompt_names_the_fields_only_for_a_struct_unit() -> None:
    client = _Recorder()
    struct_sig = extract_signature(RING, "rb_push", infer_structs=True)
    propose_properties(
        ProposalRequest("ring.c::rb_push", RING, signature=struct_sig), client=client
    )
    plain = "int id(int x) { return x; }"
    propose_properties(
        ProposalRequest("u.c::id", plain, signature=extract_signature(plain, "id")),
        client=client,
    )
    with_note, without = client.prompts
    assert "`r` points to a `ring_t`; fields: buf, head, tail, count" in with_note
    assert "r->count" in with_note and "AFTER the call" in with_note
    assert "write the number" in with_note
    assert "Struct parameters" not in without


def test_prepare_proposal_request_reads_struct_fields(tmp_path: Path) -> None:
    source = tmp_path / "ring.c"
    source.write_text(RING)
    request = prepare_proposal_request(source, function="rb_push")
    assert request.signature is not None
    ring = request.signature.params[0]
    assert isinstance(ring, StructParam) and "count" in ring.fields


def test_the_shipped_examples_parse_as_struct_units() -> None:
    ring = (EXAMPLES / "ring_buffer.c").read_text()
    sha = (EXAMPLES / "sha1.c").read_text()
    push = extract_signature(ring, "rb_push", infer_structs=True)
    assert isinstance(push.params[0], StructParam)
    update = extract_signature(sha, "sha1_update", infer_structs=True)
    assert isinstance(update.params[0], StructParam)
    assert update.params[0].fields == ("state", "count", "buffer")

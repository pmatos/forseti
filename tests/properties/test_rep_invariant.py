"""Representation-invariant renderer, static gate and roles (#387); no ESBMC."""

from __future__ import annotations

import pytest

from forseti.properties import (
    REP_INVARIANT_LABEL,
    HarnessError,
    InvariantNotApplicable,
    InvariantRole,
    PropertyKind,
    PropertyStatus,
    Provenance,
    StructParam,
    build_invariant_harness,
    extract_signature,
    infer_role,
    invariant_reason,
    make_invariant,
    normalised_type_name,
    render_invariant_harness,
    valid_type_name,
)

RING = """\
#define CAP 4
typedef struct { int buf[CAP]; unsigned head, tail, count; } ring_t;
void rb_init(ring_t *r) { r->head = r->tail = r->count = 0; }
int rb_push(ring_t *r, int v) { return 1; }
int rb_pop(ring_t *r, int *out) { return 1; }
unsigned rb_count(const ring_t *r) { return r->count; }
int rb_fill(int *dst, unsigned n, ring_t *r) { return 0; }
int plain(int x) { return x; }
"""
INV = "self->count <= CAP && self->tail < CAP"


def _render(symbol: str, **kw: object) -> str:
    signature = extract_signature(RING, symbol, struct_types=("ring_t",))
    return render_invariant_harness(
        unit_source=RING,
        signature=signature,
        type_name="ring_t",
        expression=INV,
        **kw,  # ty: ignore[invalid-argument-type]
    )


def _main_body(harness: str) -> list[str]:
    return harness.split("int main(void) {\n")[1].splitlines()


def test_struct_pointer_is_not_read_as_a_buffer_length_pair() -> None:
    push = extract_signature(RING, "rb_push", struct_types=("ring_t",))
    assert isinstance(push.params[0], StructParam)
    assert push.params[0].type_name == "ring_t"
    assert not push.params[0].const
    # without the type named, the same text keeps its (buffer, length) reading
    plain = extract_signature(RING, "rb_push")
    assert not isinstance(plain.params[0], StructParam)


def test_const_pointee_and_const_pointer_are_told_apart() -> None:
    getter = extract_signature(RING, "rb_count", struct_types=("ring_t",))
    assert isinstance(getter.params[0], StructParam) and getter.params[0].const
    src = "void f(ring_t *const r) { }"
    top_const = extract_signature(src, "f", struct_types=("ring_t",))
    assert isinstance(top_const.params[0], StructParam)
    assert not top_const.params[0].const


def test_pointer_to_pointer_is_not_a_struct_param() -> None:
    sig = extract_signature(
        "void g(ring_t **r, int n) { }", "g", struct_types=("ring_t",)
    )
    assert not any(isinstance(p, StructParam) for p in sig.params)


def test_method_assumes_at_entry_and_asserts_at_exit() -> None:
    body = _main_body(_render("rb_push"))
    assume = "    __ESBMC_assume(__forseti_inv(&r));"
    call = "    rb_push(&r, v);"
    exit_assert = f'    __ESBMC_assert(__forseti_inv(&r), "{REP_INVARIANT_LABEL}");'
    assert body.index("    ring_t r = nondet_ring_t();") < body.index(assume)
    assert body.index(assume) < body.index(call) < body.index(exit_assert)
    assert "    int v = nondet_int();" in body


def test_invariant_is_one_static_predicate_over_self() -> None:
    harness = _render("rb_push")
    assert "ring_t nondet_ring_t(void);" in harness
    assert (
        "static int __forseti_inv(const ring_t *self) {\n"
        f"    return ({INV});\n}}" in harness
    )


def test_initialiser_asserts_without_assuming() -> None:
    harness = _render("rb_init", role=InvariantRole.INITIALISER)
    assert "__ESBMC_assume" not in harness
    body = _main_body(harness)
    assert body.index("    rb_init(&r);") < body.index(
        f'    __ESBMC_assert(__forseti_inv(&r), "{REP_INVARIANT_LABEL}");'
    )
    assert "role=initialiser" in harness


def test_trailing_non_struct_pointer_is_an_output_cell() -> None:
    body = _main_body(_render("rb_pop"))
    assert "    int out;" in body
    assert "    rb_pop(&r, &out);" in body


def test_buffer_beside_struct_is_allocated_filled_freed_and_capped() -> None:
    harness = _render("rb_fill", max_len=3)
    body = _main_body(harness)
    assert "    __ESBMC_assume((n >= 0 && n <= 3));" in body
    assert any("malloc((n) * sizeof(int))" in line for line in body)
    assert "    free(dst);" in body
    assert "    rb_fill(dst, n, &r);" in body
    assert "#include <stdlib.h>" in harness


def test_probe_twin_differs_only_after_the_call() -> None:
    real, probe = _render("rb_push"), _render("rb_push", probe=True)
    cut = "    rb_push(&r, v);\n"
    assert real.split(cut)[0] == probe.split(cut)[0]
    assert REP_INVARIANT_LABEL in real.split(cut)[1]
    assert REP_INVARIANT_LABEL not in probe.split(cut)[1]
    assert '__ESBMC_assert(0, "forseti:non-vacuity");' in probe


def test_method_with_only_const_pointers_is_not_applicable() -> None:
    with pytest.raises(InvariantNotApplicable, match="const ring_t"):
        _render("rb_count")


def test_initialiser_with_only_const_pointers_is_a_render_error() -> None:
    with pytest.raises(HarnessError) as excinfo:
        _render("rb_count", role=InvariantRole.INITIALISER)
    assert not isinstance(excinfo.value, InvariantNotApplicable)


def test_function_without_the_type_is_a_render_error() -> None:
    with pytest.raises(HarnessError, match="no 'ring_t' pointer parameter"):
        _render("plain")


def test_unit_defining_main_is_refused() -> None:
    signature = extract_signature(RING, "rb_push", struct_types=("ring_t",))
    with pytest.raises(HarnessError, match="defines main"):
        render_invariant_harness(
            unit_source=RING + "int main(void) { return 0; }\n",
            signature=signature,
            type_name="ring_t",
            expression=INV,
        )


def test_blank_expression_and_bad_type_name_are_refused() -> None:
    signature = extract_signature(RING, "rb_push", struct_types=("ring_t",))
    for type_name, expression in (("ring_t", "  "), ("ring t*", INV)):
        with pytest.raises(HarnessError):
            render_invariant_harness(
                unit_source=RING,
                signature=signature,
                type_name=type_name,
                expression=expression,
            )


def test_tagged_struct_type_renders() -> None:
    src = "struct node { int v; };\nvoid bump(struct node *n) { n->v++; }\n"
    harness = build_invariant_harness(
        unit_source=src, symbol="bump", type_name="struct node", expression="1 && self"
    ).source_text
    assert "struct node nondet_struct_node(void);" in harness
    assert "    struct node n = nondet_struct_node();" in harness
    assert "    bump(&n);" in harness


def test_build_defaults_role_from_the_name_and_returns_the_probe_twin() -> None:
    init = build_invariant_harness(
        unit_source=RING, symbol="rb_init", type_name="ring_t", expression=INV
    )
    assert "__ESBMC_assume" not in init.source_text
    assert init.probe_source_text is not None
    assert "forseti:non-vacuity" in init.probe_source_text
    push = build_invariant_harness(
        unit_source=RING, symbol="rb_push", type_name="ring_t", expression=INV
    )
    assert "__ESBMC_assume(__forseti_inv(&r))" in push.source_text
    forced = build_invariant_harness(
        unit_source=RING,
        symbol="rb_init",
        type_name="ring_t",
        expression=INV,
        role=InvariantRole.METHOD,
    )
    assert "__ESBMC_assume(__forseti_inv(&r))" in forced.source_text


@pytest.mark.parametrize(
    ("symbol", "role"),
    [
        ("rb_init", InvariantRole.INITIALISER),
        ("ring_initialize", InvariantRole.INITIALISER),
        ("init", InvariantRole.INITIALISER),
        ("ring_create", InvariantRole.METHOD),
        ("ring_new_slot", InvariantRole.METHOD),
        ("ring_is_new", InvariantRole.METHOD),
        ("ring_setup_done", InvariantRole.METHOD),
        ("rb_push", InvariantRole.METHOD),
        ("reinitialise_all", InvariantRole.METHOD),
        ("rb_finish", InvariantRole.METHOD),
    ],
)
def test_infer_role(symbol: str, role: InvariantRole) -> None:
    assert infer_role(symbol) is role


@pytest.mark.parametrize("name", ["ring_t", "struct node", "_T1"])
def test_valid_type_names(name: str) -> None:
    assert valid_type_name(name)


@pytest.mark.parametrize("name", ["", "ring_t *", "struct", "a b", "1x", "union u"])
def test_invalid_type_names(name: str) -> None:
    assert not valid_type_name(name)


def test_static_gate_accepts_a_pure_predicate_over_self() -> None:
    assert invariant_reason(INV) is None


@pytest.mark.parametrize(
    ("expression", "fragment"),
    [
        ("", "empty"),
        ("0", "vacuous"),
        ("CAP > 1", "vacuous"),
        ("self->count++ < CAP", "unsafe"),
        ("self->count = 0", "unsafe"),
        ("f(self) && self", "unsafe"),
        ("self->a; self->b", "unsafe"),
    ],
)
def test_static_gate_rejects(expression: str, fragment: str) -> None:
    reason = invariant_reason(expression)
    assert reason is not None and fragment in reason


def test_make_invariant_is_a_keyed_candidate_with_a_stable_content_id() -> None:
    prov = Provenance("p", "1", "hand", "human")
    a = make_invariant(type_key="f.c::ring_t", expression=INV, provenance=prov)
    b = make_invariant(type_key="f.c::ring_t", expression=INV, provenance=prov)
    other = make_invariant(type_key="g.c::ring_t", expression=INV, provenance=prov)
    assert a.kind is PropertyKind.REP_INVARIANT
    assert a.status is PropertyStatus.CANDIDATE
    assert a.unit_id == "f.c::ring_t"
    assert a.domain == ()
    assert a.property_id == b.property_id != other.property_id


def test_type_name_whitespace_is_collapsed() -> None:
    assert normalised_type_name("struct \t node ") == "struct node"
    assert normalised_type_name("ring_t") == "ring_t"

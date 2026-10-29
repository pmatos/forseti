"""Tests for `forseti.precond.synth` — signature → plan → sidecar C text.

Pure: no ESBMC, no disk. `plan_unit` classifies each parameter's L0 shape;
`render_sidecar` turns a plan into a compilable sidecar. The end-to-end verdict
behaviour (that these harnesses actually VERIFY/VIOLATE) lives with the driver
and the esbmc-gated acceptance test.
"""

from __future__ import annotations

import pytest

from forseti.esbmc.units import Param, Unit
from forseti.precond.synth import (
    NON_VACUITY_LABEL,
    RENAMED_MAIN,
    AssumedInvariant,
    ParamRole,
    SynthError,
    bind_invariants,
    plan_unit,
    pointee_type,
    render_sidecar,
)
from forseti.properties import InvariantRole


def _unit(*params: Param, name: str = "f") -> Unit:
    return Unit(name, params)


def _roles(unit: Unit) -> dict[str, ParamRole]:
    return {p.var: p.role for p in plan_unit(unit).params}


# --- classification -------------------------------------------------------


def test_scalar_pointer_is_one_fresh_object() -> None:
    unit = _unit(Param("ctx", "sha1_ctx *"))
    assert _roles(unit) == {"ctx": ParamRole.SCALAR_PTR}


def test_restrict_qualified_concrete_pointer_still_materialisable() -> None:
    # Scrubbing `restrict` to catch `void *restrict` must not demote a concrete
    # pointee to UNRESOLVED — `uint8_t *restrict` is still one fresh object.
    unit = _unit(Param("p", "const uint8_t *restrict"))
    assert _roles(unit) == {"p": ParamRole.SCALAR_PTR}


def test_atomic_pointee_still_materialisable() -> None:
    # Unwrapping `_Atomic(T)` to catch `_Atomic(void *)` must not demote a
    # concrete pointee either — `_Atomic int *p` is still one fresh object.
    unit = _unit(Param("p", "_Atomic(int) *"))
    assert _roles(unit) == {"p": ParamRole.SCALAR_PTR}


def test_byte_length_pairing() -> None:
    unit = _unit(Param("data", "const uint8_t *"), Param("len", "unsigned long"))
    plan = plan_unit(unit)
    roles = {p.var: p.role for p in plan.params}
    assert roles == {"data": ParamRole.PTR_BYTE_LEN, "len": ParamRole.LENGTH}
    assert plan.params[0].length_var == "len"


def test_element_count_pairing() -> None:
    unit = _unit(Param("buf", "int *"), Param("count", "int"))
    plan = plan_unit(unit)
    roles = {p.var: p.role for p in plan.params}
    assert roles == {"buf": ParamRole.PTR_ELEM_COUNT, "count": ParamRole.LENGTH}
    assert plan.params[0].length_var == "count"


def test_fixed_array_extent_wins() -> None:
    unit = _unit(Param("digest", "uint8_t *", array_extent=20))
    plan = plan_unit(unit)
    assert plan.params[0].role is ParamRole.FIXED_ARRAY
    assert plan.params[0].extent == 20


def test_unreadable_fixed_array_extent_is_unresolved_not_one_element() -> None:
    # Issue #137: `uint8_t digest[SHA_DIGEST_LENGTH]` reaches us as `uint8_t *` with
    # an unreadable extent. Sizing it `sizeof(*digest)` would phantom-VIOLATE a unit
    # that writes all 20 bytes, so L0 declines and the driver says NEEDS_CONTRACT.
    unit = _unit(Param("digest", "uint8_t *", array_extent_unresolved=True))
    plan = plan_unit(unit)
    assert plan.params[0].role is ParamRole.UNRESOLVED
    assert not plan.resolvable
    assert plan.unresolved_params == ("digest",)


def test_unreadable_extent_still_prefers_an_accompanying_length() -> None:
    # A conventional `T buf[MACRO]` extent only displaces the one-element fallback:
    # a following length sizes the object *exactly*, which beats declining. The
    # written extent is documentation — C adjusts the parameter to `T *` and binds
    # nobody — so the length is the better authority.
    unit = _unit(
        Param("buf", "uint8_t *", array_extent_unresolved=True),
        Param("len", "unsigned long"),
    )
    plan = plan_unit(unit)
    assert _roles(unit) == {"buf": ParamRole.PTR_BYTE_LEN, "len": ParamRole.LENGTH}
    assert plan.resolvable


def test_unreadable_static_minimum_outranks_an_accompanying_length() -> None:
    # `T buf[static MACRO]` *does* bind the caller to at least MACRO elements, so
    # the function may touch all of them whatever `len` says. Sizing by `len` could
    # under-allocate and phantom-VIOLATE valid code, so L0 declines instead.
    unit = _unit(
        Param("buf", "uint8_t *", array_extent_unresolved=True, array_static_min=True),
        Param("len", "unsigned long"),
    )
    plan = plan_unit(unit)
    assert plan.params[0].role is ParamRole.UNRESOLVED
    assert not plan.resolvable
    assert plan.unresolved_params == ("buf",)


def test_readable_static_minimum_floors_an_accompanying_length() -> None:
    # A *readable* `[static 20]` is a caller obligation, not a capacity: a valid
    # caller may pass a far larger buffer with `len` to match. Sizing the object at
    # exactly 20 while `len` roams free phantom-VIOLATES a body that reads `len`
    # elements, so the length sizes the object with 20 as its floor.
    unit = _unit(
        Param("buf", "uint8_t *", array_extent=20, array_static_min=True),
        Param("len", "unsigned long"),
    )
    plan = plan_unit(unit)
    assert plan.params[0].role is ParamRole.PTR_BYTE_LEN
    assert plan.params[0].length_var == "len"
    assert plan.params[0].static_min_extent == 20
    assert plan.params[1].role is ParamRole.LENGTH


def test_readable_static_minimum_without_a_length_is_its_extent() -> None:
    # With no length to pair with, the weakest valid caller supplies exactly the
    # declared minimum — the plain fixed-array shape.
    unit = _unit(Param("buf", "uint8_t *", array_extent=20, array_static_min=True))
    plan = plan_unit(unit)
    assert plan.params[0].role is ParamRole.FIXED_ARRAY
    assert plan.params[0].extent == 20
    assert plan.params[0].static_min_extent is None


def test_conventional_extent_defers_to_an_accompanying_length() -> None:
    # Issue #147: without `static` the bracket binds nobody (C adjusts the
    # parameter to `T *`), so an accompanying length is the better authority —
    # unlike `[static N]` (issue #137), there is no caller obligation to floor
    # against, so `N` is dropped entirely rather than sizing `max(length, N)`.
    unit = _unit(
        Param("buf", "uint8_t *", array_extent=20),
        Param("len", "unsigned long"),
    )
    plan = plan_unit(unit)
    assert plan.params[0].role is ParamRole.PTR_BYTE_LEN
    assert plan.params[0].length_var == "len"
    assert plan.params[0].static_min_extent is None
    assert plan.params[1].role is ParamRole.LENGTH


def test_conventional_extent_wins_with_no_accompanying_length() -> None:
    # With nothing to pair with, the written extent is the only size the
    # signature states, so it still backs the object (unchanged from #134).
    unit = _unit(Param("buf", "uint8_t *", array_extent=20), Param("flag", "int"))
    plan = plan_unit(unit)
    assert plan.params[0].role is ParamRole.FIXED_ARRAY
    assert plan.params[0].extent == 20
    assert plan.params[1].role is ParamRole.SCALAR


def test_non_length_named_next_param_is_not_paired() -> None:
    # A trailing integer that is not length-named leaves the pointer a fresh
    # single object and the integer a plain scalar.
    unit = _unit(Param("p", "int *"), Param("flag", "int"))
    assert _roles(unit) == {"p": ParamRole.SCALAR_PTR, "flag": ParamRole.SCALAR}


@pytest.mark.parametrize(
    "type_str",
    [
        "void *",  # no pointee size
        "const void *",
        "void *restrict",  # restrict qualifier must not hide the void pointee
        "const void *restrict",
        "void *__restrict",  # GCC spelling
        # C11 `void *_Atomic p`, which clang prints in its functional spelling —
        # the pointee is still `void`, so it must not read as a sizeable object.
        "_Atomic(void *)",
        "_Atomic(const void *)",
        "int **",  # multi-level: one fresh T* still dangles
        "void (*)(void)",  # function pointer
        "int (*)[10]",  # pointer to array
    ],
)
def test_unresolved_pointer_shapes(type_str: str) -> None:
    plan = plan_unit(_unit(Param("p", type_str)))
    assert plan.params[0].role is ParamRole.UNRESOLVED
    assert not plan.resolvable
    assert plan.unresolved_params == ("p",)


def test_sha1_update_signature() -> None:
    unit = _unit(
        Param("ctx", "sha1_ctx *"),
        Param("data", "const uint8_t *"),
        Param("len", "unsigned long"),
        name="sha1_update",
    )
    assert _roles(unit) == {
        "ctx": ParamRole.SCALAR_PTR,
        "data": ParamRole.PTR_BYTE_LEN,
        "len": ParamRole.LENGTH,
    }


def test_unnamed_pointer_param_gets_argN_var() -> None:
    plan = plan_unit(_unit(Param("", "sha1_ctx *")))
    assert plan.params[0].var == "arg0"


# --- rendering ------------------------------------------------------------


def test_render_includes_source_and_declares_main() -> None:
    unit = _unit(Param("ctx", "sha1_ctx *"), name="sha1_init")
    text = render_sidecar(plan_unit(unit), "/abs/sha1.c")
    assert '#include "/abs/sha1.c"' in text
    assert "#include <stdlib.h>" in text
    assert "int main(void) {" in text
    assert "sha1_init(ctx);" in text
    assert "sha1_ctx * ctx = malloc(sizeof(*ctx));" in text


def test_render_renames_the_sources_main_only_around_its_include() -> None:
    # The source may define `main`; the sidecar's own must not collide with it. The
    # rename is scoped to the `#include` so the harness's `int main` is its own.
    text = render_sidecar(plan_unit(_unit(Param("ctx", "ctx *"))), "/abs/prog.c")
    lines = text.splitlines()
    assert lines[:3] == [
        f"#define main {RENAMED_MAIN}",
        '#include "/abs/prog.c"',
        "#undef main",
    ]
    assert lines.count("int main(void) {") == 1


def test_render_calls_a_unit_that_is_main_under_its_renamed_name() -> None:
    unit = _unit(Param("argv", "char *"), name="main")
    text = render_sidecar(plan_unit(unit), "/abs/prog.c")
    assert f"{RENAMED_MAIN}(argv);" in text
    assert "    main(argv);" not in text


def test_render_symbolic_length_is_bounded_and_exact() -> None:
    unit = _unit(
        Param("ctx", "sha1_ctx *"),
        Param("data", "const uint8_t *"),
        Param("len", "unsigned long"),
        name="sha1_update",
    )
    text = render_sidecar(plan_unit(unit), "s.c", max_len=8)
    # length is declared, bounded, and used to size the object exactly (bytes).
    assert "unsigned long len = nondet_unsigned_long();" in text
    assert "__ESBMC_assume(len <= 8);" in text
    assert "const uint8_t * data = malloc((size_t)len);" in text
    # length declared before the pointer that consumes it.
    assert text.index("len = nondet") < text.index("data = malloc")
    assert "sha1_update(ctx, data, len);" in text


def test_render_signed_length_gets_lower_bound() -> None:
    unit = _unit(Param("buf", "char *"), Param("len", "int"), name="g")
    text = render_sidecar(plan_unit(unit), "s.c", max_len=4)
    assert "__ESBMC_assume(len >= 0 && len <= 4);" in text


def test_render_element_count_scales_by_sizeof() -> None:
    unit = _unit(Param("buf", "int *"), Param("count", "int"), name="g")
    text = render_sidecar(plan_unit(unit), "s.c")
    assert "int * buf = malloc((size_t)count * sizeof(*buf));" in text


def test_render_fixed_array_scales_by_extent() -> None:
    unit = _unit(Param("digest", "uint8_t *", array_extent=20), name="sha1_final")
    text = render_sidecar(plan_unit(unit), "s.c")
    assert "uint8_t * digest = malloc((size_t)20 * sizeof(*digest));" in text


def test_render_conventional_extent_with_length_sizes_by_length_alone() -> None:
    # Issue #147: `uint8_t p[16], size_t len` sizes by `len` alone — no `max(len,
    # 16)` floor, since a conventional (non-`static`) extent binds nobody.
    unit = _unit(
        Param("p", "uint8_t *", array_extent=16),
        Param("len", "unsigned long"),
        name="g",
    )
    text = render_sidecar(plan_unit(unit), "s.c", max_len=8)
    assert "uint8_t * p = malloc((size_t)len);" in text


def test_render_static_minimum_floors_a_byte_length() -> None:
    # `uint8_t p[static 20], size_t len` — bytes on both sides, so the floor is
    # `20 * sizeof(*p)`; the object is whichever of the two is larger.
    unit = _unit(
        Param("p", "uint8_t *", array_extent=20, array_static_min=True),
        Param("len", "unsigned long"),
        name="g",
    )
    text = render_sidecar(plan_unit(unit), "s.c", max_len=8)
    assert (
        "uint8_t * p = malloc(((size_t)len > (size_t)20 * sizeof(*p) "
        "? (size_t)len : (size_t)20 * sizeof(*p)));" in text
    )
    # the length is still symbolic and bounded — the floor does not pin it.
    assert "__ESBMC_assume(len <= 8);" in text


def test_render_static_minimum_floors_an_element_count() -> None:
    # An element count meets the floor in elements, before scaling by `sizeof`.
    unit = _unit(
        Param("p", "int *", array_extent=4, array_static_min=True),
        Param("count", "int"),
        name="g",
    )
    text = render_sidecar(plan_unit(unit), "s.c")
    assert (
        "int * p = malloc(((size_t)count > (size_t)4 ? (size_t)count : (size_t)4) "
        "* sizeof(*p));" in text
    )


def test_render_non_vacuity_appends_assert_after_call() -> None:
    unit = _unit(Param("ctx", "sha1_ctx *"), name="sha1_init")
    text = render_sidecar(plan_unit(unit), "s.c", non_vacuity=True)
    assert f'__ESBMC_assert(0, "{NON_VACUITY_LABEL}");' in text
    assert text.index("sha1_init(ctx);") < text.index("__ESBMC_assert")


def test_render_refuses_unresolved_unit() -> None:
    with pytest.raises(SynthError, match="unresolved"):
        render_sidecar(plan_unit(_unit(Param("p", "void *"))), "s.c")


def test_render_one_nondet_prototype_per_distinct_type() -> None:
    unit = _unit(
        Param("a", "int *"),  # next is `m` (not length-named) → fresh single object
        Param("m", "int"),  # plain scalar
        Param("n", "int"),  # length-named but no pointer precedes it → plain scalar
        name="g",
    )
    plan = plan_unit(unit)
    assert plan.params[0].role is ParamRole.SCALAR_PTR
    text = render_sidecar(plan, "s.c")
    # exactly one `extern int nondet_int(void);` even though two int scalars.
    assert text.count("extern int nondet_int(void);") == 1


# --- accepted representation invariants (#387) ----------------------------


@pytest.mark.parametrize(
    ("type_str", "expected"),
    [
        ("ring_t *", "ring_t"),
        ("const ring_t *", "ring_t"),
        ("ring_t *const", "ring_t"),
        ("volatile  ring_t *restrict", "ring_t"),
        ("struct  node *", "struct node"),
        ("const struct node *", "struct node"),
        ("_Atomic(ring_t) *", "ring_t"),
        ("ring_t **", None),
        ("ring_t", None),
        ("void (*)(int)", None),
        ("*", None),
    ],
)
def test_pointee_type_names_the_struct_a_single_pointer_points_to(
    type_str: str, expected: str | None
) -> None:
    assert pointee_type(type_str) == expected


def _ring_push() -> Unit:
    return _unit(Param("r", "ring_t *"), Param("v", "int"), name="rb_push")


def test_bind_invariants_attaches_each_types_expressions_to_its_pointers() -> None:
    plan = bind_invariants(
        plan_unit(_ring_push()), {"ring_t": ("self->count <= CAP",), "other_t": ("1",)}
    )
    assert plan.invariants == (
        AssumedInvariant("ring_t", ("self->count <= CAP",), ("r",)),
    )


def test_bind_invariants_without_a_matching_type_returns_the_plan_unchanged() -> None:
    plan = plan_unit(_ring_push())
    assert bind_invariants(plan, {}) is plan
    assert bind_invariants(plan, {"other_t": ("1",)}) is plan
    assert bind_invariants(plan, {"ring_t": ()}) is plan


def test_bind_invariants_skips_buffers_and_unresolved_pointers() -> None:
    sized = _unit(Param("rs", "ring_t *"), Param("n", "unsigned long"), name="f")
    fixed = _unit(Param("rs", "ring_t *", array_extent=4), name="g")
    plan_sized, plan_fixed = plan_unit(sized), plan_unit(fixed)
    assert bind_invariants(plan_sized, {"ring_t": ("1",)}) is plan_sized
    assert bind_invariants(plan_fixed, {"ring_t": ("1",)}) is plan_fixed


def test_bind_invariants_covers_every_pointer_to_the_type() -> None:
    unit = _unit(Param("dst", "ring_t *"), Param("src", "const ring_t *"), name="copy")
    (inv,) = bind_invariants(plan_unit(unit), {"ring_t": ("a", "b")}).invariants
    assert inv.params == ("dst", "src")
    assert inv.expressions == ("a", "b")


def test_render_assumes_the_invariant_after_materialising_and_before_the_call() -> None:
    plan = bind_invariants(
        plan_unit(_ring_push()), {"ring_t": ("self->count <= CAP", "self->tail < CAP")}
    )
    text = render_sidecar(plan, "/abs/ring.c")
    helper = "static int __forseti_inv_ring_t(const ring_t *self) {"
    assert helper in text
    assert "return (self->count <= CAP) && (self->tail < CAP);" in text
    alloc = text.index("ring_t * r = malloc(sizeof(*r));")
    assume = text.index("__ESBMC_assume(__forseti_inv_ring_t(r));")
    call = text.index("rb_push(r, v);")
    assert (
        text.index('#include "/abs/ring.c"')
        < text.index(helper)
        < text.index("int main(void) {")
    )
    assert alloc < assume < call


def test_render_names_a_tagged_structs_helper_with_an_identifier() -> None:
    unit = _unit(Param("n", "struct node *"), name="bump")
    text = render_sidecar(
        bind_invariants(plan_unit(unit), {"struct node": ("self->v >= 0",)}), "s.c"
    )
    assert "static int __forseti_inv_struct_node(const struct node *self) {" in text


def test_render_without_invariants_is_unchanged() -> None:
    plan = plan_unit(_ring_push())
    assert plan.invariants == ()
    text = render_sidecar(plan, "/abs/ring.c")
    assert "__forseti_inv" not in text
    assert "__ESBMC_assume" not in text


def test_the_non_vacuity_probe_keeps_the_assumption() -> None:
    plan = bind_invariants(plan_unit(_ring_push()), {"ring_t": ("0 == self->count",)})
    text = render_sidecar(plan, "s.c", non_vacuity=True)
    assert text.index("__ESBMC_assume(__forseti_inv_ring_t(r));") < text.index(
        NON_VACUITY_LABEL
    )


def test_bind_invariants_binds_nothing_for_an_initialiser() -> None:
    init = plan_unit(_unit(Param("r", "ring_t *"), name="rb_init"))
    assert bind_invariants(init, {"ring_t": ("1",)}) is init


def test_bind_invariants_role_overrides_the_name() -> None:
    init = plan_unit(_unit(Param("r", "ring_t *"), name="rb_init"))
    method = plan_unit(_unit(Param("r", "ring_t *"), name="ring_new"))
    assert bind_invariants(
        init, {"ring_t": ("1",)}, role=InvariantRole.METHOD
    ).invariants
    assert (
        bind_invariants(method, {"ring_t": ("1",)}, role=InvariantRole.INITIALISER)
        is method
    )
    assert bind_invariants(method, {"ring_t": ("1",)}).invariants  # not inferred


def test_bind_invariants_accepts_none() -> None:
    plan = plan_unit(_ring_push())
    assert bind_invariants(plan, None) is plan

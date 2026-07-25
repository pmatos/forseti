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
    ParamRole,
    SynthError,
    plan_unit,
    render_sidecar,
)


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


def test_conventional_extent_still_outranks_an_accompanying_length() -> None:
    # Without `static` the bracket binds nobody, so #134's rule is unchanged here:
    # the written extent is the only size the signature states and the neighbouring
    # length stays a plain scalar. That leaves its own phantom for `len > N` —
    # pre-existing, tracked in #147, and deliberately not changed by this fix.
    unit = _unit(
        Param("buf", "uint8_t *", array_extent=20),
        Param("len", "unsigned long"),
    )
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
    assert text.startswith('#include "/abs/sha1.c"')
    assert "#include <stdlib.h>" in text
    assert "int main(void) {" in text
    assert "sha1_init(ctx);" in text
    assert "sha1_ctx * ctx = malloc(sizeof(*ctx));" in text


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

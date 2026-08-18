"""Tests for the S3 obligation renderers in `forseti.precond.synth` (no ESBMC).

`obligation_expr` and `inject_obligations` are pure: plan in, C text out. What
matters here is that the *check* a caller must satisfy is the same predicate the
S2 sidecar *assumed* (same size expression, same parameter), that injecting it
leaves the rest of the translation unit — and its line numbering — untouched,
and that a plan L0 cannot name is declined rather than injected wrongly.
"""

from __future__ import annotations

import pytest

from forseti.esbmc.units import Param, Unit
from forseti.precond.synth import (
    OBLIGATION_LABEL_PREFIX,
    OBLIGATION_SITE_LABEL_PREFIX,
    SynthError,
    inject_obligations,
    obligation_expr,
    plan_unit,
)

SUM_BYTES = Unit(
    "sum_bytes",
    (Param("buf", "const unsigned char *"), Param("len", "unsigned long")),
)

SOURCE = """\
#include <stddef.h>

/* sums bytes { not a definition brace } */
unsigned sum_bytes(const unsigned char *buf, size_t len) {
    unsigned acc = 0;
    for (size_t i = 0; i < len; i++) acc += buf[i];
    return acc;
}

unsigned caller(const unsigned char *frame, size_t len) {
    return sum_bytes(frame, len);
}
"""


def _plan(unit: Unit = SUM_BYTES):  # noqa: ANN202
    return plan_unit(unit)


def test_obligation_names_the_pointer_and_its_synthesised_size() -> None:
    plan = _plan()
    expr = obligation_expr(plan.pointer_params[0])
    assert "__ESBMC_r_ok" in expr
    assert "buf" in expr
    # the same size the sidecar would `malloc` — a byte length, not an element count
    assert "(size_t)len" in expr


def test_obligation_rebases_and_guards_against_wraparound() -> None:
    # Both guards are load-bearing on esbmc 8.3.0: `r_ok` answers *true* for a
    # pointer already past its object, so the check is rebased to offset zero,
    # and a caller's underflowed length must not wrap the span down to a small
    # (satisfiable) one. See `obligation_expr`'s docstring.
    expr = obligation_expr(_plan().pointer_params[0])
    assert "__ESBMC_POINTER_OFFSET" in expr
    assert ">= 0" in expr
    assert expr.count("__ESBMC_r_ok") == 1


def test_fixed_array_obligation_uses_the_extent() -> None:
    unit = Unit("digest", (Param("out", "unsigned char *", array_extent=20),))
    expr = obligation_expr(plan_unit(unit).pointer_params[0])
    assert "(size_t)20 * sizeof(*out)" in expr


def test_element_count_obligation_guards_its_own_multiplication() -> None:
    # `_pointer_alloc` multiplies a caller-controlled count by `sizeof(*p)` for
    # this role alone — a count near `SIZE_MAX / sizeof(*p)` would otherwise wrap
    # the product to a small `size` before the offset/wraparound guards ever see
    # it, letting `r_ok` accept an undersized object.
    unit = Unit("fill", (Param("p", "int *"), Param("count", "size_t")))
    expr = obligation_expr(plan_unit(unit).pointer_params[0])
    assert "((size_t)count) <= (__SIZE_TYPE__)-1 / sizeof(*p)" in expr
    # placed first, so a short-circuit never lets the wrapped product decide it
    assert expr.index("(__SIZE_TYPE__)-1") < expr.index("__ESBMC_POINTER_OFFSET")


def test_byte_length_and_fixed_array_obligations_add_no_count_guard() -> None:
    # Neither multiplicand is caller-controlled: `PTR_BYTE_LEN` never multiplies
    # at all, and `FIXED_ARRAY`'s extent comes from the signature itself.
    assert "(__SIZE_TYPE__)-1" not in obligation_expr(_plan().pointer_params[0])
    unit = Unit("digest", (Param("out", "unsigned char *", array_extent=20),))
    assert "(__SIZE_TYPE__)-1" not in obligation_expr(plan_unit(unit).pointer_params[0])


def test_injection_adds_one_labelled_assert_per_pointer() -> None:
    injected = inject_obligations(SOURCE, _plan())
    assert f'"{OBLIGATION_LABEL_PREFIX}sum_bytes:buf"' in injected
    assert injected.count("__ESBMC_assert") == 1


def test_injection_preserves_every_other_line() -> None:
    injected = inject_obligations(SOURCE, _plan())
    original, copy = SOURCE.splitlines(), injected.splitlines()
    assert len(original) == len(copy)
    differing = [
        i for i, (a, b) in enumerate(zip(original, copy, strict=True)) if a != b
    ]
    # exactly the definition's own line changed, and only by an appended assert
    assert differing == [3]
    assert copy[3].startswith(original[3])


def test_injection_ignores_a_brace_inside_a_comment() -> None:
    # The `{` in the doc comment above the definition must not be mistaken for
    # the body's — masking comments before locating it is what prevents that.
    injected = inject_obligations(SOURCE, _plan())
    assert injected.splitlines()[2] == SOURCE.splitlines()[2]


def test_site_probe_replaces_the_obligations_with_a_reachability_assert() -> None:
    injected = inject_obligations(SOURCE, _plan(), site_probe=True)
    assert f'__ESBMC_assert(0, "{OBLIGATION_SITE_LABEL_PREFIX}sum_bytes");' in injected
    assert "__ESBMC_r_ok" not in injected


def test_a_source_path_prepends_a_line_directive() -> None:
    # The copy is written to disk under its own path, not the source's — without
    # this, `__FILE__` inside it would report *that* path instead. `#line`
    # renumbers everything after it, so nothing but this one new line changes.
    without = inject_obligations(SOURCE, _plan())
    with_path = inject_obligations(SOURCE, _plan(), source_path="/orig/frame.c")
    lines = with_path.splitlines()
    assert lines[0] == '#line 1 "/orig/frame.c"'
    assert lines[1:] == without.splitlines()


def test_a_source_path_escapes_quotes_and_backslashes() -> None:
    # Either character would otherwise close the directive's string literal
    # early or corrupt the path it names.
    quoted = inject_obligations(SOURCE, _plan(), source_path='weird"path.c')
    assert quoted.splitlines()[0] == '#line 1 "weird\\"path.c"'
    backslashed = inject_obligations(SOURCE, _plan(), source_path=r"C:\path.c")
    assert backslashed.splitlines()[0] == '#line 1 "C:\\\\path.c"'


_IF0_DUP = """\
#if 0
void f(int *p) { /* never compiled */ }
#endif
void f(int *p) { *p = 0; }
"""


def test_injection_targets_the_compiled_definition_not_a_dead_if0_body() -> None:
    # Issue #145 on the S3 obligation path: an inactive `#if 0` definition ahead
    # of the one clang compiled must not capture the injection point. The plan
    # anchors on `unit.def_line` (the compiled definition's presumed line, line 4
    # here); without it the obligation lands after the dead brace, cpp deletes it,
    # and a caller passing an invalid/too-small pointer would be reported
    # DISCHARGED against a callee carrying no obligation at all (a silent pass).
    unit = Unit("f", (Param("p", "int *"),), def_line=4)
    lines = inject_obligations(_IF0_DUP, plan_unit(unit)).splitlines()
    assert lines.count(lines[3]) == 1  # sanity: distinct live/dead body lines
    assert "__ESBMC_assert" in lines[3]  # rides the live body (line 4)
    assert "__ESBMC_assert" not in lines[1]  # never the dead `#if 0` body (line 2)
    assert f'"{OBLIGATION_LABEL_PREFIX}f:p"' in lines[3]


def test_unresolved_plan_is_declined() -> None:
    plan = plan_unit(Unit("f", (Param("p", "void *"),)))
    with pytest.raises(SynthError, match="unresolved"):
        inject_obligations("void f(void *p) {}\n", plan)


def test_unnamed_pointer_is_declined_rather_than_injected() -> None:
    # The sidecar can call it `arg0` because the sidecar *declares* it; the
    # callee's body cannot, so there is nothing to write an obligation about.
    plan = plan_unit(Unit("f", (Param("", "int *"),)))
    with pytest.raises(SynthError, match="unnamed"):
        inject_obligations("void f(int *) {}\n", plan)


def test_missing_definition_is_declined() -> None:
    with pytest.raises(SynthError, match="no definition"):
        inject_obligations(
            "unsigned sum_bytes(const unsigned char *, size_t);\n", _plan()
        )

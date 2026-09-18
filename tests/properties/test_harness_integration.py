"""End-to-end: render a harness, then run the real esbmc on it.

Skipped automatically when esbmc is not on PATH, so the unit suite stays
self-contained. These prove the writer's output is genuinely checkable -- a true
property VERIFIES, a false one is VIOLATED with the expected counterexample, and
the true case is non-vacuous (the assert site is reachable).
"""

from __future__ import annotations

import shutil
from functools import partial
from pathlib import Path

import pytest

from forseti.esbmc import (
    EsbmcResult,
    Unknown,
    UnknownReason,
    Verified,
    Violated,
    verify,
)
from forseti.precond.run import escalating_port
from forseti.properties import (
    BufferParam,
    ScalarParam,
    SemanticSpec,
    UnitSignature,
    render_semantic_harness,
)

pytestmark = pytest.mark.skipif(
    shutil.which("esbmc") is None, reason="esbmc binary not on PATH"
)

# The acceptance kernel: my_abs(INT64_MIN) returns INT64_MIN (still negative),
# because -INT64_MIN is not representable. The precondition x > INT64_MIN is what
# makes "result >= 0" hold; dropping it exposes the violation.
ABS_SLICE = "int64_t my_abs(int64_t x) { return (x < 0) ? -x : x; }"
ABS_SIG = UnitSignature("my_abs", "int64_t", (ScalarParam("int64_t", "x"),))

# INT64_MIN as esbmc's two's-complement bit string (top bit set, 63 zeros); we
# pin the counterexample input by this pattern, not the decimal literal, which
# esbmc 8.3.0 renders as "-9223372036854775807 - 1".
_INT64_MIN_BITS = "1" + "0" * 63


def _render(spec: SemanticSpec, tmp_path: Path) -> Path:
    source = tmp_path / "harness.c"
    source.write_text(
        render_semantic_harness(unit_source=ABS_SLICE, signature=ABS_SIG, spec=spec)
    )
    return source


def test_true_property_verifies(tmp_path: Path) -> None:
    source = _render(SemanticSpec("result >= 0", ("x > INT64_MIN",)), tmp_path)
    assert isinstance(verify(source, unwind=1), Verified)


def test_false_property_is_violated(tmp_path: Path) -> None:
    # Same postcondition, but with the domain precondition removed.
    source = _render(SemanticSpec("result >= 0"), tmp_path)
    result = verify(source, unwind=1)
    assert isinstance(result, Violated)
    cex = result.counterexample
    assert cex is not None
    assert any(
        a.binary is not None and a.binary.replace(" ", "") == _INT64_MIN_BITS
        for a in cex.inputs
    )


def test_buffer_content_precondition_renders_valid_c(tmp_path: Path) -> None:
    # A domain precondition over buffer *contents* must be emitted after the
    # buffer is declared/filled. If it leaks before the allocation the harness
    # references an undeclared identifier and esbmc returns a parse Error (not
    # a verdict) -- so a clean Verified proves the C is well-formed.
    source = tmp_path / "buffer.c"
    source.write_text(
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
            spec=SemanticSpec("result == a[0]", ("n >= 1 && n <= 2", "a[0] >= 0")),
        )
    )
    assert isinstance(verify(source, unwind=2), Verified)


def _render_unconstrained_length_buffer(length_ctype: str, tmp_path: Path) -> Path:
    """A tautologically-true property over a `(ptr, length_ctype)` buffer with
    no domain entry bounding the length -- the shared shape #297's regression
    tests check against, varying only the length param's width."""
    source = tmp_path / "unconstrained.c"
    source.write_text(
        render_semantic_harness(
            unit_source=f"int first(const int *a, {length_ctype} n) {{ return 0; }}",
            signature=UnitSignature(
                "first",
                "int",
                (
                    BufferParam("int", "a", "n", const=True),
                    ScalarParam(length_ctype, "n"),
                ),
            ),
            spec=SemanticSpec("result == result"),
        )
    )
    return source


def test_unconstrained_length_buffer_verifies(tmp_path: Path) -> None:
    # #297: len == 0 is reachable with no domain bound. The buffer used to be
    # a raw stack VLA, and ESBMC treats a zero-size VLA as its own violation
    # independent of the checked property -- every such property spuriously
    # VIOLATED regardless of whether it actually held.
    source = _render_unconstrained_length_buffer("unsigned", tmp_path)
    assert isinstance(verify(source, unwind=2), Verified)


def test_unconstrained_length_buffer_is_freed(tmp_path: Path) -> None:
    # Follow-up to #297/#298: the malloc'd buffer must be `free`d, or a
    # leak-checking run reports every buffer-bearing property VIOLATED
    # regardless of the postcondition -- the same "harness artifact fails
    # independent of the property" failure class as the zero-length VLA, just
    # relocated from the allocation itself to a forgotten one.
    source = _render_unconstrained_length_buffer("unsigned", tmp_path)
    result = verify(source, unwind=2, extra_flags=("--memory-leak-check",))
    assert isinstance(result, Verified)


def test_wide_unconstrained_length_does_not_overflow_the_allocation(
    tmp_path: Path,
) -> None:
    # A `size_t`-typed length is exactly as wide as `size_t` itself, so
    # `length * sizeof(elem_ctype)` can wrap to a small value when `length` is
    # domain-unconstrained: `malloc` would then succeed with a too-small object
    # and the fill loop writes past it -- VIOLATED independent of the
    # postcondition, the same failure class as #297 relocated into the
    # allocation's own size arithmetic. The overflow guard in `_render_buffer`
    # must keep this sound. (A 32-bit `unsigned` length, as in the test above,
    # cannot wrap a 64-bit `size_t` multiply, which is why that case alone
    # never surfaced this.)
    source = _render_unconstrained_length_buffer("size_t", tmp_path)
    assert isinstance(verify(source, unwind=2), Verified)


def test_true_property_is_non_vacuous(tmp_path: Path) -> None:
    # An always-false postcondition under the true-case precondition must be
    # VIOLATED: that proves the assert site is reachable, so the true case above
    # was a real pass and not a vacuous one.
    source = _render(SemanticSpec("0", ("x > INT64_MIN",)), tmp_path)
    assert isinstance(verify(source, unwind=1), Violated)


# --- default length cap (#299) ------------------------------------------------

_COUNT_ODD_SLICE = (
    "int count_odd(const unsigned char *a, unsigned n) {\n"
    "    int s = 0;\n"
    "    for (unsigned i = 0; i < n; i++) s += a[i] & 1;\n"
    "    return s;\n"
    "}\n"
)
_COUNT_ODD_SIG = UnitSignature(
    "count_odd",
    "int",
    (BufferParam("unsigned char", "a", "n", const=True), ScalarParam("unsigned", "n")),
)


def _count_odd_verdict(
    postcondition: str, tmp_path: Path, *, max_len: int | None, unwind: int
) -> EsbmcResult:
    """The verdict `check_source` would reach for one property over `count_odd`:
    unwinding assertions ON, wrapped in `escalating_port` (so an under-unwound
    loop is `Unknown`, not a bare `Violated`)."""
    source = tmp_path / "count_odd.c"
    source.write_text(
        render_semantic_harness(
            unit_source=_COUNT_ODD_SLICE,
            signature=_COUNT_ODD_SIG,
            spec=SemanticSpec(postcondition),
            max_len=max_len,
        )
    )
    port = escalating_port(partial(verify, no_unwinding_assertions=False))
    return port(source, unwind=unwind)


def test_uncapped_unconstrained_length_is_unknown_at_any_unwind(
    tmp_path: Path,
) -> None:
    # The #299 ceiling: with no domain bound the fill loop and the unit's own
    # loop run a symbolic number of times, so even a generous unwind cannot
    # settle a genuinely true property.
    result = _count_odd_verdict(
        "result >= 0 && (unsigned)result <= n", tmp_path, max_len=None, unwind=64
    )
    assert isinstance(result, Unknown)
    assert result.reason is UnknownReason.UNDER_UNWOUND


def test_capped_unconstrained_length_settles_a_true_property(tmp_path: Path) -> None:
    # The fix: with `len <= 8` a genuinely true property over an unconstrained
    # `(ptr, len)` buffer settles to a real verdict instead of UNKNOWN.
    result = _count_odd_verdict(
        "result >= 0 && (unsigned)result <= n", tmp_path, max_len=8, unwind=16
    )
    assert isinstance(result, Verified)


def test_capped_unconstrained_length_settles_a_false_property(tmp_path: Path) -> None:
    result = _count_odd_verdict("result < 0", tmp_path, max_len=8, unwind=16)
    assert isinstance(result, Violated)


def test_cap_below_the_unwind_is_needed_to_settle(tmp_path: Path) -> None:
    # A fill loop over up to N elements needs k > N: at k == max_len the honest
    # verdict is still UNKNOWN (the ladder's job to climb), never a false HELD.
    result = _count_odd_verdict(
        "result >= 0 && (unsigned)result <= n", tmp_path, max_len=8, unwind=8
    )
    assert isinstance(result, Unknown)

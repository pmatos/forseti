"""End-to-end: render a harness, then run the real esbmc on it.

Skipped automatically when esbmc is not on PATH, so the unit suite stays
self-contained. These prove the writer's output is genuinely checkable -- a true
property VERIFIES, a false one is VIOLATED with the expected counterexample, and
the true case is non-vacuous (the assert site is reachable).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from forseti.esbmc import Verified, Violated, verify
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
    # buffer is declared/filled. If it leaks before the VLA declaration the
    # harness references an undeclared identifier and esbmc returns a parse
    # Error (not a verdict) -- so a clean Verified proves the C is well-formed.
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


def test_true_property_is_non_vacuous(tmp_path: Path) -> None:
    # An always-false postcondition under the true-case precondition must be
    # VIOLATED: that proves the assert site is reachable, so the true case above
    # was a real pass and not a vacuous one.
    source = _render(SemanticSpec("0", ("x > INT64_MIN",)), tmp_path)
    assert isinstance(verify(source, unwind=1), Violated)

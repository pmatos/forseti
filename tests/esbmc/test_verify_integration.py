"""End-to-end tests that run the real esbmc binary on the C fixtures.

Skipped automatically when esbmc is not on PATH, so the unit suite stays
self-contained. These guard against ESBMC output-format drift (roadmap Risk 5).
"""

import shutil
from pathlib import Path

import pytest

from forseti.esbmc import (
    Error,
    Unknown,
    UnknownReason,
    Verified,
    Violated,
    verify,
)

pytestmark = pytest.mark.skipif(
    shutil.which("esbmc") is None, reason="esbmc binary not on PATH"
)

FIXTURES = Path(__file__).parent / "fixtures"
EXAMPLES = Path(__file__).resolve().parents[2] / "examples"

# INT64_MIN as the two's-complement bit string esbmc prints (top bit set, 63
# zeros). We pin the counterexample input by this pattern, not the decimal
# literal: esbmc 8.3.0 renders INT64_MIN as "-9223372036854775807 - 1", so a
# decimal-string match would be brittle across versions.
_INT64_MIN_BITS = "1" + "0" * 63


def test_safe_program_verifies() -> None:
    result = verify(FIXTURES / "safe.c", unwind=8)
    assert isinstance(result, Verified)
    assert result.meta.esbmc_version  # version was captured from output


def test_int_min_abs_overflow_is_violated() -> None:
    result = verify(
        FIXTURES / "overflow.c", unwind=1, extra_flags=("--overflow-check",)
    )
    assert isinstance(result, Violated)
    assert "overflow" in result.raw_counterexample.lower()
    # the raw trace is parsed end-to-end into the typed model
    assert result.counterexample is not None
    assert len(result.counterexample.steps) >= 1
    vp = result.counterexample.violated_property
    assert vp.description  # ESBMC's overflow message, whatever its wording
    assert vp.loc.function is not None
    assert vp.loc.file.endswith("overflow.c")


def test_parse_error_is_error() -> None:
    result = verify(FIXTURES / "broken.c", unwind=1)
    assert isinstance(result, Error)


def test_hard_problem_times_out_to_unknown() -> None:
    result = verify(FIXTURES / "hard.c", unwind=1, timeout_s=2)
    assert isinstance(result, Unknown)
    assert result.reason is UnknownReason.TIMEOUT


# The two worked examples behind the manual P0 loop turn
# (docs/walkthroughs/0001-manual-loop-abs.md). These pin the *no-overflow-check*
# default path the walkthrough documents — distinct from
# test_int_min_abs_overflow_is_violated above, which drives fixtures/overflow.c
# *with* --overflow-check (a different violated property).
def test_example_abs_is_violated_at_int_min() -> None:
    result = verify(EXAMPLES / "abs.c", unwind=1)
    assert isinstance(result, Violated)
    cex = result.counterexample
    assert cex is not None
    assert cex.violated_property.loc.file.endswith("abs.c")
    # The breaking input is INT64_MIN, identified by its bit pattern.
    assert any(
        a.binary is not None and a.binary.replace(" ", "") == _INT64_MIN_BITS
        for a in cex.inputs
    )


def test_example_abs_fixed_verifies() -> None:
    result = verify(EXAMPLES / "abs_fixed.c", unwind=1)
    assert isinstance(result, Verified)

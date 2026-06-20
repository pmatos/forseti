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

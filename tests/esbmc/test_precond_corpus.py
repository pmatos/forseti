"""Acceptance: the memory-precondition gate over the sha1 corpus (RFC-0003 S2).

The issue's exit criteria, pinned end-to-end against the real esbmc binary:

- ``sha1_init/update/final/transform`` each reach **ASSUMED_VERIFIED** up to
  ``max_len`` under their synthesised precondition (a fresh ``sha1_ctx``, a
  ``malloc(len)`` buffer, a ``malloc(20)`` digest) — including the non-vacuity
  discharge (a reachable call site).
- the off-by-one twin ``sha1_bug.c::sha1_update`` (``i <= len`` reads
  ``data[len]``) is **VIOLATED** with a real ``array bounds violated``
  counterexample — a non-vacuous failure, not a phantom.

Skipped when esbmc is not on PATH, exactly like `test_corpus.py`; CI installs a
pinned esbmc so it always runs there.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from forseti.precond import Assessment, verify_precondition

pytestmark = pytest.mark.skipif(
    shutil.which("esbmc") is None, reason="esbmc binary not on PATH"
)

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
MAX_LEN = 8


@pytest.mark.parametrize(
    "function",
    ["sha1_init", "sha1_transform", "sha1_update", "sha1_final"],
)
def test_sha1_unit_assumed_verified(function: str) -> None:
    result = verify_precondition(
        EXAMPLES / "sha1.c", function=function, max_len=MAX_LEN
    )
    assert result.assessment is Assessment.ASSUMED_VERIFIED, result.label
    assert result.max_len == MAX_LEN
    assert result.settled_k is not None and result.settled_k > MAX_LEN
    assert "assuming valid caller pointers" in result.label


def test_offbyone_twin_is_violated_non_vacuously() -> None:
    result = verify_precondition(
        EXAMPLES / "sha1_bug.c", function="sha1_update", max_len=MAX_LEN
    )
    assert result.assessment is Assessment.VIOLATED, result.label
    # a real, reachable out-of-bounds — the mechanism the gate exists to keep,
    # never silenced as a phantom.
    assert result.esbmc_result is not None
    raw = getattr(result.esbmc_result, "raw_counterexample", "")
    assert "array bounds violated" in raw


_STATIC_MIN_AND_LEN = """\
#include <stddef.h>

void fill(unsigned char p[static 4], size_t len) {
  for (size_t i = 0; i < len; i++) p[i] = 0;
  p[3] = 1;
}

void fill_bug(unsigned char p[static 4], size_t len) {
  for (size_t i = 0; i <= len; i++) p[i] = 0;
}

void over_minimum(unsigned char p[static 4], size_t len) {
  (void)len;
  p[4] = 0;
}
"""


def test_static_minimum_with_length_is_not_a_phantom(tmp_path: Path) -> None:
    # Issue #137: `p[static 4]` binds the caller to *at least* 4 elements, and the
    # companion `len` says the buffer holds `len` — a valid caller satisfies both,
    # so a body that touches `len` elements *and* `p[3]` is correct code. Sizing
    # the object at exactly 4 phantom-VIOLATED it for every `len > 4`.
    src = tmp_path / "static_min.c"
    src.write_text(_STATIC_MIN_AND_LEN)
    result = verify_precondition(src, function="fill", max_len=MAX_LEN)
    assert result.assessment is Assessment.ASSUMED_VERIFIED, result.label


def test_static_minimum_with_length_still_catches_the_off_by_one(
    tmp_path: Path,
) -> None:
    # The floor must not blunt the detector: `i <= len` reads `p[len]`, which is
    # out of bounds for every `len >= 4`, and stays a real VIOLATED.
    src = tmp_path / "static_min.c"
    src.write_text(_STATIC_MIN_AND_LEN)
    result = verify_precondition(src, function="fill_bug", max_len=MAX_LEN)
    assert result.assessment is Assessment.VIOLATED, result.label
    assert result.esbmc_result is not None
    raw = getattr(result.esbmc_result, "raw_counterexample", "")
    assert "array bounds violated" in raw


def test_static_minimum_floor_still_explores_the_weakest_caller(
    tmp_path: Path,
) -> None:
    # The floor raises the object's *ceiling*, it does not pin the length: `len`
    # stays symbolic down to 0, so a caller giving exactly the declared 4 elements
    # is still explored and `p[4]` — over the minimum, with nothing else to justify
    # it — remains a real VIOLATED rather than being swallowed by the max.
    src = tmp_path / "static_min.c"
    src.write_text(_STATIC_MIN_AND_LEN)
    result = verify_precondition(src, function="over_minimum", max_len=MAX_LEN)
    assert result.assessment is Assessment.VIOLATED, result.label
    assert result.esbmc_result is not None
    raw = getattr(result.esbmc_result, "raw_counterexample", "")
    assert "array bounds violated" in raw

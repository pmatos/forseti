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

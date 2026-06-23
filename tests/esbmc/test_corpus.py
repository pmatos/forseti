"""End-to-end tests pinning the C kernel corpus verdicts (examples/).

Like test_verify_integration.py, these run the real esbmc binary and guard
against ESBMC output-format drift (roadmap Risk 5): each clean kernel must stay
VERIFIED at its documented bound and each ``*_bug`` twin must stay VIOLATED. The
bounds mirror examples/README.md — chosen strictly above each loop's trip count
so the ``--no-unwinding-assertions`` runs stay non-vacuous.
"""

import shutil
from pathlib import Path

import pytest

from forseti.esbmc import Verified, Violated, verify

pytestmark = pytest.mark.skipif(
    shutil.which("esbmc") is None, reason="esbmc binary not on PATH"
)

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"

# (filename, unwind bound) — bounds documented in examples/README.md.
_CLEAN = [
    ("ring_buffer.c", 6),
    ("merge_sort.c", 5),
    ("utf8_decode.c", 4),
    ("murmurhash.c", 8),
]
_BUGGY = [
    ("ring_buffer_bug.c", 6),
    ("merge_sort_bug.c", 5),
    ("utf8_decode_bug.c", 4),
    ("murmurhash_bug.c", 8),
]


@pytest.mark.parametrize("name, k", _CLEAN)
def test_clean_kernel_verifies(name: str, k: int) -> None:
    result = verify(EXAMPLES / name, unwind=k, timeout_s=30)
    assert isinstance(result, Verified)


@pytest.mark.parametrize("name, k", _BUGGY)
def test_bug_kernel_is_violated(name: str, k: int) -> None:
    result = verify(EXAMPLES / name, unwind=k, timeout_s=30)
    assert isinstance(result, Violated)
    # The raw trace is always present; the typed parse is guarded before use.
    assert result.raw_counterexample
    if result.counterexample is not None:
        assert result.counterexample.violated_property.loc.file.endswith(name)

"""Verdict presentation shared by both CLIs: the human text and the exit code.

`render_result` is the human-readable projection of an `EsbmcResult` — the
sibling of `forseti.core.result_to_payload`'s JSON projection — and `EXIT_CODES`
is the one verdict->process-status contract. Both are properties of the sealed
result union, not of any single front-end, so they live here in `forseti.esbmc`
(the layer that owns `EsbmcResult`/`Verdict`) and are shared by the low-level
`forseti-esbmc` shell and the unified `forseti verify` CLI alike. Keeping them in
one place means the two CLIs can never drift to different text or different codes
for the same verdict.
"""

from __future__ import annotations

from pathlib import Path
from typing import assert_never

from .result import Error, EsbmcResult, Unknown, Verdict, Verified, Violated

# Our own exit-code contract (not the esbmc binary's): each verdict maps to a
# distinct status so a shell or CI step can branch on it. UNKNOWN is deliberately
# non-zero — an inconclusive run is a distinct state, never a silent pass.
EXIT_CODES: dict[Verdict, int] = {
    Verdict.VERIFIED: 0,
    Verdict.VIOLATED: 1,
    Verdict.UNKNOWN: 2,
    Verdict.ERROR: 3,
}


def render_result(result: EsbmcResult, source: Path, unwind: int) -> str:
    """Render a verdict as human-readable text (pure; the caller prints it).

    A header line always names the source, the bound `k`, and the esbmc build
    (so a VERIFIED stays honestly qualified as "verified up to k"). The
    variant-specific evidence a human needs to act is appended per verdict: a
    VIOLATED shows its raw counterexample after a blank line, an UNKNOWN its
    reason, an ERROR its message. A VERIFIED adds nothing — "no violation found
    up to k" is the whole story. The `match` is exhaustive over the sealed
    union, so a new verdict variant is a type error here rather than a silent
    omission.
    """
    version = result.meta.esbmc_version or "?"
    header = f"{result.verdict.value.upper()}  ({source}, k={unwind}, esbmc {version})"
    match result:
        case Verified():
            return header
        case Violated():
            return f"{header}\n\n{result.raw_counterexample}"
        case Unknown():
            return f"{header}\nreason: {result.reason.value}"
        case Error():
            return f"{header}\nerror: {result.message}"
        case _:
            assert_never(result)

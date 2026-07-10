"""The three projections of an `EsbmcResult`: human text, exit code, and JSON.

`render_result` is the human-readable projection, `EXIT_CODES` the one
verdict->process-status contract, and `result_to_dict` the JSON projection. All
three are properties of the sealed result union, not of any single front-end, so
they live here in `forseti.esbmc` (the layer that owns `EsbmcResult`/`Verdict`)
and are shared by every caller: the low-level `forseti-esbmc` shell, the unified
`forseti verify` CLI (`core.result_to_payload`), and the property check phase
(`orchestrator.check`). Keeping them in one place means those front-ends can
never drift to different text, different codes, or a different verdict shape.
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


def result_to_dict(
    result: EsbmcResult, *, structured_cex: bool = True
) -> dict[str, object]:
    """Render a verdict as a JSON-serialisable dict — the union's JSON projection.

    Emits only what is *intrinsic* to the result: the verdict, the provenance a
    VERIFIED needs to stay honestly qualified ("verified up to k under this
    esbmc build" — `esbmc_version`, `argv`, `duration_s`), and the
    variant-specific evidence an agent acts on. The `match` is exhaustive over
    the sealed union, so a new verdict variant is a type error here rather than a
    silently dropped shape. A caller adds its own *framing* — a CLI its
    `source`/`unwind`, the check phase its settled `k` — because those are the
    caller's context, not properties of the result.

    `structured_cex` selects a VIOLATED's counterexample shape. True (the default,
    the machine-facing shape the grading harness consumes): `raw_counterexample`
    (the lossless trace text) plus the typed `counterexample`
    (`Counterexample.to_dict()`, or `None` when parsing failed — a parse failure
    never downgrades the verdict). False (the agent-facing CLI/MCP shape,
    mirroring `render_result`'s raw trace): a single `counterexample` field
    carrying the raw trace text.
    """
    payload: dict[str, object] = {
        "verdict": result.verdict.value,
        "esbmc_version": result.meta.esbmc_version,
        "argv": list(result.meta.argv),
        "duration_s": result.meta.duration_s,
    }
    match result:
        case Verified():
            return payload
        case Violated():
            if structured_cex:
                payload["raw_counterexample"] = result.raw_counterexample
                payload["counterexample"] = (
                    result.counterexample.to_dict()
                    if result.counterexample is not None
                    else None
                )
            else:
                payload["counterexample"] = result.raw_counterexample
            return payload
        case Unknown():
            payload["reason"] = result.reason.value
            return payload
        case Error():
            payload["message"] = result.message
            return payload
        case _:
            assert_never(result)

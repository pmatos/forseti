"""Forseti Core's `verify` operation — the harness-neutral entry point.

`verify_source` is the single place the `--function` flag and a sane default
timeout are assembled before delegating to :func:`forseti.esbmc.verify`, so the
unified CLI and the MCP tool share one behaviour. `result_to_payload` renders
any :class:`~forseti.esbmc.EsbmcResult` as a JSON-serialisable dict — the wire
shape both the CLI (`--json`) and the MCP `verify` tool return, so a harness
adapter sees the same verdict structure regardless of transport.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import assert_never

from forseti.esbmc import (
    EsbmcResult,
    Error,
    Frontend,
    Unknown,
    Verified,
    Violated,
    verify,
)

# One JSON object per verdict. `object` (not `Any`) keeps the dict honest under
# mypy --strict while staying trivially `json.dumps`-able.
Payload = dict[str, object]

DEFAULT_UNWIND = 1
DEFAULT_TIMEOUT_S = 30.0


def verify_source(
    source: Path,
    *,
    unwind: int = DEFAULT_UNWIND,
    timeout_s: float | None = DEFAULT_TIMEOUT_S,
    function: str | None = None,
    extra_flags: Sequence[str] = (),
    esbmc_bin: str = "esbmc",
    frontend: Frontend = Frontend.C,
) -> EsbmcResult:
    """Verify `source` with ESBMC and return the typed verdict.

    A thin Core wrapper over :func:`forseti.esbmc.verify`: it folds `function`
    into the forwarded flags (so `--function` is spelled once, here) and applies
    a default timeout so an agent-driven call can never hang unbounded. `unwind`
    is recorded in the result's provenance, so a VERIFIED stays honestly
    qualified as "verified up to k".
    """
    flags = list(extra_flags)
    if function is not None:
        flags += ["--function", function]
    return verify(
        source,
        unwind=unwind,
        timeout_s=timeout_s,
        extra_flags=tuple(flags),
        esbmc_bin=esbmc_bin,
        frontend=frontend,
    )


def result_to_payload(result: EsbmcResult, source: Path, unwind: int) -> Payload:
    """Render a verdict as a JSON-serialisable dict.

    Common provenance (verdict, source, k, esbmc version, argv, duration) is
    always present; the variant-specific evidence an agent needs to act is added
    per verdict: a VIOLATED carries the raw `counterexample`, an UNKNOWN its
    `reason`, an ERROR its `message`. A VERIFIED adds nothing beyond the base —
    "no violation found up to k" is the whole story.
    """
    payload: Payload = {
        "verdict": result.verdict.value,
        "source": str(source),
        "unwind": unwind,
        "esbmc_version": result.meta.esbmc_version,
        "duration_s": result.meta.duration_s,
        "argv": list(result.meta.argv),
    }
    if isinstance(result, Verified):
        return payload
    if isinstance(result, Violated):
        payload["counterexample"] = result.raw_counterexample
        return payload
    if isinstance(result, Unknown):
        payload["reason"] = result.reason.value
        return payload
    if isinstance(result, Error):
        payload["message"] = result.message
        return payload
    assert_never(result)

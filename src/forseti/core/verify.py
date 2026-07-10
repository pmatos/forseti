"""Forseti Core's `verify` operation — the harness-neutral entry point.

`verify_source` applies a sane default timeout and passes the target `function`
through as data before delegating to :func:`forseti.esbmc.verify` (which owns
the `--function` argv spelling), so the unified CLI and the MCP tool share one
behaviour. `result_to_payload` frames the shared
:func:`forseti.esbmc.result_to_dict` projection with this front-end's context
(`source`/`unwind`) — the wire shape both the CLI (`--json`) and the MCP `verify`
tool return, so a harness adapter sees the same verdict structure regardless of
transport.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from forseti.esbmc import EsbmcResult, Frontend, result_to_dict, verify

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

    A thin Core wrapper over :func:`forseti.esbmc.verify`: it passes `function`
    through as data (esbmc owns the `--function` spelling) and applies a default
    timeout so an agent-driven call can never hang unbounded. `unwind` is
    recorded in the result's provenance, so a VERIFIED stays honestly qualified
    as "verified up to k".
    """
    return verify(
        source,
        unwind=unwind,
        timeout_s=timeout_s,
        function=function,
        extra_flags=extra_flags,
        esbmc_bin=esbmc_bin,
        frontend=frontend,
    )


def result_to_payload(result: EsbmcResult, source: Path, unwind: int) -> Payload:
    """Render a verdict as a JSON-serialisable dict — the CLI/MCP wire shape.

    This front-end's framing (`source`, `unwind`) over the shared
    :func:`forseti.esbmc.result_to_dict` projection, which owns the intrinsic
    verdict fields and the exhaustive per-variant evidence. `structured_cex` is
    off, so a VIOLATED carries a single `counterexample` field holding the raw
    trace text — the whole story an agent needs, mirroring `render_result`.
    """
    return {
        "source": str(source),
        "unwind": unwind,
        **result_to_dict(result, structured_cex=False),
    }

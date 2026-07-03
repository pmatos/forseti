"""Forseti Core — the harness-neutral surface (RFC-0001).

Push *all logic* into a Core the harnesses share, then keep each harness's glue
thin. This package is that Core's public face: the `verify` operation plus its
JSON wire shape, exposed as a unified `forseti` CLI (:mod:`forseti.core.cli`)
and an MCP server (:mod:`forseti.core.mcp_server`). `propose` and the loop
orchestration land here next (tracked under epic #14).
"""

from __future__ import annotations

from forseti.esbmc import Verdict

from .verify import Payload, result_to_payload, verify_source

# Our own exit-code contract (not esbmc's): each verdict maps to a distinct
# status so a shell or CI step can branch on it. UNKNOWN is deliberately
# non-zero — an inconclusive run is a distinct state, never a silent pass.
EXIT_CODES: dict[Verdict, int] = {
    Verdict.VERIFIED: 0,
    Verdict.VIOLATED: 1,
    Verdict.UNKNOWN: 2,
    Verdict.ERROR: 3,
}

__all__ = [
    "EXIT_CODES",
    "Payload",
    "result_to_payload",
    "verify_source",
]

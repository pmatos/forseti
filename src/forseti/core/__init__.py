"""Forseti Core — the harness-neutral surface (RFC-0001).

Push *all logic* into a Core the harnesses share, then keep each harness's glue
thin. This package is that Core's public face: the `verify` and `propose`
operations plus their JSON wire shapes, exposed as a unified `forseti` CLI
(:mod:`forseti.core.cli`) and an MCP server (:mod:`forseti.core.mcp_server`).
The loop orchestration lands here next (tracked under epic #14).
"""

from __future__ import annotations

from forseti.esbmc import EXIT_CODES

from .propose import propose_source
from .verify import Payload, result_to_payload, verify_source

# The verdict->exit-code contract is owned by `forseti.esbmc` (the layer that
# owns `Verdict`) and re-exported here, so the unified `forseti` CLI and the
# low-level `forseti-esbmc` shell share one table and can never drift.

__all__ = [
    "EXIT_CODES",
    "Payload",
    "propose_source",
    "result_to_payload",
    "verify_source",
]

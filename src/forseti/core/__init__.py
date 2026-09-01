"""Forseti Core — the harness-neutral surface (RFC-0001).

Push *all logic* into a Core the harnesses share, then keep each harness's glue
thin. This package is that Core's public face: the `verify`, `propose`,
`submit`, and `check` operations plus their JSON wire shapes, exposed as a
unified `forseti` CLI (:mod:`forseti.core.cli`) and an MCP server
(:mod:`forseti.core.mcp_server`). `submit` is `propose`'s LLM-free sibling
(#213): a host harness (or a configured non-`claude -p` proposer) hands Core an
already-formed candidate instead of asking Core's own `ClaudeCliClient` for one,
so the semantic-check path never requires `claude -p` on `PATH`.
"""

from __future__ import annotations

from forseti.esbmc import EXIT_CODES

from .check import check_source
from .propose import propose_source
from .submit import submit_source
from .verify import Payload, result_to_payload, verify_source

# The verdict->exit-code contract is owned by `forseti.esbmc` (the layer that
# owns `Verdict`) and re-exported here, so the unified `forseti` CLI and the
# low-level `forseti-esbmc` shell share one table and can never drift.

__all__ = [
    "EXIT_CODES",
    "Payload",
    "check_source",
    "propose_source",
    "result_to_payload",
    "submit_source",
    "verify_source",
]

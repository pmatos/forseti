"""Forseti Core as an MCP server — the substrate every harness shares (RFC-0001).

Claude Code, Codex, and opencode all differ in their hooks but agree on MCP, so
the Core exposes its operations as MCP tools here. Today that is a single
`verify` tool; `propose` and the loop land alongside it (epic #14). The tool is
a thin shell over :func:`forseti.core.verify_source` and returns the same JSON
payload the CLI's `--json` does, so an adapter sees one verdict shape either way.

This module imports the `mcp` SDK at import time (an optional dependency,
`forseti[mcp]`); the unified CLI imports it lazily so `forseti verify` works
without the SDK installed.
"""

from __future__ import annotations

from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .verify import (
    DEFAULT_TIMEOUT_S,
    DEFAULT_UNWIND,
    Payload,
    result_to_payload,
    verify_source,
)

_INSTRUCTIONS = (
    "Forseti Core: bounded verification with ESBMC. Call `verify` on a source "
    "file after editing it; a VIOLATED verdict carries a concrete "
    "counterexample to fix, and a VERIFIED is only 'verified up to k'."
)


def verify_tool(
    source: str,
    unwind: int = DEFAULT_UNWIND,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    function: str | None = None,
    esbmc_bin: str = "esbmc",
) -> Payload:
    """Verify a source file with ESBMC and return the typed verdict.

    Args:
        source: Path to the source file to verify.
        unwind: Loop-unwind bound k; a VERIFIED is only "verified up to k".
        timeout_s: Per-run timeout in seconds.
        function: Entry function to verify (defaults to ESBMC's, i.e. main).
        esbmc_bin: The esbmc binary to invoke.

    Returns:
        A JSON object with the verdict plus provenance; VIOLATED adds
        `counterexample`, UNKNOWN adds `reason`, ERROR adds `message`.
    """
    path = Path(source)
    result = verify_source(
        path,
        unwind=unwind,
        timeout_s=timeout_s,
        function=function,
        esbmc_bin=esbmc_bin,
    )
    return result_to_payload(result, path, unwind)


def build_server(name: str = "forseti") -> FastMCP:
    """A `FastMCP` server exposing Forseti Core's tools (currently `verify`)."""
    server: FastMCP = FastMCP(name, instructions=_INSTRUCTIONS)
    server.add_tool(
        verify_tool,
        name="verify",
        description="Verify a source file with ESBMC; returns a typed verdict.",
    )
    return server


def serve() -> None:
    """Run the Core MCP server on stdio (the transport all three harnesses use)."""
    build_server().run()

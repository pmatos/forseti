"""Forseti Core as an MCP server — the substrate every harness shares (RFC-0001).

Claude Code, Codex, and opencode all differ in their hooks but agree on MCP, so
the Core exposes its operations as MCP tools here: `verify` (ESBMC) and `propose`
(the property proposer, #65); the loop lands alongside them (epic #14). Each tool
is a thin shell over its `forseti.core` entry point and returns the same JSON
payload the CLI's `--json` does, so an adapter sees one shape either way.

This module imports the `mcp` SDK at import time (an optional dependency,
`forseti[mcp]`); the unified CLI imports it lazily so `forseti verify` works
without the SDK installed.
"""

from __future__ import annotations

from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .propose import (
    DEFAULT_MAX_CANDIDATES,
    DEFAULT_MODEL,
    DEFAULT_STORE_ROOT,
    propose_source,
)
from .propose import (
    DEFAULT_TIMEOUT_S as PROPOSE_TIMEOUT_S,
)
from .verify import (
    DEFAULT_TIMEOUT_S,
    DEFAULT_UNWIND,
    Payload,
    result_to_payload,
    verify_source,
)

_INSTRUCTIONS = (
    "Forseti Core: bounded verification with ESBMC plus LLM property proposal. "
    "Call `verify` on a source file after editing it; a VIOLATED verdict carries "
    "a concrete counterexample to fix, and a VERIFIED is only 'verified up to k'. "
    "Call `propose` to generate candidate properties for a unit before checking."
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


def propose_tool(
    source: str,
    function: str,
    persist: bool = True,
    store_root: str = str(DEFAULT_STORE_ROOT),
    model: str = DEFAULT_MODEL,
    timeout_s: float = PROPOSE_TIMEOUT_S,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
) -> dict[str, object]:
    """Propose candidate properties for a unit and (optionally) store them.

    Args:
        source: Path to the source file defining the unit.
        function: The function under test (the `symbol` of `path::symbol`).
        persist: When true, store each accepted candidate as CANDIDATE; when
            false, a dry run that proposes and validates without writing.
        store_root: The `.forseti` store directory to persist into.
        model: The LLM model the proposer calls.
        timeout_s: Per-call timeout for the proposer's LLM invocation.
        max_candidates: Cap on the number of accepted candidates.

    Returns:
        A JSON object with the unit id, prompt/backend provenance, and the
        `accepted` and `rejected` candidate lists.
    """
    result = propose_source(
        Path(source),
        function=function,
        persist=persist,
        store_root=Path(store_root),
        model=model,
        timeout_s=timeout_s,
        max_candidates=max_candidates,
    )
    return result.to_dict()


def build_server(name: str = "forseti") -> FastMCP:
    """A `FastMCP` server exposing Forseti Core's tools (`verify`, `propose`)."""
    server: FastMCP = FastMCP(name, instructions=_INSTRUCTIONS)
    server.add_tool(
        verify_tool,
        name="verify",
        description="Verify a source file with ESBMC; returns a typed verdict.",
    )
    server.add_tool(
        propose_tool,
        name="propose",
        description=(
            "Propose candidate properties for a unit with the LLM proposer; "
            "returns the accepted and rejected candidates."
        ),
    )
    return server


def serve() -> None:
    """Run the Core MCP server on stdio (the transport all three harnesses use)."""
    build_server().run()

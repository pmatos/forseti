"""Forseti Core as an MCP server — the substrate every harness shares (RFC-0001).

Claude Code, Codex, and opencode all differ in their hooks but agree on MCP, so
the Core exposes its operations as MCP tools here: `verify` (ESBMC), `propose`
(the LLM property proposer, #65), `submit` (ingest a host-generated candidate,
no LLM call, #213), and `check` (verify a unit's stored properties, #213). Each
tool is a thin shell over its `forseti.core` entry point and returns the same
JSON payload the CLI's `--json` does, so an adapter sees one shape either way.

This module imports the `mcp` SDK at import time (an optional dependency,
`forseti[mcp]`); the unified CLI imports it lazily so `forseti verify` works
without the SDK installed.
"""

from __future__ import annotations

from pathlib import Path

from mcp.server.mcpserver import MCPServer

from .check import (
    DEFAULT_TIMEOUT_S as CHECK_TIMEOUT_S,
)
from .check import (
    DEFAULT_UNWIND as CHECK_DEFAULT_UNWIND,
)
from .check import check_source, default_unwind_ladder_above
from .propose import (
    DEFAULT_MAX_CANDIDATES,
    DEFAULT_MODEL,
    DEFAULT_STORE_ROOT,
    propose_source,
)
from .propose import (
    DEFAULT_TIMEOUT_S as PROPOSE_TIMEOUT_S,
)
from .submit import DEFAULT_PROMPT_ID, DEFAULT_PROMPT_VERSION, submit_source
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
    "Call `propose` to generate candidate properties for a unit before checking, "
    "or `submit` to ingest a candidate the host model already generated (no LLM "
    "call). Call `check` to verify a unit's stored properties: held | violated | "
    "unknown | error, plus skipped for a deferred reachability property."
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


def submit_tool(
    source: str,
    function: str,
    expression: str,
    provider: str,
    model: str,
    domain: list[str] | None = None,
    referenced_params: list[str] | None = None,
    rationale: str = "",
    prompt_id: str = DEFAULT_PROMPT_ID,
    prompt_version: str = DEFAULT_PROMPT_VERSION,
    persist: bool = True,
    store_root: str = str(DEFAULT_STORE_ROOT),
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
) -> dict[str, object]:
    """Ingest one host-generated candidate property -- no LLM call.

    Args:
        source: Path to the source file defining the unit.
        function: The function under test (the `symbol` of `path::symbol`).
        expression: The candidate's C boolean expression (e.g. "result >= 0").
        provider: Who/what produced this candidate (e.g. "codex").
        model: The model that produced this candidate (e.g. "gpt-5.1").
        domain: Preconditions over the parameters, emitted as
            `__ESBMC_assume(...)` before the call.
        referenced_params: Parameter names `expression` uses.
        rationale: Free-text rationale, stored as the property's description.
        prompt_id: Provenance prompt id for this candidate.
        prompt_version: Provenance prompt version for this candidate.
        persist: When true, store the candidate as CANDIDATE if it validates;
            when false, a dry run that validates without writing.
        store_root: The `.forseti` store directory to persist into.
        max_candidates: Cap on the number of accepted candidates (this call
            submits one, so this only matters via a shared batch caller).

    Returns:
        The same JSON shape `propose` returns: unit id, prompt/backend
        provenance, and the `accepted`/`rejected` candidate lists.
    """
    result = submit_source(
        Path(source),
        function=function,
        expression=expression,
        provider=provider,
        model=model,
        domain=tuple(domain or ()),
        referenced_params=tuple(referenced_params or ()),
        rationale=rationale,
        prompt_id=prompt_id,
        prompt_version=prompt_version,
        persist=persist,
        store_root=Path(store_root),
        max_candidates=max_candidates,
    )
    return result.to_dict()


def check_tool(
    source: str,
    function: str,
    store_root: str = str(DEFAULT_STORE_ROOT),
    unwind: int = CHECK_DEFAULT_UNWIND,
    unwind_ladder: list[int] | None = None,
    timeout_s: float = CHECK_TIMEOUT_S,
    esbmc_bin: str = "esbmc",
) -> dict[str, object]:
    """Check a unit's stored, checkable properties against ESBMC.

    Args:
        source: Path to the source file defining the unit.
        function: The function under test (the `symbol` of `path::symbol`).
        store_root: The `.forseti` store directory to read properties from.
        unwind: Loop-unwind bound k for the first attempt.
        unwind_ladder: Bounds tried after `unwind` on an UNKNOWN verdict
            (default: `default_unwind_ladder_above(unwind)`).
        timeout_s: Per-attempt esbmc timeout in seconds.
        esbmc_bin: The esbmc binary to invoke.

    Returns:
        A JSON object with the unit id, per-outcome counts, and one verdict
        per checked property: held | violated | unknown | error | skipped.
    """
    ladder = (
        tuple(unwind_ladder)
        if unwind_ladder is not None
        else default_unwind_ladder_above(unwind)
    )
    run = check_source(
        Path(source),
        function=function,
        store_root=Path(store_root),
        unwind=unwind,
        unwind_ladder=ladder,
        timeout_s=timeout_s,
        esbmc_bin=esbmc_bin,
    )
    return run.to_dict()


def build_server(name: str = "forseti") -> MCPServer:
    """An `MCPServer` exposing Forseti Core's tools (`verify`, `propose`,
    `submit`, `check`)."""
    server: MCPServer = MCPServer(name, instructions=_INSTRUCTIONS)
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
    server.add_tool(
        submit_tool,
        name="submit",
        description=(
            "Ingest one host-generated candidate property -- no LLM call; "
            "validated the same way `propose`'s candidates are."
        ),
    )
    server.add_tool(
        check_tool,
        name="check",
        description=(
            "Check a unit's stored properties with ESBMC; returns held | "
            "violated | unknown | error (plus skipped) per property."
        ),
    )
    return server


def serve() -> None:
    """Run the Core MCP server on stdio (the transport all three harnesses use)."""
    build_server().run()

"""Tests for the Core MCP server.

Skipped when the optional `mcp` SDK is not installed (`pytest.importorskip`
runs before the server module is imported). The verdict-producing tests also
need esbmc on PATH.
"""

import asyncio
import os
import shutil
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402
from mcp.types import CallToolResult  # noqa: E402

from forseti.core.mcp_server import build_server, verify_tool  # noqa: E402

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"

needs_esbmc = pytest.mark.skipif(
    shutil.which("esbmc") is None, reason="esbmc binary not on PATH"
)


def test_build_server_registers_verify_tool() -> None:
    server = build_server()
    tools = asyncio.run(server.list_tools())
    names = {t.name for t in tools}
    assert "verify" in names
    verify = next(t for t in tools if t.name == "verify")
    # The tool's input schema is derived from verify_tool's signature.
    assert "source" in verify.inputSchema.get("properties", {})


@needs_esbmc
def test_verify_tool_reports_violation() -> None:
    payload = verify_tool(str(EXAMPLES / "abs.c"), unwind=1)
    assert payload["verdict"] == "violated"
    assert payload["counterexample"]


@needs_esbmc
def test_verify_stdio_roundtrip() -> None:
    """Drive the server over a real MCP stdio transport, end to end.

    Launches `python -m forseti.core mcp` as the server subprocess and calls the
    `verify` tool through an MCP client session — the same path a hookless
    harness (opencode) takes. The child inherits our environment so it finds
    esbmc on PATH.
    """

    async def roundtrip() -> CallToolResult:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "forseti.core", "mcp"],
            env={**os.environ},
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await session.call_tool(
                    "verify",
                    {"source": str(EXAMPLES / "abs.c"), "unwind": 1},
                )

    result = asyncio.run(asyncio.wait_for(roundtrip(), timeout=60.0))
    assert result.isError is False
    payload = result.structuredContent
    assert payload is not None
    assert payload["verdict"] == "violated"
    assert payload["counterexample"]

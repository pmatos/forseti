"""Tests for the Core MCP server.

Skipped when the optional `mcp` SDK is not installed (`pytest.importorskip`
runs before the server module is imported). The verdict-producing tests also
need esbmc on PATH.
"""

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult

from forseti.core.mcp_server import build_server, propose_tool, verify_tool

_ABS_SLICE = "int64_t my_abs(int64_t x) {\n    return (x < 0) ? -x : x;\n}\n"
_CANNED_REPLY = json.dumps({"candidates": [{"expression": "result >= 0"}]})


class _FakeLLMClient:
    """A stand-in `LLMClient` returning a canned candidate reply (no subprocess)."""

    provider = "fake"
    model = "fake-1"

    def __init__(self, *_a: object, **_kw: object) -> None: ...

    def complete(self, prompt: str) -> str:
        return _CANNED_REPLY


EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
SRC = Path(__file__).resolve().parents[2] / "src"

needs_esbmc = pytest.mark.skipif(
    shutil.which("esbmc") is None, reason="esbmc binary not on PATH"
)


def _child_env() -> dict[str, str]:
    """Environment for the server subprocess.

    Prepend `src/` to PYTHONPATH so the child imports `forseti` via the same
    source tree pytest uses (pyproject `pythonpath = ["src"]`), rather than
    depending on an editable install being present and correctly located. Also
    carries PATH so the child finds esbmc.
    """
    existing = os.environ.get("PYTHONPATH")
    pythonpath = f"{SRC}{os.pathsep}{existing}" if existing else str(SRC)
    return {**os.environ, "PYTHONPATH": pythonpath}


def test_build_server_registers_verify_tool() -> None:
    server = build_server()
    tools = asyncio.run(server.list_tools())
    names = {t.name for t in tools}
    assert "verify" in names
    verify = next(t for t in tools if t.name == "verify")
    # The tool's input schema is derived from verify_tool's signature.
    assert "source" in verify.inputSchema.get("properties", {})


def test_build_server_registers_propose_tool() -> None:
    server = build_server()
    tools = asyncio.run(server.list_tools())
    names = {t.name for t in tools}
    assert "propose" in names
    propose = next(t for t in tools if t.name == "propose")
    props = propose.inputSchema.get("properties", {})
    assert "source" in props
    assert "function" in props


def test_propose_tool_returns_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Hermetic: no esbmc, no live LLM. Monkeypatch the proposer's client and run
    # a dry proposal (persist=False) so the tool's payload shape is pinned.
    source = tmp_path / "abs_unit.c"
    source.write_text(_ABS_SLICE)
    monkeypatch.setattr("forseti.core.propose.ClaudeCliClient", _FakeLLMClient)
    payload = propose_tool(str(source), "my_abs", persist=False)
    assert payload["unit_id"] == f"{source}::my_abs"
    accepted = payload["accepted"]
    assert isinstance(accepted, list)
    assert any(a["expression"] == "result >= 0" for a in accepted)


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
    harness (opencode) takes. The child gets `src/` on PYTHONPATH (so it imports
    forseti) plus PATH (so it finds esbmc); see `_child_env`.
    """

    async def roundtrip() -> CallToolResult:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "forseti.core", "mcp"],
            env=_child_env(),
        )
        async with (
            stdio_client(params) as (read, write),
            ClientSession(read, write) as session,
        ):
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

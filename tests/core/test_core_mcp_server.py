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

from forseti.core.mcp_server import (
    build_server,
    check_tool,
    propose_tool,
    semantic_loop_tool,
    submit_tool,
    verify_tool,
)

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
    assert "source" in verify.input_schema.get("properties", {})


def test_build_server_registers_propose_tool() -> None:
    server = build_server()
    tools = asyncio.run(server.list_tools())
    names = {t.name for t in tools}
    assert "propose" in names
    propose = next(t for t in tools if t.name == "propose")
    props = propose.input_schema.get("properties", {})
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


def test_build_server_registers_submit_tool() -> None:
    server = build_server()
    tools = asyncio.run(server.list_tools())
    names = {t.name for t in tools}
    assert "submit" in names
    submit = next(t for t in tools if t.name == "submit")
    props = submit.input_schema.get("properties", {})
    assert {"source", "function", "expression", "provider", "model"} <= props.keys()


def test_build_server_registers_check_tool() -> None:
    server = build_server()
    tools = asyncio.run(server.list_tools())
    names = {t.name for t in tools}
    assert "check" in names
    check = next(t for t in tools if t.name == "check")
    props = check.input_schema.get("properties", {})
    assert "source" in props
    assert "function" in props


def test_submit_tool_never_shells_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No LLM call: the MCP `submit` tool must never invoke a subprocess."""
    import subprocess

    def _boom(*_a: object, **_kw: object) -> None:
        raise AssertionError("submit_tool must never invoke a subprocess")

    monkeypatch.setattr(subprocess, "run", _boom)
    source = tmp_path / "abs_unit.c"
    source.write_text(_ABS_SLICE)
    payload = submit_tool(
        str(source),
        "my_abs",
        "result >= 0",
        provider="codex",
        model="gpt-5.1",
        persist=False,
    )
    assert payload["provider"] == "codex"
    accepted = payload["accepted"]
    assert isinstance(accepted, list)
    assert accepted[0]["expression"] == "result >= 0"
    assert accepted[0]["provenance"]["provider"] == "codex"


@needs_esbmc
def test_check_tool_reports_held_and_violated(tmp_path: Path) -> None:
    source = tmp_path / "abs_unit.c"
    source.write_text(_ABS_SLICE)
    store_root = tmp_path / ".forseti"
    submit_tool(
        str(source),
        "my_abs",
        "result >= 0",
        provider="codex",
        model="gpt-5.1",
        domain=["x > INT64_MIN"],  # exclude the un-negatable minimum
        store_root=str(store_root),
    )
    submit_tool(
        str(source),
        "my_abs",
        "result == x",  # false for any negative x -- must come back VIOLATED
        provider="codex",
        model="gpt-5.1",
        domain=["x > INT64_MIN"],
        store_root=str(store_root),
    )
    run = check_tool(str(source), "my_abs", store_root=str(store_root))
    counts = run["counts"]
    assert isinstance(counts, dict)
    assert counts["held"] == 1
    assert counts["violated"] == 1
    verdicts = run["verdicts"]
    assert isinstance(verdicts, list)
    outcomes = {v["outcome"] for v in verdicts}
    assert outcomes == {"held", "violated"}
    violated = next(v for v in verdicts if v["outcome"] == "violated")
    assert violated["result"]["raw_counterexample"]


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
    assert result.is_error is False
    payload = result.structured_content
    assert payload is not None
    assert payload["verdict"] == "violated"
    assert payload["counterexample"]


# --- semantic_loop (#213) -----------------------------------------------------


def test_build_server_registers_semantic_loop_tool() -> None:
    server = build_server()
    tools = asyncio.run(server.list_tools())
    names = {t.name for t in tools}
    assert "semantic_loop" in names
    tool = next(t for t in tools if t.name == "semantic_loop")
    props = tool.input_schema.get("properties", {})
    assert {"source", "function", "mode"} <= props.keys()


def test_semantic_loop_tool_rejects_an_unknown_mode(tmp_path: Path) -> None:
    source = tmp_path / "abs_unit.c"
    source.write_text(_ABS_SLICE)
    with pytest.raises(ValueError, match="mode must be one of"):
        semantic_loop_tool(str(source), "my_abs", "bogus-mode")


def test_semantic_loop_tool_submit_mode_rejects_non_list_domain(tmp_path: Path) -> None:
    source = tmp_path / "abs_unit.c"
    source.write_text(_ABS_SLICE)
    with pytest.raises(ValueError, match="list of strings"):
        semantic_loop_tool(
            str(source),
            "my_abs",
            "submit",
            store_root=str(tmp_path / ".forseti"),
            candidates=[{"expression": "result >= 0", "domain": "x > 0"}],
            provider="codex",
            model="gpt-5.1",
        )


@needs_esbmc
def test_semantic_loop_tool_propose_mode_ingests_via_the_llm_proposer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The canned reply has no `domain`, so `result >= 0` is actually violated
    # at x == INT64_MIN -- proves propose -> persist -> check ran for real,
    # not just that the call didn't crash.
    monkeypatch.setattr("forseti.core.propose.ClaudeCliClient", _FakeLLMClient)
    source = tmp_path / "abs_unit.c"
    source.write_text(_ABS_SLICE)
    store_root = tmp_path / ".forseti"

    payload = semantic_loop_tool(
        str(source), "my_abs", "propose", store_root=str(store_root)
    )
    ingestion = payload["ingestion"]
    assert isinstance(ingestion, list)
    assert ingestion[0]["accepted"][0]["expression"] == "result >= 0"
    assert payload["outcome"] == "violated"


@needs_esbmc
def test_semantic_loop_tool_submit_mode_ingests_and_checks(tmp_path: Path) -> None:
    source = tmp_path / "abs_unit.c"
    source.write_text(_ABS_SLICE)
    store_root = tmp_path / ".forseti"

    payload = semantic_loop_tool(
        str(source),
        "my_abs",
        "submit",
        store_root=str(store_root),
        candidates=[{"expression": "result >= 0", "domain": ["x > INT64_MIN"]}],
        provider="codex",
        model="gpt-5.1",
    )
    assert payload["outcome"] == "held"
    ingestion = payload["ingestion"]
    assert isinstance(ingestion, list)
    assert ingestion[0]["accepted"][0]["expression"] == "result >= 0"


@needs_esbmc
def test_semantic_loop_tool_check_only_mode_reads_persisted_candidate(
    tmp_path: Path,
) -> None:
    source = tmp_path / "abs_unit.c"
    source.write_text(_ABS_SLICE)
    store_root = tmp_path / ".forseti"
    submit_tool(
        str(source),
        "my_abs",
        "result >= 0",
        provider="codex",
        model="gpt-5.1",
        domain=["x > INT64_MIN"],
        store_root=str(store_root),
    )

    payload = semantic_loop_tool(
        str(source), "my_abs", "check_only", store_root=str(store_root)
    )
    assert payload["ingestion"] == []
    assert payload["outcome"] == "held"


@needs_esbmc
@pytest.mark.parametrize(
    ("expression", "domain", "expected_outcome", "expected_returncode"),
    [
        ("result >= 0", ["x > INT64_MIN"], "held", 0),
        ("result < 0", [], "violated", 1),
    ],
)
def test_semantic_loop_stdio_roundtrip_matches_cli_outcome(
    tmp_path: Path,
    expression: str,
    domain: list[str],
    expected_outcome: str,
    expected_returncode: int,
) -> None:
    """Drive `semantic_loop` over a real MCP stdio transport, end to end --
    the shape Codex's host model uses (design doc: MCP, no subagent concept)
    -- and cross-check it against the exact same eligible-C-function fixture
    driven through the `forseti semantic-loop` CLI subprocess, the shape a
    Claude Code Bash subagent uses (issue #213 acceptance: both adapter
    contracts must reach the same outcome for the same fixture, both a valid
    property that holds and an invalid one that comes back with a
    counterexample)."""
    import subprocess

    source = tmp_path / "abs_unit.c"
    source.write_text(_ABS_SLICE)
    mcp_root = tmp_path / ".forseti-mcp"
    cli_root = tmp_path / ".forseti-cli"

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
                "semantic_loop",
                {
                    "source": str(source),
                    "function": "my_abs",
                    "mode": "submit",
                    "store_root": str(mcp_root),
                    "candidates": [{"expression": expression, "domain": domain}],
                    "provider": "codex",
                    "model": "gpt-5.1",
                },
            )

    mcp_result = asyncio.run(asyncio.wait_for(roundtrip(), timeout=60.0))
    assert mcp_result.is_error is False
    mcp_payload = mcp_result.structured_content
    assert mcp_payload is not None
    assert mcp_payload["outcome"] == expected_outcome

    cli_candidates = tmp_path / "candidates.json"
    cli_candidates.write_text(
        json.dumps([{"expression": expression, "domain": domain}])
    )
    cli = subprocess.run(
        [
            sys.executable,
            "-m",
            "forseti.core",
            "semantic-loop",
            str(source),
            "--function",
            "my_abs",
            "--mode",
            "submit",
            "--candidates-json",
            str(cli_candidates),
            "--provider",
            "codex",
            "--model",
            "gpt-5.1",
            "--store-root",
            str(cli_root),
            "--json",
        ],
        capture_output=True,
        text=True,
        env=_child_env(),
        check=False,
    )
    assert cli.returncode == expected_returncode, cli.stderr
    cli_payload = json.loads(cli.stdout)
    assert cli_payload["outcome"] == mcp_payload["outcome"] == expected_outcome

"""Tests for `forseti mcp` dispatch — the lazy-import shell around the server.

Hermetic: no esbmc, no server subprocess, and no dependency on the optional
`mcp` SDK being installed either way. The point is the CLI's two branches —
the SDK is missing (exit 1 with an actionable message) or `serve()` is handed
control — not the server itself, which `test_core_mcp_server.py` covers.
"""

import sys
import types

import pytest

from forseti.core.cli import main


class _StubServerModule(types.ModuleType):
    """Stands in for `forseti.core.mcp_server`, recording `serve()` calls.

    A real ModuleType subclass rather than a namespace object, so the lazy
    `from .mcp_server import serve` resolves through the normal import
    machinery. `serve` binds as a method, so the CLI's zero-arg call lands here.
    """

    def __init__(self) -> None:
        super().__init__("forseti.core.mcp_server")
        self.calls = 0

    def serve(self) -> None:
        self.calls += 1


def test_mcp_hands_control_to_serve(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _StubServerModule()
    monkeypatch.setitem(sys.modules, "forseti.core.mcp_server", stub)
    assert main(["mcp"]) == 0
    assert stub.calls == 1


def test_mcp_without_the_sdk_exits_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Simulate the base install: `mcp` present but hollow, so mcp_server's
    # `from mcp.server.mcpserver import MCPServer` raises. Drop any already-imported
    # mcp_server first, or the cached module would satisfy the import.
    monkeypatch.delitem(sys.modules, "forseti.core.mcp_server", raising=False)
    monkeypatch.setitem(sys.modules, "mcp", types.ModuleType("mcp"))
    monkeypatch.delitem(sys.modules, "mcp.server", raising=False)
    monkeypatch.delitem(sys.modules, "mcp.server.mcpserver", raising=False)

    assert main(["mcp"]) == 1
    err = capsys.readouterr().err
    assert "pip install 'forseti[mcp]'" in err

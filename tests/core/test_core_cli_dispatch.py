"""Tests for the `forseti` CLI's subcommand → handler dispatch.

Hermetic: builds the parser and inspects the bound handler, or drives `main`
with a stubbed handler; never invokes esbmc, an LLM, or the MCP server. The
contract under test is that *registering* a subcommand is what binds its
handler, so a subcommand can never be added to the parser yet forgotten in the
dispatch (the failure mode of a parallel name→function table).
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from forseti.core import cli
from forseti.core.cli import _build_parser, main


def _registered_subparsers(
    parser: argparse.ArgumentParser,
) -> dict[str, argparse.ArgumentParser]:
    """The subcommand name → subparser map argparse builds under the hood."""
    actions = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    assert len(actions) == 1, "expected exactly one subparsers group"
    return dict(actions[0].choices)


def test_every_subcommand_binds_a_callable_handler() -> None:
    # The dispatch contract: each registered subcommand carries its own handler
    # as a parser default, so `main` routes without a parallel name→function
    # table. Enumerated generically so a subcommand registered inline (e.g. mcp)
    # is covered too — this closes the register-but-forget-to-dispatch gap.
    subparsers = _registered_subparsers(_build_parser())
    assert set(subparsers) == {
        "verify",
        "list-units",
        "synth",
        "discharge",
        "propose",
        "claude-code-hook",
        "enable-project",
        "mcp",
    }
    for name, subparser in subparsers.items():
        handler = subparser.get_default("func")
        assert callable(handler), f"subcommand {name!r} binds no callable handler"
        # A shared arg-builder (e.g. `synth`/`discharge`'s `_add_precondition_
        # arguments`) binding a handler itself, rather than leaving it to each
        # `_add_*_parser`, would silently glue every user of that builder to
        # the same handler — pin the name→handler identity, not just callable.
        expected_handler = getattr(cli, f"_run_{name.replace('-', '_')}")
        assert handler is expected_handler, (
            f"subcommand {name!r} binds {handler!r}, expected {expected_handler!r}"
        )


def test_main_dispatches_to_the_bound_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `main` routes purely through the bound handler: replace the function the
    # `verify` subcommand resolves to and confirm `main` hands it the parsed
    # namespace and returns its exit code — no esbmc runs.
    captured: dict[str, argparse.Namespace] = {}

    def spy(args: argparse.Namespace) -> int:
        captured["args"] = args
        return 7

    monkeypatch.setattr(cli, "_run_verify", spy)
    code = main(["verify", "x.c"])
    assert code == 7
    assert captured["args"].command == "verify"
    assert captured["args"].source == Path("x.c")


def test_bare_invocation_errors_without_a_subcommand() -> None:
    # required=True is preserved: a bare `forseti` is a parse error (exit 2),
    # never a fall-through to a handler.
    with pytest.raises(SystemExit) as excinfo:
        main([])
    assert excinfo.value.code == 2


def test_unknown_subcommand_errors() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["does-not-exist"])
    assert excinfo.value.code == 2

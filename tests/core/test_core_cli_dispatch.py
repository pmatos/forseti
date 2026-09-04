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

from forseti.adapters.claude_code.install import HOOK_NAMES as CLAUDE_CODE_HOOK_NAMES
from forseti.adapters.codex.install import HOOK_NAMES as CODEX_HOOK_NAMES
from forseti.adapters.oh_my_pi.install import HOOK_NAMES as OMP_HOOK_NAMES
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
        "submit-property",
        "check",
        "semantic-loop",
        "claude-code-hook",
        "codex-hook",
        "omp-hook",
        "enable-project",
        "disable-project",
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


def test_claude_code_hook_choices_match_install_hook_specs() -> None:
    # The `claude-code-hook <name>` positional's argparse `choices` and
    # `install._HOOK_SPECS` (what `enable-project` writes into settings.json)
    # each name the same four hooks independently; pin them together so one
    # can't drift from the other.
    subparsers = _registered_subparsers(_build_parser())
    name_action = next(
        a for a in subparsers["claude-code-hook"]._actions if a.dest == "name"
    )
    assert set(name_action.choices or []) == CLAUDE_CODE_HOOK_NAMES


def test_codex_hook_choices_match_install_hook_names() -> None:
    # Same pin as above, for `codex-hook <name>` against
    # `codex.install.HOOK_NAMES` (what `enable-project --harness codex` wires).
    subparsers = _registered_subparsers(_build_parser())
    name_action = next(a for a in subparsers["codex-hook"]._actions if a.dest == "name")
    assert set(name_action.choices or []) == CODEX_HOOK_NAMES


def test_omp_hook_choices_match_install_hook_names() -> None:
    # Same pin as above, for `omp-hook <name>` against
    # `oh_my_pi.install.HOOK_NAMES` (what `enable-project --harness oh-my-pi`
    # wires into the packaged `forseti-gate.ts` extension).
    subparsers = _registered_subparsers(_build_parser())
    name_action = next(a for a in subparsers["omp-hook"]._actions if a.dest == "name")
    assert set(name_action.choices or []) == OMP_HOOK_NAMES


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

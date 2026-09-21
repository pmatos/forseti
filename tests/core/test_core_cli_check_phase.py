"""Tests for the check-phase argument surface shared by `check` and `semantic-loop`.

`_add_check_phase_arguments`/`_check_phase_kwargs` are the single home for the
flags both subcommands take before an ESBMC check (`-k/--unwind`,
`--unwind-ladder`, `-t/--timeout`, `--max-len`, `--json`, `--esbmc-bin` and the
`--` passthrough) and for their mapping onto Core's keyword call — the core-CLI
sibling of `esbmc.verify_cli`'s `add_verify_arguments`/`verify_kwargs`. Pure
argparse plus a recording stub for the Core entry points: never invokes esbmc
or an LLM.
"""

from __future__ import annotations

import argparse
import inspect
from pathlib import Path
from typing import Any

import pytest

from forseti.core import check_source, cli
from forseti.core.check import DEFAULT_TIMEOUT_S, DEFAULT_UNWIND
from forseti.core.loop import run_semantic_loop
from forseti.precond import DEFAULT_MAX_LEN

_CHECK_PHASE_DESTS = (
    "unwind",
    "unwind_ladder",
    "timeout",
    "max_len",
    "json",
    "esbmc_bin",
    "esbmc_args",
)
_FLAGS = [
    "-k",
    "3",
    "--unwind-ladder",
    "5,9",
    "-t",
    "7",
    "--max-len",
    "4",
    "--esbmc-bin",
    "/x",
    "--",
    "-DN",
]
_FORWARDED = {
    "unwind": 3,
    "unwind_ladder": (5, 9),
    "extra_flags": ("-DN",),
    "esbmc_bin": "/x",
    "max_len": 4,
}


def _parser(
    json_help: str = "JSON-HELP", passthrough_example: str = "EXAMPLE"
) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    cli._add_check_phase_arguments(
        parser, json_help=json_help, passthrough_example=passthrough_example
    )
    return parser


def _help_for(parser: argparse.ArgumentParser, dest: str) -> str | None:
    (action,) = [a for a in parser._actions if a.dest == dest]
    return action.help


def test_defaults_are_cores_check_defaults() -> None:
    args = _parser().parse_args([])
    assert args.unwind == DEFAULT_UNWIND
    # `None`, not a derived tuple: check_source derives the rungs above --unwind.
    assert args.unwind_ladder is None
    assert args.timeout == DEFAULT_TIMEOUT_S
    assert args.max_len == DEFAULT_MAX_LEN
    assert args.json is False
    assert args.esbmc_bin == "esbmc"
    assert args.esbmc_args == []


def test_overrides_parse() -> None:
    args = _parser().parse_args(
        [
            "-k",
            "3",
            "--unwind-ladder",
            "5,9",
            "-t",
            "7",
            "--max-len",
            "4",
            "--json",
            "--esbmc-bin",
            "/opt/esbmc",
            "--",
            "-DNDEBUG",
            "-I.",
        ]
    )
    assert args.unwind == 3
    assert args.unwind_ladder == (5, 9)
    assert args.timeout == 7.0
    assert args.max_len == 4
    assert args.json is True
    assert args.esbmc_bin == "/opt/esbmc"
    assert args.esbmc_args == ["-DNDEBUG", "-I."]


def test_an_explicit_empty_ladder_is_not_the_derived_default() -> None:
    assert _parser().parse_args(["--unwind-ladder", ""]).unwind_ladder == ()


def test_only_the_json_help_and_passthrough_example_vary() -> None:
    parser = _parser(json_help="emit it", passthrough_example="... f.c -- -DX")
    assert _help_for(parser, "json") == "emit it"
    assert _help_for(parser, "esbmc_args") == (
        "flags forwarded verbatim to esbmc; place them after a `--` separator, "
        "e.g. `... f.c -- -DX`"
    )


@pytest.mark.parametrize(
    ("timeout_kw", "core_entry_point"),
    [("timeout_s", check_source), ("check_timeout_s", run_semantic_loop)],
)
def test_kwargs_spell_the_timeout_for_their_core_entry_point(
    timeout_kw: Any, core_entry_point: Any
) -> None:
    args = _parser().parse_args(
        ["-k", "3", "--unwind-ladder", "", "-t", "7", "--max-len", "4", "--", "-DN"]
    )
    kwargs = cli._check_phase_kwargs(args, timeout_kw=timeout_kw)
    assert kwargs == {
        "unwind": 3,
        "unwind_ladder": (),
        timeout_kw: 7.0,
        "extra_flags": ("-DN",),
        "esbmc_bin": "esbmc",
        "max_len": 4,
    }
    # The splat is not keyword-checked statically: pin that every key is one
    # the Core entry point actually accepts.
    assert set(kwargs) <= set(inspect.signature(core_entry_point).parameters)


def test_kwargs_forward_an_unset_ladder_as_none() -> None:
    kwargs = cli._check_phase_kwargs(_parser().parse_args([]), timeout_kw="timeout_s")
    assert kwargs["unwind_ladder"] is None


def _check_phase_actions(
    subparser: argparse.ArgumentParser,
) -> list[tuple[Any, ...]]:
    return [
        (a.dest, tuple(a.option_strings), a.default, a.type, a.nargs, a.metavar)
        for a in subparser._actions
        if a.dest in _CHECK_PHASE_DESTS
    ]


def test_check_and_semantic_loop_register_the_same_surface() -> None:
    (sub,) = [
        a
        for a in cli._build_parser()._actions
        if isinstance(a, argparse._SubParsersAction)
    ]
    check, loop = sub.choices["check"], sub.choices["semantic-loop"]
    assert [a[0] for a in _check_phase_actions(check)] == list(_CHECK_PHASE_DESTS)
    assert _check_phase_actions(check) == _check_phase_actions(loop)
    assert _help_for(check, "json") == "emit the check run as a JSON object"
    assert _help_for(loop, "json") == (
        "emit the run as a JSON object (the MCP tool's payload)"
    )


def _record_core_call(monkeypatch: pytest.MonkeyPatch, name: str) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    def record(source: Path, **kwargs: Any) -> Any:
        seen.update(kwargs)
        raise ValueError("stop after the call is recorded")

    monkeypatch.setattr(cli, name, record)
    return seen


def test_check_forwards_the_check_phase_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _record_core_call(monkeypatch, "check_source")
    argv = ["check", "f.c", "--function", "f", "--store-root", str(tmp_path)]
    assert cli.main([*argv, *_FLAGS]) == 1
    assert seen == {
        "function": "f",
        "store_root": tmp_path,
        "timeout_s": 7.0,
        **_FORWARDED,
    }


def test_semantic_loop_forwards_the_check_phase_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _record_core_call(monkeypatch, "run_semantic_loop")
    argv = ["semantic-loop", "f.c", "--function", "f", "--mode", "check-only"]
    assert cli.main([*argv, "--store-root", str(tmp_path), *_FLAGS]) == 1
    assert {k: seen[k] for k in (*_FORWARDED, "check_timeout_s")} == {
        "check_timeout_s": 7.0,
        **_FORWARDED,
    }
    assert "timeout_s" not in seen

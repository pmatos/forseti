"""Unit tests for the shared verify-CLI surface — pure argparse, no esbmc.

`add_verify_arguments`/`verify_kwargs` are the single home for the verify
argument surface (`source`, `-k/--unwind`, `-t/--timeout`, `--function`,
`--esbmc-bin`, and the `--`-separated esbmc passthrough) and its mapping onto the
`verify(...)` keyword call. Both CLIs (`forseti-esbmc` and `forseti verify`) build
on them, so they are pinned here once, at the layer that owns them, rather than
re-derived per front-end — the sibling discipline to `render_result`/`EXIT_CODES`
in test_render.py.
"""

from __future__ import annotations

import argparse

from forseti.esbmc import verify_cli


def _parse(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    verify_cli.add_verify_arguments(parser)
    return parser.parse_args(list(argv))


def test_defaults_match_the_shared_constants() -> None:
    args = _parse("f.c")
    assert args.unwind == verify_cli.DEFAULT_UNWIND
    assert args.timeout == verify_cli.DEFAULT_TIMEOUT_S
    assert args.function is None
    assert args.esbmc_bin == "esbmc"
    assert args.esbmc_args == []


def test_shared_defaults_are_k1_and_bounded_timeout() -> None:
    # k=1 (a VERIFIED is only "verified up to k") and a bounded default so an
    # agent-driven invocation can never hang unbounded. Independent literals,
    # not read back from the implementation.
    assert verify_cli.DEFAULT_UNWIND == 1
    assert verify_cli.DEFAULT_TIMEOUT_S == 30.0


def test_unwind_and_timeout_overrides_parse() -> None:
    args = _parse("f.c", "-k", "8", "-t", "5")
    assert args.unwind == 8
    assert args.timeout == 5.0


def test_function_and_binary_overrides_parse() -> None:
    args = _parse("f.c", "--function", "drfrom_bytes", "--esbmc-bin", "/opt/esbmc")
    assert args.function == "drfrom_bytes"
    assert args.esbmc_bin == "/opt/esbmc"


def test_passthrough_flags_captured_after_separator() -> None:
    args = _parse("f.c", "--", "--overflow-check", "--memory-leak-check")
    assert args.esbmc_args == ["--overflow-check", "--memory-leak-check"]


def test_verify_kwargs_maps_every_shared_argument() -> None:
    args = _parse(
        "f.c",
        "-k",
        "8",
        "-t",
        "5",
        "--function",
        "foo",
        "--esbmc-bin",
        "/opt/esbmc",
        "--",
        "--overflow-check",
    )
    assert verify_cli.verify_kwargs(args) == {
        "unwind": 8,
        "timeout_s": 5.0,
        "function": "foo",
        "extra_flags": ("--overflow-check",),
        "esbmc_bin": "/opt/esbmc",
    }


def test_verify_kwargs_extra_flags_is_a_tuple() -> None:
    # `verify` takes a Sequence; a tuple keeps the forwarded flags immutable and
    # matches build_argv's own return type.
    args = _parse("f.c")
    assert verify_cli.verify_kwargs(args)["extra_flags"] == ()
    assert isinstance(verify_cli.verify_kwargs(args)["extra_flags"], tuple)


def test_core_verify_reuses_the_same_default_constants() -> None:
    # core's library/MCP defaults re-source the shared CLI defaults rather than
    # defining their own copy, so the two entry points can never drift on k or
    # the timeout budget (mirrors the EXIT_CODES single-source-of-truth check).
    from forseti.core import verify as core_verify

    assert core_verify.DEFAULT_UNWIND is verify_cli.DEFAULT_UNWIND
    assert core_verify.DEFAULT_TIMEOUT_S is verify_cli.DEFAULT_TIMEOUT_S

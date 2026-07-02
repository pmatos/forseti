"""The unified ``forseti`` command — Forseti Core's CLI face (RFC-0001).

Subcommands:

- ``forseti verify <source>`` — run ESBMC and print a typed verdict (or ``--json``
  for the same payload the MCP tool returns). Its exit code follows Core's
  verdict contract (:data:`forseti.core.EXIT_CODES`): VERIFIED=0, VIOLATED=1,
  UNKNOWN=2, ERROR=3 — an inconclusive run is never a silent pass.
- ``forseti mcp`` — start the Core MCP server on stdio (needs the ``mcp`` extra;
  imported lazily so plain ``verify`` works without the SDK).

The low-level ``forseti-esbmc`` entry point stays as the thin esbmc-only shell;
this is the harness-neutral Core surface that grows ``propose`` and the loop.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from forseti.esbmc import Error, EsbmcResult, Unknown, Violated

from . import EXIT_CODES
from .verify import DEFAULT_TIMEOUT_S, DEFAULT_UNWIND, result_to_payload, verify_source


def _add_verify_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser(
        "verify",
        help="run ESBMC on a source and report a typed verdict",
        description=(
            "Verify a source with ESBMC: verified (up to k) | violated | "
            "unknown | error."
        ),
    )
    p.add_argument("source", type=Path, help="source file to verify")
    p.add_argument(
        "-k",
        "--unwind",
        type=int,
        default=DEFAULT_UNWIND,
        help=f"loop unwind bound k (default: {DEFAULT_UNWIND}); a VERIFIED is only 'verified up to k'",
    )
    p.add_argument(
        "-t",
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_S,
        metavar="SECONDS",
        help=f"per-run timeout in seconds (default: {DEFAULT_TIMEOUT_S:g})",
    )
    p.add_argument(
        "--function",
        metavar="NAME",
        help="entry function to verify (default: ESBMC's, i.e. main)",
    )
    p.add_argument(
        "--esbmc-bin",
        default="esbmc",
        help="esbmc binary to invoke (default: esbmc on PATH)",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="emit the verdict as a JSON object (the MCP tool's payload)",
    )
    # Passthrough after `--`; see forseti.esbmc.cli for why not a `-X` option.
    p.add_argument(
        "esbmc_args",
        nargs="*",
        metavar="ESBMC_ARG",
        help="flags forwarded verbatim to esbmc, placed after a `--` separator",
    )


def _report(result: EsbmcResult, source: Path, unwind: int) -> None:
    version = result.meta.esbmc_version or "?"
    print(f"{result.verdict.value.upper()}  ({source}, k={unwind}, esbmc {version})")
    if isinstance(result, Violated):
        print()
        print(result.raw_counterexample)
    elif isinstance(result, Unknown):
        print(f"reason: {result.reason.value}")
    elif isinstance(result, Error):
        print(f"error: {result.message}")


def _run_verify(args: argparse.Namespace) -> int:
    result = verify_source(
        args.source,
        unwind=args.unwind,
        timeout_s=args.timeout,
        function=args.function,
        extra_flags=tuple(args.esbmc_args),
        esbmc_bin=args.esbmc_bin,
    )
    if args.json:
        print(json.dumps(result_to_payload(result, args.source, args.unwind)))
    else:
        _report(result, args.source, args.unwind)
    return EXIT_CODES[result.verdict]


def _run_mcp(_args: argparse.Namespace) -> int:
    try:
        from .mcp_server import serve
    except ImportError:
        print(
            "forseti mcp: the MCP server needs the 'mcp' extra. "
            "Install it with:  pip install 'forseti[mcp]'",
            file=sys.stderr,
        )
        return 1
    serve()
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="forseti",
        description="Forseti Core: write -> verify -> counterexample -> fix.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    _add_verify_parser(sub)
    sub.add_parser(
        "mcp",
        help="start the Core MCP server on stdio (needs the 'mcp' extra)",
        description="Expose Forseti Core's tools (currently `verify`) over MCP/stdio.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "verify":
        return _run_verify(args)
    if args.command == "mcp":
        return _run_mcp(args)
    raise AssertionError(f"unhandled command: {args.command}")  # pragma: no cover


if __name__ == "__main__":
    raise SystemExit(main())

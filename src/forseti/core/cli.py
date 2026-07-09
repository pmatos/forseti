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

from forseti.esbmc import add_verify_arguments, render_result, verify_kwargs

from . import EXIT_CODES
from .verify import result_to_payload, verify_source


def _add_verify_parser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    p = sub.add_parser(
        "verify",
        help="run ESBMC on a source and report a typed verdict",
        description=(
            "Verify a source with ESBMC: verified (up to k) | violated | "
            "unknown | error."
        ),
    )
    add_verify_arguments(p)
    p.add_argument(
        "--json",
        action="store_true",
        help="emit the verdict as a JSON object (the MCP tool's payload)",
    )


def _run_verify(args: argparse.Namespace) -> int:
    result = verify_source(args.source, **verify_kwargs(args))
    if args.json:
        print(json.dumps(result_to_payload(result, args.source, args.unwind)))
    else:
        print(render_result(result, args.source, args.unwind))
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

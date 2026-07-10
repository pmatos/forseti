"""The unified ``forseti`` command — Forseti Core's CLI face (RFC-0001).

Subcommands:

- ``forseti verify <source>`` — run ESBMC and print a typed verdict (or ``--json``
  for the same payload the MCP tool returns). Its exit code follows Core's
  verdict contract (:data:`forseti.core.EXIT_CODES`): VERIFIED=0, VIOLATED=1,
  UNKNOWN=2, ERROR=3 — an inconclusive run is never a silent pass.
- ``forseti propose <source> --function NAME`` — ask the property proposer (#65)
  for candidate properties over that unit and persist the survivors (``--json``
  emits the same payload the MCP tool returns). Exit 0 on a completed run,
  1 when the proposer itself fails (LLM/parse/IO) — never a silent empty run.
- ``forseti mcp`` — start the Core MCP server on stdio (needs the ``mcp`` extra;
  imported lazily so plain ``verify`` works without the SDK).

The low-level ``forseti-esbmc`` entry point stays as the thin esbmc-only shell;
this is the harness-neutral Core surface that grows the loop next.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from forseti.esbmc import render_result
from forseti.properties import LLMError, ProposalParseError, ProposalResult

from . import EXIT_CODES
from .propose import (
    DEFAULT_MAX_CANDIDATES,
    DEFAULT_MODEL,
    DEFAULT_STORE_ROOT,
    propose_source,
)
from .propose import (
    DEFAULT_TIMEOUT_S as PROPOSE_TIMEOUT_S,
)
from .verify import DEFAULT_TIMEOUT_S, DEFAULT_UNWIND, result_to_payload, verify_source


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
    p.add_argument("source", type=Path, help="source file to verify")
    p.add_argument(
        "-k",
        "--unwind",
        type=int,
        default=DEFAULT_UNWIND,
        help=(
            f"loop unwind bound k (default: {DEFAULT_UNWIND}); "
            "a VERIFIED is only 'verified up to k'"
        ),
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
        print(render_result(result, args.source, args.unwind))
    return EXIT_CODES[result.verdict]


def _add_propose_parser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    p = sub.add_parser(
        "propose",
        help="propose candidate properties for a unit and persist the survivors",
        description=(
            "Ask the property proposer (an LLM) for candidate properties over "
            "<source>::<function>, statically validate them, and store the "
            "survivors as CANDIDATE."
        ),
    )
    p.add_argument("source", type=Path, help="source file defining the unit")
    p.add_argument(
        "--function",
        required=True,
        metavar="NAME",
        help="the function under test (the `symbol` of `path::symbol`)",
    )
    p.add_argument(
        "--no-store",
        action="store_true",
        help="dry run: propose and validate without writing to the store",
    )
    p.add_argument(
        "--store-root",
        type=Path,
        default=DEFAULT_STORE_ROOT,
        metavar="DIR",
        help=f"the .forseti store directory (default: {DEFAULT_STORE_ROOT})",
    )
    p.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"LLM model for the proposer (default: {DEFAULT_MODEL})",
    )
    p.add_argument(
        "--claude-bin",
        default="claude",
        help="claude binary to invoke (default: claude on PATH)",
    )
    p.add_argument(
        "-t",
        "--timeout",
        type=float,
        default=PROPOSE_TIMEOUT_S,
        metavar="SECONDS",
        help=f"proposer LLM timeout in seconds (default: {PROPOSE_TIMEOUT_S:g})",
    )
    p.add_argument(
        "--max-candidates",
        type=int,
        default=DEFAULT_MAX_CANDIDATES,
        metavar="N",
        help=f"cap on accepted candidates (default: {DEFAULT_MAX_CANDIDATES})",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="emit the proposal as a JSON object (the MCP tool's payload)",
    )


def _render_proposal(result: ProposalResult) -> str:
    """A concise human summary of a proposer run (the non-``--json`` output)."""
    lines = [
        f"Proposed {len(result.accepted)} propert"
        f"{'y' if len(result.accepted) == 1 else 'ies'} for {result.unit_id} "
        f"(provider={result.provider}, model={result.model})",
    ]
    for prop in result.accepted:
        domain = f"  [domain: {', '.join(prop.domain)}]" if prop.domain else ""
        lines.append(f"  + [{prop.property_id}] {prop.expression}{domain}")
    if result.rejected:
        lines.append(f"Rejected {len(result.rejected)}:")
        lines.extend(
            f"  - {rej.spec.expression}: {rej.reason}" for rej in result.rejected
        )
    return "\n".join(lines)


def _run_propose(args: argparse.Namespace) -> int:
    try:
        result = propose_source(
            args.source,
            function=args.function,
            persist=not args.no_store,
            store_root=args.store_root,
            model=args.model,
            claude_bin=args.claude_bin,
            timeout_s=args.timeout,
            max_candidates=args.max_candidates,
        )
    except (LLMError, ProposalParseError, OSError) as exc:
        print(f"forseti propose: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result.to_dict()))
    else:
        print(_render_proposal(result))
    return 0


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
    _add_propose_parser(sub)
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
    if args.command == "propose":
        return _run_propose(args)
    if args.command == "mcp":
        return _run_mcp(args)
    raise AssertionError(f"unhandled command: {args.command}")  # pragma: no cover


if __name__ == "__main__":
    raise SystemExit(main())

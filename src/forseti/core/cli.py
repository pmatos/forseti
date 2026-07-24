"""The unified ``forseti`` command — Forseti Core's CLI face (RFC-0001).

Subcommands:

- ``forseti verify <source>`` — run ESBMC and print a typed verdict (or ``--json``
  for the same payload the MCP tool returns). Its exit code follows Core's
  verdict contract (:data:`forseti.core.EXIT_CODES`): VERIFIED=0, VIOLATED=1,
  UNKNOWN=2, ERROR=3 — an inconclusive run is never a silent pass.
- ``forseti synth <source> --function NAME`` — synthesise an L0 memory
  precondition for that pointer-taking unit (RFC-0003 S2) and verify against it,
  reporting an honestly-labelled assessment (``--emit-only`` prints the generated
  sidecar instead). Exit follows the assessment contract
  (:data:`forseti.precond.ASSESSMENT_EXIT_CODES`).
- ``forseti propose <source> --function NAME`` — ask the property proposer (#65)
  for candidate properties over that unit and persist the survivors (``--json``
  emits the same payload the MCP tool returns). Exit 0 on a completed run,
  1 when the run itself fails (LLM/parse/store/IO) — never a silent empty run.
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

from forseti.esbmc import (
    ListUnitsError,
    Violated,
    add_verify_arguments,
    list_units,
    render_result,
    verify_kwargs,
)
from forseti.precond import (
    ASSESSMENT_EXIT_CODES,
    DEFAULT_MAX_LEN,
    Assessment,
    PreconditionUnavailable,
    synthesize,
    verify_precondition,
)
from forseti.precond import (
    DEFAULT_TIMEOUT_S as SYNTH_TIMEOUT_S,
)
from forseti.properties import (
    LLMError,
    PropertyStoreError,
    ProposalParseError,
    ProposalResult,
)

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


def _add_list_units_parser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    p = sub.add_parser(
        "list-units",
        help="list a C source's function definitions and their parameter types",
        description=(
            "Parse <source> with ESBMC's clang frontend (`--parse-tree-only`, no "
            "main needed) and report each function definition, its parameters "
            "with canonical (typedef-resolved) types, and whether it takes a "
            "pointer/array parameter."
        ),
    )
    p.add_argument("source", type=Path, help="C source file to inspect")
    p.add_argument(
        "-t",
        "--timeout",
        type=float,
        default=30.0,
        metavar="SECONDS",
        help="esbmc parse timeout in seconds (default: 30)",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="emit the units as a JSON object",
    )


def _run_list_units(args: argparse.Namespace) -> int:
    try:
        units = list_units(args.source, timeout_s=args.timeout)
    except ListUnitsError as exc:
        print(f"forseti list-units: {exc}", file=sys.stderr)
        return 1
    if args.json:
        payload = {
            "source": str(args.source),
            "units": [
                {
                    "function": u.name,
                    "takes_pointer": u.takes_pointer,
                    "params": [
                        {
                            "name": p.name,
                            "type": p.type,
                            "is_pointer": p.is_pointer,
                            "array_extent": p.array_extent,
                        }
                        for p in u.params
                    ],
                }
                for u in units
            ],
        }
        print(json.dumps(payload))
    else:
        for u in units:
            mark = " [needs-contract]" if u.takes_pointer else ""
            sig = ", ".join(f"{p.type} {p.name}".strip() for p in u.params) or "void"
            print(f"{u.name}({sig}){mark}")
    return 0


def _add_synth_parser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    p = sub.add_parser(
        "synth",
        help="synthesise a memory precondition for a unit and verify against it",
        description=(
            "Read an L0 memory precondition off <source>::<function>'s type "
            "signature (RFC-0003 S2), materialise a valid object per pointer in a "
            "generated sidecar (the source stays pristine), and verify with "
            "unwinding assertions on + a k-ladder + a non-vacuity check. A pass is "
            "reported honestly as VERIFIED *assuming valid caller pointers* "
            "(undischarged)."
        ),
    )
    p.add_argument("source", type=Path, help="C source file defining the unit")
    p.add_argument(
        "--function",
        required=True,
        metavar="NAME",
        help="the pointer-taking function under test (the `symbol` of `path::symbol`)",
    )
    p.add_argument(
        "--max-len",
        type=int,
        default=DEFAULT_MAX_LEN,
        metavar="N",
        help=(
            "symbolic-length ceiling for `(ptr, len)` shapes "
            f"(default: {DEFAULT_MAX_LEN}); a VERIFIED is 'assumed up to len<=N'"
        ),
    )
    p.add_argument(
        "-t",
        "--timeout",
        type=float,
        default=SYNTH_TIMEOUT_S,
        metavar="SECONDS",
        help=f"per-run esbmc timeout in seconds (default: {SYNTH_TIMEOUT_S:g})",
    )
    p.add_argument(
        "--esbmc-bin",
        default="esbmc",
        help="esbmc binary to invoke (default: esbmc on PATH)",
    )
    p.add_argument(
        "--emit-only",
        action="store_true",
        help="print the generated sidecar C harness and exit (no verification)",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="emit the assessment as a JSON object",
    )


def _run_synth(args: argparse.Namespace) -> int:
    if args.emit_only:
        try:
            text = synthesize(
                args.source,
                function=args.function,
                max_len=args.max_len,
                esbmc_bin=args.esbmc_bin,
            )
        except PreconditionUnavailable as exc:
            print(f"forseti synth: {exc.detail}", file=sys.stderr)
            return ASSESSMENT_EXIT_CODES[exc.assessment]
        print(text, end="")
        return 0

    result = verify_precondition(
        args.source,
        function=args.function,
        max_len=args.max_len,
        timeout_s=args.timeout,
        esbmc_bin=args.esbmc_bin,
    )
    if args.json:
        print(json.dumps(result.to_dict()))
    else:
        print(f"{args.source}::{args.function}: {result.label}")
        if result.assessment is Assessment.VIOLATED and isinstance(
            result.esbmc_result, Violated
        ):
            print(f"\n{result.esbmc_result.raw_counterexample}")
    return ASSESSMENT_EXIT_CODES[result.assessment]


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
    except (LLMError, ProposalParseError, PropertyStoreError, OSError) as exc:
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
    _add_list_units_parser(sub)
    _add_synth_parser(sub)
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
    if args.command == "list-units":
        return _run_list_units(args)
    if args.command == "synth":
        return _run_synth(args)
    if args.command == "propose":
        return _run_propose(args)
    if args.command == "mcp":
        return _run_mcp(args)
    raise AssertionError(f"unhandled command: {args.command}")  # pragma: no cover


if __name__ == "__main__":
    raise SystemExit(main())

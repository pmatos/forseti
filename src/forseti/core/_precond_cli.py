"""The ``forseti synth`` / ``forseti discharge`` subcommands (RFC-0003 S2/S3).

Split out of :mod:`forseti.core.cli` because these two are the CLI's only
consumers of :mod:`forseti.precond`: they share the ``_add_precondition_
arguments`` surface, the ``{source}::{function}: {label}`` headline, and the
``--emit-only`` → :class:`PreconditionUnavailable` → assessment-exit-code shape.
``cli`` re-imports ``_add_*_parser``/``_run_*`` so registering a subcommand still
binds its own handler (the dispatch contract), keeping this pure glue: the
engine lives in :mod:`forseti.precond`, this only parses args, formats, and maps
the assessment to an exit code.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from forseti.esbmc import Violated
from forseti.precond import (
    ASSESSMENT_EXIT_CODES,
    DEFAULT_MAX_LEN,
    Assessment,
    PreconditionUnavailable,
    discharge_precondition,
    emit_obligations,
    synthesize,
    verify_precondition,
)
from forseti.precond import (
    DEFAULT_TIMEOUT_S as SYNTH_TIMEOUT_S,
)


def _add_precondition_arguments(p: argparse.ArgumentParser, *, emit_help: str) -> None:
    """The argument surface `synth` and `discharge` share (RFC-0003 S2/S3)."""
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
    p.add_argument("--emit-only", action="store_true", help=emit_help)
    p.add_argument(
        "--json",
        action="store_true",
        help="emit the assessment as a JSON object",
    )


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
    _add_precondition_arguments(
        p,
        emit_help="print the generated sidecar C harness and exit (no verification)",
    )
    p.set_defaults(func=_run_synth)


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


def _add_discharge_parser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    p = sub.add_parser(
        "discharge",
        help="verify a unit's memory precondition and discharge it at its callers",
        description=(
            "Run `synth` (RFC-0003 S2), then discharge the assumption it leaves "
            "(S3): the same precondition is injected into a generated *copy* of "
            "the translation unit as a checked obligation at the unit's entry, and "
            "every caller in that TU is verified against the copy. The verdict is "
            "upgraded to VERIFIED (discharged) only when the unit is `static` (so "
            "this TU holds every caller), every caller was checked, and every "
            "check passed; a caller that passes an invalid or too-small pointer is "
            "VIOLATED at the call site."
        ),
    )
    _add_precondition_arguments(
        p,
        emit_help=(
            "print the obligation-injected copy of the translation unit and exit "
            "(no verification)"
        ),
    )
    p.set_defaults(func=_run_discharge)


def _run_discharge(args: argparse.Namespace) -> int:
    if args.emit_only:
        try:
            text = emit_obligations(
                args.source,
                function=args.function,
                esbmc_bin=args.esbmc_bin,
            )
        except PreconditionUnavailable as exc:
            print(f"forseti discharge: {exc.detail}", file=sys.stderr)
            return ASSESSMENT_EXIT_CODES[exc.assessment]
        print(text, end="")
        return 0

    result = discharge_precondition(
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
        for check in result.callers:
            print(f"  {check.caller}(): {check.outcome.value} — {check.detail}")
    return ASSESSMENT_EXIT_CODES[result.assessment]

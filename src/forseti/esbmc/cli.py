"""Command-line entry point: run ESBMC on a C source and print the verdict.

Exposed as the ``forseti-esbmc`` console script and via
``python -m forseti.esbmc``. A thin shell over :func:`forseti.esbmc.verify` —
the classification lives in the library, never here.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .result import Error, EsbmcResult, Unknown, Verdict, Violated
from .runner import verify

# Our own exit-code contract (not esbmc's): each verdict maps to a distinct
# status so a shell or CI step can branch on it. UNKNOWN is deliberately
# non-zero — an inconclusive run is a distinct state, never a silent pass.
_EXIT = {
    Verdict.VERIFIED: 0,
    Verdict.VIOLATED: 1,
    Verdict.UNKNOWN: 2,
    Verdict.ERROR: 3,
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="forseti-esbmc",
        description=(
            "Run ESBMC on a C source and report a typed verdict: "
            "verified (up to k) | violated | unknown | error."
        ),
    )
    parser.add_argument("source", type=Path, help="C source file to verify")
    parser.add_argument(
        "-k",
        "--unwind",
        type=int,
        default=1,
        help="loop unwind bound k (default: 1); a VERIFIED is only 'verified up to k'",
    )
    parser.add_argument(
        "-t",
        "--timeout",
        type=float,
        default=30.0,
        metavar="SECONDS",
        help="per-run timeout in seconds (default: 30)",
    )
    parser.add_argument(
        "--function",
        metavar="NAME",
        help="entry function to verify (default: ESBMC's, i.e. main)",
    )
    parser.add_argument(
        "--esbmc-bin",
        default="esbmc",
        help="esbmc binary to invoke (default: esbmc on PATH)",
    )
    parser.add_argument(
        "-X",
        "--extra",
        action="append",
        default=[],
        metavar="FLAG",
        help="extra flag passed straight through to esbmc (repeatable)",
    )
    return parser


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


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    extra = list(args.extra)
    if args.function:
        extra += ["--function", args.function]

    result = verify(
        args.source,
        unwind=args.unwind,
        timeout_s=args.timeout,
        extra_flags=tuple(extra),
        esbmc_bin=args.esbmc_bin,
    )
    _report(result, args.source, args.unwind)
    return _EXIT[result.verdict]


if __name__ == "__main__":
    raise SystemExit(main())

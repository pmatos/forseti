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
    # Passthrough lives after a `--` separator rather than behind a `-X` option:
    # esbmc flags are almost always dashed (--overflow-check, ...), and argparse
    # cannot bind a dashed token as the value of an optional, so `-X --overflow-check`
    # would parse as a missing argument. After `--`, option parsing is off and the
    # flags reach us verbatim.
    parser.add_argument(
        "esbmc_args",
        nargs="*",
        metavar="ESBMC_ARG",
        help=(
            "flags forwarded verbatim to esbmc; place them after a `--` separator, "
            "e.g. `forseti-esbmc file.c -- --overflow-check --no-unwinding-assertions`"
        ),
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

    extra = list(args.esbmc_args)
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

"""Command-line entry point: run ESBMC on a C source and print the verdict.

Exposed as the ``forseti-esbmc`` console script and via
``python -m forseti.esbmc``. A thin shell over :func:`forseti.esbmc.verify` —
the classification lives in the library, never here.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .render import EXIT_CODES, render_result
from .runner import verify


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


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    result = verify(
        args.source,
        unwind=args.unwind,
        timeout_s=args.timeout,
        function=args.function,
        extra_flags=tuple(args.esbmc_args),
        esbmc_bin=args.esbmc_bin,
    )
    print(render_result(result, args.source, args.unwind))
    return EXIT_CODES[result.verdict]


if __name__ == "__main__":
    raise SystemExit(main())

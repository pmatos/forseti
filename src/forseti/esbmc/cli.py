"""Command-line entry point: run ESBMC on a C source and print the verdict.

Exposed as the ``forseti-esbmc`` console script and via
``python -m forseti.esbmc``. A thin shell over :func:`forseti.esbmc.verify` —
the classification lives in the library, never here.
"""

from __future__ import annotations

import argparse

from .render import EXIT_CODES, render_result
from .runner import verify
from .verify_cli import add_verify_arguments, verify_kwargs


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="forseti-esbmc",
        description=(
            "Run ESBMC on a C source and report a typed verdict: "
            "verified (up to k) | violated | unknown | error."
        ),
    )
    add_verify_arguments(parser)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    result = verify(args.source, **verify_kwargs(args))
    print(render_result(result, args.source, args.unwind))
    return EXIT_CODES[result.verdict]


if __name__ == "__main__":
    raise SystemExit(main())

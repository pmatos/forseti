"""The shared verify argument surface, owned by the esbmc layer.

Both the low-level ``forseti-esbmc`` shell (:mod:`forseti.esbmc.cli`) and the
unified ``forseti verify`` subcommand (:mod:`forseti.core.cli`) expose the *same*
verify surface — the ``source``, the ``-k/--unwind`` bound, the ``-t/--timeout``
budget, the ``--function`` entry point, the ``--esbmc-bin`` selector, and the
``--``-separated ``esbmc_args`` passthrough — and turn parsed args into the *same*
``verify(...)`` keyword call. The two already shared ``render_result`` and
``EXIT_CODES`` (:mod:`forseti.esbmc.render`) so verdict text and exit codes could
never drift; this module is the sibling home for the *input* side, so the flags,
their defaults, and the kwargs handed to esbmc are spelled once rather than
copied per front-end. ``--json`` and the ``mcp`` subcommand stay with the
``forseti`` parser — they are the unified CLI's own surface, not the shared one.

Pure and esbmc-free: it builds argparse wiring and a kwargs dict, so it sits at
the bottom of the dependency graph and is directly testable without the binary.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

# The shared verify defaults. ``k=1`` keeps a VERIFIED honestly qualified as
# "verified up to k"; a bounded default timeout means an agent-driven invocation
# can never hang unbounded. Re-sourced by ``forseti.core.verify`` for its
# library/MCP defaults so the CLI and the programmatic entry point stay in step.
DEFAULT_UNWIND = 1
DEFAULT_TIMEOUT_S = 30.0

# Passthrough lives after a ``--`` separator rather than behind a ``-X`` option:
# esbmc flags are almost always dashed (--overflow-check, ...), and argparse
# cannot bind a dashed token as the value of an optional, so ``-X --overflow-check``
# would parse as a missing argument. After ``--``, option parsing is off and the
# flags reach us verbatim.
_PASSTHROUGH_HELP = (
    "flags forwarded verbatim to esbmc; place them after a `--` separator, e.g. "
    "`... file.c -- --overflow-check --no-unwinding-assertions`"
)


def add_verify_arguments(parser: argparse.ArgumentParser) -> None:
    """Register the shared verify argument surface on `parser`.

    Adds ``source``, ``-k/--unwind``, ``-t/--timeout``, ``--function``,
    ``--esbmc-bin``, and the trailing ``esbmc_args`` passthrough — the exact set
    both CLIs need. Callers layer their own extras on top (``forseti verify`` adds
    ``--json``); everything shared is spelled here once.
    """
    parser.add_argument("source", type=Path, help="source file to verify")
    parser.add_argument(
        "-k",
        "--unwind",
        type=int,
        default=DEFAULT_UNWIND,
        help=(
            f"loop unwind bound k (default: {DEFAULT_UNWIND}); "
            "a VERIFIED is only 'verified up to k'"
        ),
    )
    parser.add_argument(
        "-t",
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_S,
        metavar="SECONDS",
        help=f"per-run timeout in seconds (default: {DEFAULT_TIMEOUT_S:g})",
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
        "esbmc_args",
        nargs="*",
        metavar="ESBMC_ARG",
        help=_PASSTHROUGH_HELP,
    )


def verify_kwargs(args: argparse.Namespace) -> dict[str, Any]:
    """Map parsed verify args to the ``verify``/``verify_source`` keyword call.

    The single place the shared arguments become the keyword call, so both CLIs
    hand esbmc identical ``(unwind, timeout_s, function, extra_flags, esbmc_bin)``.
    `source` is returned by the caller separately (it is the one positional the
    verify functions take positionally). `extra_flags` is a tuple so the forwarded
    passthrough stays immutable and matches `build_argv`'s own return type.
    """
    return {
        "unwind": args.unwind,
        "timeout_s": args.timeout,
        "function": args.function,
        "extra_flags": tuple(args.esbmc_args),
        "esbmc_bin": args.esbmc_bin,
    }

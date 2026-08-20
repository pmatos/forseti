"""The unified ``forseti`` command — Forseti Core's CLI face (RFC-0001).

Subcommands:

- ``forseti verify <source>`` — run ESBMC and print a typed verdict (or ``--json``
  for the same payload the MCP tool returns). Its exit code follows Core's
  verdict contract (:data:`forseti.core.EXIT_CODES`): VERIFIED=0, VIOLATED=1,
  UNKNOWN=2, ERROR=3 — an inconclusive run is never a silent pass.
- ``forseti list-units <source>`` — enumerate the source's function definitions
  and their canonical (typedef-resolved) parameter types from ESBMC's own clang
  frontend (``--json`` for a machine-readable payload). The harness adapters call
  this instead of pattern-matching signatures themselves, so the frontend that
  *verifies* a unit is also the one that finds it (#131).
- ``forseti synth <source> --function NAME`` — synthesise an L0 memory
  precondition for that pointer-taking unit (RFC-0003 S2) and verify against it,
  reporting an honestly-labelled assessment (``--emit-only`` prints the generated
  sidecar instead). Exit follows the assessment contract
  (:data:`forseti.precond.ASSESSMENT_EXIT_CODES`).
- ``forseti discharge <source> --function NAME`` — the same, then *discharge*
  the assumption (RFC-0003 S3): the precondition is injected into a generated
  copy of the translation unit as a checked obligation and verified at every
  caller, upgrading ``ASSUMED_VERIFIED`` to ``DISCHARGED_VERIFIED`` only when
  every caller was checked and every check passed.
- ``forseti propose <source> --function NAME`` — ask the property proposer (#65)
  for candidate properties over that unit and persist the survivors (``--json``
  emits the same payload the MCP tool returns). Exit 0 on a completed run,
  1 when the run itself fails (LLM/parse/store/IO) — never a silent empty run.
- ``forseti check <source> --function NAME`` — check that unit's stored,
  checkable properties (#66) against ESBMC, one verdict each (``--json`` emits
  the ``PropertyCheckRun`` payload). Exit follows worst-outcome-wins over every
  checked property (:data:`EXIT_CODES`, VIOLATED > UNKNOWN > ERROR > VERIFIED);
  a run with nothing checkable (no stored properties, or every stored property
  is reachability-kind and deferred per ADR-0009 D2) says so loudly rather than
  reading as a clean pass.
- ``forseti claude-code-hook <name>`` — dispatch to one of the Claude Code
  adapter's verify-gate hooks (RFC-0004). Internal: wired into a project's
  settings file by ``enable-project``, not meant to be run by hand.
- ``forseti enable-project [DIR] [--shared]`` — install/update the Claude Code
  verify-gate hooks for a project (RFC-0004). Idempotent: always regenerates
  forseti's own hook entries from the currently installed version, leaving
  every other hook/key in the target settings file untouched.
- ``forseti mcp`` — start the Core MCP server on stdio (needs the ``mcp`` extra;
  imported lazily so plain ``verify`` works without the SDK).

The low-level ``forseti-esbmc`` entry point stays as the thin esbmc-only shell;
this is the harness-neutral Core surface that grows the loop next.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path

from forseti.adapters.claude_code.install import (
    HOOK_NAMES,
    ProjectSettingsError,
    install,
)
from forseti.esbmc import (
    ListUnitsError,
    Verdict,
    Violated,
    add_esbmc_invocation_arguments,
    add_verify_arguments,
    list_units,
    render_result,
    verify_kwargs,
)
from forseti.orchestrator import PropertyCheckRun, property_check_transcript
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
from forseti.properties import (
    LLMError,
    PropertyStoreError,
    ProposalParseError,
    ProposalResult,
)
from forseti.update_notice import installed_version, update_notice

from . import EXIT_CODES
from .check import (
    DEFAULT_TIMEOUT_S as CHECK_TIMEOUT_S,
)
from .check import (
    DEFAULT_UNWIND as CHECK_DEFAULT_UNWIND,
)
from .check import (
    DEFAULT_UNWIND_LADDER as CHECK_DEFAULT_UNWIND_LADDER,
)
from .check import check_source
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
    p.set_defaults(func=_run_verify)


def _run_verify(args: argparse.Namespace) -> int:
    result = verify_source(args.source, **verify_kwargs(args))
    if args.json:
        print(json.dumps(result_to_payload(result, args.source, args.unwind)))
    else:
        print(render_result(result, args.source, args.unwind))
    return EXIT_CODES[result.verdict]


_LIST_UNITS_PASSTHROUGH_HELP = (
    "flags forwarded verbatim to the esbmc parse; place them after a `--` "
    "separator, e.g. `... file.c -- -Iinclude -DNDEBUG`. Use this for the build "
    "flags the translation unit needs to parse at all — a missing `-I` makes "
    "esbmc exit nonzero and the listing fail rather than report no units"
)


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
    # Same `--esbmc-bin` + `--` passthrough surface as `forseti verify`, so a
    # translation unit that only *parses* with build flags can be enumerated at
    # all: without its `-I`, esbmc exits nonzero on the `#include` and the listing
    # raises rather than returning units. `-D` goes further than convenience —
    # it changes *which* functions exist, so a gate that enumerates without the
    # project's defines would miss units the verify then has to check.
    add_esbmc_invocation_arguments(p, passthrough_help=_LIST_UNITS_PASSTHROUGH_HELP)
    p.set_defaults(func=_run_list_units)


def _run_list_units(args: argparse.Namespace) -> int:
    try:
        units = list_units(
            args.source,
            esbmc_bin=args.esbmc_bin,
            timeout_s=args.timeout,
            extra_flags=tuple(args.esbmc_args),
        )
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
                            "array_extent_unresolved": p.array_extent_unresolved,
                            "array_static_min": p.array_static_min,
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


def _add_unit_store_arguments(p: argparse.ArgumentParser) -> None:
    """`<source> --function NAME [--store-root DIR]`, shared by propose/check."""
    p.add_argument("source", type=Path, help="source file defining the unit")
    p.add_argument(
        "--function",
        required=True,
        metavar="NAME",
        help="the function under test (the `symbol` of `path::symbol`)",
    )
    p.add_argument(
        "--store-root",
        type=Path,
        default=DEFAULT_STORE_ROOT,
        metavar="DIR",
        help=f"the .forseti store directory (default: {DEFAULT_STORE_ROOT})",
    )


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
    _add_unit_store_arguments(p)
    p.add_argument(
        "--no-store",
        action="store_true",
        help="dry run: propose and validate without writing to the store",
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
    p.set_defaults(func=_run_propose)


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


def _parse_ladder(value: str) -> tuple[int, ...]:
    """``"8,16"`` -> ``(8, 16)``; ``""`` -> ``()``. Raises on a non-int token."""
    if not value.strip():
        return ()
    try:
        return tuple(int(tok) for tok in value.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"--unwind-ladder must be a comma-separated list of ints, got {value!r}"
        ) from exc


def _add_check_parser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    p = sub.add_parser(
        "check",
        help="check a unit's stored properties with ESBMC, one verdict each",
        description=(
            "Read <source>::<function>'s stored, checkable properties (proposed by "
            "`forseti propose`), render each semantic one to a self-contained ESBMC "
            "harness, and verify it: held | violated | unknown | error, plus "
            "skipped for a deferred reachability property (ADR-0009 D2)."
        ),
    )
    _add_unit_store_arguments(p)
    p.add_argument(
        "-k",
        "--unwind",
        type=int,
        default=CHECK_DEFAULT_UNWIND,
        help=f"loop unwind bound k (default: {CHECK_DEFAULT_UNWIND})",
    )
    p.add_argument(
        "--unwind-ladder",
        type=_parse_ladder,
        default=None,
        metavar="K1,K2,...",
        help=(
            "comma-separated bounds tried after --unwind on an UNKNOWN verdict "
            f"(default: whichever of {','.join(map(str, CHECK_DEFAULT_UNWIND_LADDER))} "
            "exceed --unwind, so raising -k/--unwind alone still works)"
        ),
    )
    p.add_argument(
        "-t",
        "--timeout",
        type=float,
        default=CHECK_TIMEOUT_S,
        metavar="SECONDS",
        help=f"per-attempt esbmc timeout in seconds (default: {CHECK_TIMEOUT_S:g})",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="emit the check run as a JSON object",
    )
    add_esbmc_invocation_arguments(
        p,
        passthrough_help=(
            "flags forwarded verbatim to esbmc; place them after a `--` separator, "
            "e.g. `... file.c --function f -- -DNDEBUG`"
        ),
    )
    p.set_defaults(func=_run_check)


def _check_exit_code(run: PropertyCheckRun) -> int:
    """Worst-outcome-wins over every checked property, mirroring `EXIT_CODES`.

    `violated` beats `unknown` beats `error` beats a clean run (`held`/`skipped`
    only) — the same VIOLATED > UNKNOWN > ERROR > VERIFIED severity ordering a
    single-unit `forseti verify` uses, extended to "worst verdict across every
    property this run checked" rather than one. A run makes exactly one of the
    four counts decisive, in that order, so it can never under-report a
    violation because an unrelated property also came back unknown/error.
    """
    counts = run.counts()
    if counts["violated"]:
        return EXIT_CODES[Verdict.VIOLATED]
    if counts["unknown"]:
        return EXIT_CODES[Verdict.UNKNOWN]
    if counts["error"]:
        return EXIT_CODES[Verdict.ERROR]
    return EXIT_CODES[Verdict.VERIFIED]


def _render_check(run: PropertyCheckRun) -> str:
    """Human-readable check output: a loud headline for "nothing was actually
    semantically checked", then the full per-property transcript.

    An empty run (no stored properties at all) and an all-`skipped` run (every
    stored property is reachability-kind, deferred per ADR-0009 D2) both read as
    a clean 0 counts otherwise indistinguishable from "every property held" —
    CLAUDE.md "never silently pass" applies to the report, not just the exit
    code.
    """
    counts = run.counts()
    total = len(run.verdicts)
    checked = total - counts["skipped"]
    lines: list[str] = []
    if total == 0:
        lines.append(
            f"No properties stored for {run.unit_id} -- nothing was checked. "
            "Run `forseti propose` first."
        )
    elif checked == 0:
        lines.append(
            f"{total} stored propert{'y' if total == 1 else 'ies'} for "
            f"{run.unit_id} -- all reachability-kind (deferred, ADR-0009 D2); "
            "no semantic property was actually checked."
        )
    lines.append(property_check_transcript(run))
    return "\n".join(lines)


def _run_check(args: argparse.Namespace) -> int:
    unwind_ladder = args.unwind_ladder
    if unwind_ladder is None:
        # --unwind-ladder wasn't given: derive it from the chosen --unwind so
        # `-k 8` (or higher) doesn't collide with the fixed default rungs and
        # raise ValueError below (issue #95 review) -- an explicit
        # --unwind-ladder (including "" -> ()) always passes through as-is.
        unwind_ladder = tuple(k for k in CHECK_DEFAULT_UNWIND_LADDER if k > args.unwind)
    try:
        result = check_source(
            args.source,
            function=args.function,
            store_root=args.store_root,
            unwind=args.unwind,
            unwind_ladder=unwind_ladder,
            timeout_s=args.timeout,
            extra_flags=tuple(args.esbmc_args),
            esbmc_bin=args.esbmc_bin,
        )
    except (PropertyStoreError, OSError, ValueError) as exc:
        print(f"forseti check: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result.to_dict()))
    else:
        print(_render_check(result))
    return _check_exit_code(result)


def _add_claude_code_hook_parser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    p = sub.add_parser(
        "claude-code-hook",
        help="run a Claude Code verify-gate hook (internal; wired by enable-project)",
        description=(
            "Dispatch to one of the Claude Code adapter's hook handlers, reading "
            "the hook JSON payload from stdin the way Claude Code's hook protocol "
            "expects. Not meant to be invoked by hand -- `forseti enable-project` "
            "wires these into a project's settings file (RFC-0004)."
        ),
    )
    p.add_argument("name", choices=sorted(HOOK_NAMES), help="which hook to run")
    p.set_defaults(func=_run_claude_code_hook)


def _run_claude_code_hook(args: argparse.Namespace) -> int:
    # Imported lazily, one module per invocation: each hook fires as its own
    # short-lived `forseti claude-code-hook <name>` process (potentially
    # hundreds per session), and the gate itself shells out to `forseti
    # verify`/`list-units` once per unit -- neither path should pay to import
    # the other three hook modules it never calls.
    if args.name == "session-start":
        from forseti.adapters.claude_code import session_start

        return session_start.main()
    if args.name == "post-tool-use":
        from forseti.adapters.claude_code import post_tool_use

        return post_tool_use.main()
    if args.name == "post-bash":
        from forseti.adapters.claude_code import post_bash

        return post_bash.main()
    if args.name == "stop-gate":
        from forseti.adapters.claude_code import stop_gate

        return stop_gate.main()
    raise AssertionError(f"unreachable: unknown hook {args.name!r} (argparse choices)")


def _add_enable_project_parser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    p = sub.add_parser(
        "enable-project",
        help="install/update the Claude Code verify-gate hooks for a project",
        description=(
            "Write (or idempotently update) the Claude Code adapter's "
            "SessionStart/PostToolUse/Stop hook entries into a project's Claude "
            "Code settings file. Always regenerates forseti's own hook entries "
            "from the currently installed forseti version; every other hook or "
            "key already in the file is left untouched (RFC-0004)."
        ),
    )
    p.add_argument(
        "project_dir",
        type=Path,
        nargs="?",
        default=Path("."),
        help="the project root (default: the current directory)",
    )
    p.add_argument(
        "--shared",
        action="store_true",
        help=(
            "write to .claude/settings.json (git-committed, team-wide) instead "
            "of the default .claude/settings.local.json (gitignored, personal)"
        ),
    )
    p.set_defaults(func=_run_enable_project)


def _run_enable_project(args: argparse.Namespace) -> int:
    try:
        settings_path, outcome = install(args.project_dir, shared=args.shared)
    except (ProjectSettingsError, OSError) as exc:
        print(f"forseti enable-project: {exc}", file=sys.stderr)
        return 1
    message = f"Claude Code verify-gate hooks {outcome.value} at {settings_path}"
    print(f"forseti enable-project: {message}")
    return 0


def _add_mcp_parser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    p = sub.add_parser(
        "mcp",
        help="start the Core MCP server on stdio (needs the 'mcp' extra)",
        description="Expose Forseti Core's tools (currently `verify`) over MCP/stdio.",
    )
    p.set_defaults(func=_run_mcp)


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
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {installed_version() or 'unknown'}",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    # Each `_add_*` helper binds its own handler via `set_defaults(func=...)`, so
    # registering a subcommand *is* wiring its dispatch — there is no parallel
    # name->function table for a new subcommand to fall out of sync with.
    _add_verify_parser(sub)
    _add_list_units_parser(sub)
    _add_synth_parser(sub)
    _add_discharge_parser(sub)
    _add_propose_parser(sub)
    _add_check_parser(sub)
    _add_claude_code_hook_parser(sub)
    _add_enable_project_parser(sub)
    _add_mcp_parser(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments[0] not in {"claude-code-hook", "mcp"}:
        notice = update_notice()
        if notice is not None:
            print(notice, file=sys.stderr)
    args = _build_parser().parse_args(arguments)
    # `required=True` guarantees a subcommand (and thus a bound `func`) was
    # parsed, or argparse exits before we get here.
    handler: Callable[[argparse.Namespace], int] = args.func
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())

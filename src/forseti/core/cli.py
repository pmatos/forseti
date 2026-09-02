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
- ``forseti submit-property <source> --function NAME --expression EXPR
  --provider NAME --model NAME`` — ingest one already-formed candidate property
  through the exact same static validation `propose` applies, with no LLM call
  (#213): for a host harness, or a configured non-``claude -p`` proposer, that
  generates its own candidates. Exit 0 iff the candidate was accepted, 1 if it
  was rejected or the run itself failed (store/IO) — never a silent no-op.
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
- ``forseti codex-hook <name>`` — dispatch to the Codex adapter's
  ``PostToolUse`` verify-gate hook (#212). Internal: wired into a project's
  ``.codex/config.toml`` by ``enable-project --harness codex``.
- ``forseti enable-project [DIR] [--harness codex|claude-code] [--shared]`` —
  install/update the verify-gate hooks for one harness (RFC-0004, #212).
  ``--harness`` defaults to auto-detection from the session's own env vars
  (Codex's ``CODEX_SESSION_ID``/``CODEX_THREAD_ID``, Claude Code's
  ``CLAUDECODE``); if that's ambiguous or the markers are absent, the command
  fails rather than guessing. Idempotent: always regenerates forseti's own
  hook entries from the currently installed version, leaving every other
  hook/key in the target file untouched. Codex installs never create
  ``.claude/``, and vice versa. Codex's project-local config cannot carry a
  ``notify`` key (Codex 0.148); forseti never emits one.
- ``forseti disable-project [DIR] --harness codex|claude-code [--shared]`` —
  migration cleanup: remove *only* forseti's own hook entries for one harness
  (e.g. after ``enable-project`` targeted the wrong one), leaving foreign
  hooks and unrelated keys untouched. Requires an explicit ``--harness``;
  never auto-detects, and never creates a file that wasn't already there.
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
from enum import Enum
from pathlib import Path

from forseti.adapters.claude_code import install as claude_code_install
from forseti.adapters.claude_code.install import (
    HOOK_NAMES as CLAUDE_CODE_HOOK_NAMES,
)
from forseti.adapters.codex import install as codex_install
from forseti.adapters.codex import verify_hook as codex_verify_hook
from forseti.adapters.codex.install import HOOK_NAMES as CODEX_HOOK_NAMES
from forseti.adapters.harness import Harness, detect_harness
from forseti.esbmc import (
    ListUnitsError,
    Verdict,
    add_esbmc_invocation_arguments,
    add_verify_arguments,
    list_units,
    render_result,
    verify_kwargs,
)
from forseti.orchestrator import PropertyCheckRun, property_check_transcript
from forseti.properties import (
    BlankProvenanceError,
    CandidateSpec,
    LLMError,
    PropertyStoreError,
    ProposalParseError,
    ProposalResult,
)
from forseti.update_notice import installed_version, update_notice

from . import EXIT_CODES

# The synth/discharge glue lives in its own module (RFC-0003 S2/S3), but the
# dispatch contract resolves each subcommand's handler as `cli._run_<name>`
# (test_core_cli_dispatch) and `_build_parser` calls `_add_*_parser` here, so
# both are re-exported into this namespace. `_run_synth`/`_run_discharge` are
# referenced only via that getattr, hence noqa: F401 — dropping them would break
# dispatch silently (pinned by test_precond_cli's identity assertions).
from ._precond_cli import (
    _add_discharge_parser,
    _add_synth_parser,
    _run_discharge,  # noqa: F401
    _run_synth,  # noqa: F401
)
from .check import (
    DEFAULT_TIMEOUT_S as CHECK_TIMEOUT_S,
)
from .check import (
    DEFAULT_UNWIND as CHECK_DEFAULT_UNWIND,
)
from .check import (
    DEFAULT_UNWIND_LADDER as CHECK_DEFAULT_UNWIND_LADDER,
)
from .check import check_source, default_unwind_ladder_above
from .loop import LoopMode, SemanticLoopResult, run_semantic_loop
from .propose import (
    DEFAULT_MAX_CANDIDATES,
    DEFAULT_MODEL,
    DEFAULT_STORE_ROOT,
    propose_source,
)
from .propose import (
    DEFAULT_TIMEOUT_S as PROPOSE_TIMEOUT_S,
)
from .submit import DEFAULT_PROMPT_ID as SUBMIT_DEFAULT_PROMPT_ID
from .submit import DEFAULT_PROMPT_VERSION as SUBMIT_DEFAULT_PROMPT_VERSION
from .submit import submit_source
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


def _add_submit_property_parser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    p = sub.add_parser(
        "submit-property",
        help="ingest a host-generated candidate property -- no LLM call",
        description=(
            "Validate and store one already-formed candidate property for "
            "<source>::<function>, the same static checks `forseti propose` "
            "applies, without invoking any LLM. For a host harness (or a "
            "configured non-claude proposer) that generates its own candidates."
        ),
    )
    _add_unit_store_arguments(p)
    p.add_argument(
        "--expression",
        required=True,
        help='the candidate\'s C boolean expression, e.g. "result >= 0"',
    )
    p.add_argument(
        "--domain",
        action="append",
        default=[],
        metavar="EXPR",
        help=(
            "a precondition over the parameters (repeatable); emitted as "
            "__ESBMC_assume(...) before the call"
        ),
    )
    p.add_argument(
        "--referenced-param",
        dest="referenced_params",
        action="append",
        default=[],
        metavar="NAME",
        help="a parameter name --expression references (repeatable)",
    )
    p.add_argument(
        "--rationale",
        default="",
        help="free-text rationale, stored as the property's description",
    )
    p.add_argument(
        "--provider",
        required=True,
        help='who/what produced this candidate, e.g. "codex" or "claude-code-subagent"',
    )
    p.add_argument(
        "--model",
        required=True,
        help='the model that produced this candidate, e.g. "gpt-5.1"',
    )
    p.add_argument(
        "--prompt-id",
        default=SUBMIT_DEFAULT_PROMPT_ID,
        help=(
            "provenance prompt id for this candidate "
            f"(default: {SUBMIT_DEFAULT_PROMPT_ID})"
        ),
    )
    p.add_argument(
        "--prompt-version",
        default=SUBMIT_DEFAULT_PROMPT_VERSION,
        help=(
            "provenance prompt version for this candidate "
            f"(default: {SUBMIT_DEFAULT_PROMPT_VERSION})"
        ),
    )
    p.add_argument(
        "--no-store",
        action="store_true",
        help="dry run: validate without writing to the store",
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
        help="emit the result as a JSON object (the MCP tool's payload)",
    )
    p.set_defaults(func=_run_submit_property)


def _run_submit_property(args: argparse.Namespace) -> int:
    try:
        result = submit_source(
            args.source,
            function=args.function,
            expression=args.expression,
            provider=args.provider,
            model=args.model,
            domain=args.domain,
            referenced_params=args.referenced_params,
            rationale=args.rationale,
            prompt_id=args.prompt_id,
            prompt_version=args.prompt_version,
            persist=not args.no_store,
            store_root=args.store_root,
            max_candidates=args.max_candidates,
        )
    except (BlankProvenanceError, PropertyStoreError, OSError) as exc:
        print(f"forseti submit-property: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result.to_dict()))
    else:
        print(_render_proposal(result))
    return 0 if result.accepted else 1


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


_OUTCOME_VERDICT = {
    "violated": Verdict.VIOLATED,
    "unknown": Verdict.UNKNOWN,
    "error": Verdict.ERROR,
    "empty": Verdict.VERIFIED,
    "held": Verdict.VERIFIED,
}


def _check_exit_code(run: PropertyCheckRun) -> int:
    """`run.outcome`'s exit code, mirroring `EXIT_CODES`.

    `run.outcome` is Core's own worst-outcome-wins policy (`PropertyCheckRun`,
    issue #213) — this only maps that one decision onto the same VIOLATED >
    UNKNOWN > ERROR > VERIFIED severity ordering a single-unit `forseti verify`
    uses, rather than re-deriving it from `counts()` here.
    """
    return EXIT_CODES[_OUTCOME_VERDICT[run.outcome]]


def _render_check(run: PropertyCheckRun) -> str:
    """Human-readable check output: a loud headline for "nothing was actually
    semantically checked", then the full per-property transcript.

    An empty run (no stored properties at all) and an all-`skipped` run (every
    stored property is reachability-kind, deferred per ADR-0009 D2) both settle
    `run.outcome == "empty"` (`PropertyCheckRun`) — otherwise indistinguishable
    from "every property held" by exit code alone. CLAUDE.md "never silently
    pass" applies to the report, not just the exit code.
    """
    total = len(run.verdicts)
    lines: list[str] = []
    if run.outcome == "empty":
        if total == 0:
            lines.append(
                f"No properties stored for {run.unit_id} -- nothing was checked. "
                "Run `forseti propose` first."
            )
        else:
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
        unwind_ladder = default_unwind_ladder_above(args.unwind)
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


def _add_semantic_loop_parser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    p = sub.add_parser(
        "semantic-loop",
        help="ingest candidates (per --mode), then check them with ESBMC -- one call",
        description=(
            "The composed semantic-property loop (#213): ingest candidate "
            "properties for <source>::<function> per --mode (propose from the "
            "LLM proposer, submit already-formed ones, or skip ingestion and "
            "check what's already stored), then check every stored, checkable "
            "property with ESBMC and report Core's own worst-outcome-wins "
            "`outcome`: held | violated | unknown | error | empty. Replaces "
            "calling `propose`/`submit-property` and `check` separately and "
            "re-deriving that decision yourself."
        ),
    )
    _add_unit_store_arguments(p)
    p.add_argument(
        "--mode",
        required=True,
        choices=("propose", "submit", "check-only"),
        help="how to ingest candidates before checking",
    )
    p.add_argument(
        "--candidates-json",
        type=Path,
        metavar="FILE",
        help=(
            "--mode submit only: a file containing a JSON array of candidate "
            'objects ({"expression": ..., "domain": [...], '
            '"referenced_params": [...], "rationale": ...}), each validated '
            "and persisted independently"
        ),
    )
    p.add_argument(
        "--provider",
        help='--mode submit only: who/what produced these candidates, e.g. "codex"',
    )
    p.add_argument(
        "--model",
        help=(
            "--mode submit only: the model that produced these candidates, "
            'e.g. "gpt-5.1"'
        ),
    )
    p.add_argument(
        "--prompt-id",
        default=SUBMIT_DEFAULT_PROMPT_ID,
        help=(
            "--mode submit only: provenance prompt id for these candidates "
            f"(default: {SUBMIT_DEFAULT_PROMPT_ID})"
        ),
    )
    p.add_argument(
        "--prompt-version",
        default=SUBMIT_DEFAULT_PROMPT_VERSION,
        help=(
            "--mode submit only: provenance prompt version for these candidates "
            f"(default: {SUBMIT_DEFAULT_PROMPT_VERSION})"
        ),
    )
    p.add_argument(
        "--propose-model",
        default=DEFAULT_MODEL,
        help=(
            "--mode propose only: LLM model for the proposer "
            f"(default: {DEFAULT_MODEL})"
        ),
    )
    p.add_argument(
        "--claude-bin",
        default="claude",
        help="--mode propose only: claude binary to invoke (default: claude on PATH)",
    )
    p.add_argument(
        "--propose-timeout",
        type=float,
        default=PROPOSE_TIMEOUT_S,
        metavar="SECONDS",
        help=(
            "--mode propose only: proposer LLM timeout in seconds "
            f"(default: {PROPOSE_TIMEOUT_S:g})"
        ),
    )
    p.add_argument(
        "--max-candidates",
        type=int,
        default=DEFAULT_MAX_CANDIDATES,
        metavar="N",
        help=f"cap on accepted candidates (default: {DEFAULT_MAX_CANDIDATES})",
    )
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
        help="emit the run as a JSON object (the MCP tool's payload)",
    )
    add_esbmc_invocation_arguments(
        p,
        passthrough_help=(
            "flags forwarded verbatim to esbmc; place them after a `--` separator, "
            "e.g. `... file.c --function f --mode check-only -- -DNDEBUG`"
        ),
    )
    p.set_defaults(func=_run_semantic_loop)


def _parse_candidates_json(path: Path) -> tuple[CandidateSpec, ...]:
    """A JSON array of candidate objects -> `CandidateSpec`s.

    Raises `ValueError` (malformed JSON, not a list, or a candidate missing
    `expression`) so the CLI can report one clean diagnostic rather than a
    bare `KeyError`/`TypeError` traceback.
    """
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path}: invalid JSON ({exc})") from exc
    if not isinstance(raw, list):
        raise ValueError(f"{path}: must contain a JSON array of candidate objects")
    candidates = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict) or "expression" not in entry:
            raise ValueError(f"{path}: candidate {index} is missing 'expression'")
        candidates.append(
            CandidateSpec(
                expression=entry["expression"],
                domain=tuple(entry.get("domain", ())),
                referenced_params=tuple(entry.get("referenced_params", ())),
                rationale=entry.get("rationale", ""),
            )
        )
    return tuple(candidates)


def _render_semantic_loop(result: SemanticLoopResult) -> str:
    lines = [_render_proposal(ingested) for ingested in result.ingestion]
    lines.append(_render_check(result.check))
    return "\n".join(lines)


def _run_semantic_loop(args: argparse.Namespace) -> int:
    candidates: tuple[CandidateSpec, ...] = ()
    if args.mode == "submit":
        if args.candidates_json is None:
            print(
                "forseti semantic-loop: --mode submit requires --candidates-json",
                file=sys.stderr,
            )
            return 1
        try:
            candidates = _parse_candidates_json(args.candidates_json)
        except (ValueError, OSError) as exc:
            print(f"forseti semantic-loop: {exc}", file=sys.stderr)
            return 1
    elif args.candidates_json is not None:
        # Caught here, not left to run_semantic_loop's own "does not take
        # candidates" ValueError: a --mode propose/check-only caller passing
        # --candidates-json anyway is a CLI-level mistake (the flag would
        # otherwise be silently ignored -- CLAUDE.md "never silently pass").
        print(
            f"forseti semantic-loop: --candidates-json is --mode submit only, "
            f"not --mode {args.mode}",
            file=sys.stderr,
        )
        return 1

    loop_mode: LoopMode
    if args.mode == "propose":
        loop_mode = "propose"
    elif args.mode == "submit":
        loop_mode = "submit"
    else:
        loop_mode = "check_only"

    unwind_ladder = args.unwind_ladder
    if unwind_ladder is None:
        unwind_ladder = default_unwind_ladder_above(args.unwind)

    try:
        result = run_semantic_loop(
            args.source,
            function=args.function,
            mode=loop_mode,
            store_root=args.store_root,
            max_candidates=args.max_candidates,
            candidates=candidates,
            provider=args.provider,
            model=args.model,
            prompt_id=args.prompt_id,
            prompt_version=args.prompt_version,
            propose_model=args.propose_model,
            claude_bin=args.claude_bin,
            propose_timeout_s=args.propose_timeout,
            unwind=args.unwind,
            unwind_ladder=unwind_ladder,
            check_timeout_s=args.timeout,
            extra_flags=tuple(args.esbmc_args),
            esbmc_bin=args.esbmc_bin,
        )
    except (
        ValueError,
        LLMError,
        ProposalParseError,
        BlankProvenanceError,
        PropertyStoreError,
        OSError,
    ) as exc:
        print(f"forseti semantic-loop: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result.to_dict()))
    else:
        print(_render_semantic_loop(result))
    return _check_exit_code(result.check)


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
    p.add_argument(
        "name", choices=sorted(CLAUDE_CODE_HOOK_NAMES), help="which hook to run"
    )
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


def _add_codex_hook_parser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    p = sub.add_parser(
        "codex-hook",
        help="run a Codex verify-gate hook (internal; wired by enable-project)",
        description=(
            "Dispatch to the Codex adapter's `PostToolUse` hook handler, reading "
            "the hook JSON payload from stdin the way Codex's hook protocol "
            "expects. Not meant to be invoked by hand -- `forseti enable-project "
            "--harness codex` wires this into a project's `.codex/config.toml` "
            "(#212)."
        ),
    )
    p.add_argument("name", choices=sorted(CODEX_HOOK_NAMES), help="which hook to run")
    p.set_defaults(func=_run_codex_hook)


def _run_codex_hook(args: argparse.Namespace) -> int:
    if args.name == "verify":
        return codex_verify_hook.main()
    raise AssertionError(f"unreachable: unknown hook {args.name!r} (argparse choices)")


def _add_enable_project_parser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    p = sub.add_parser(
        "enable-project",
        help="install/update the verify-gate hooks for a project's harness",
        description=(
            "Write (or idempotently update) the selected harness's verify-gate "
            "hook entries into a project's settings file: Claude Code's "
            "SessionStart/PostToolUse/Stop hooks into its settings file, or "
            "Codex's PostToolUse `apply_patch` gate into `.codex/config.toml`. "
            "Always regenerates forseti's own entries from the currently "
            "installed forseti version; every other hook or key already in the "
            "file is left untouched (RFC-0004, #212). Codex skips a freshly "
            "wired hook until you trust it: run `/hooks` in Codex and trust the "
            "PostToolUse entry."
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
        "--harness",
        choices=[h.value for h in Harness],
        default=None,
        help=(
            "which harness to install for; default: auto-detect from the "
            "session's own env vars (Codex's CODEX_SESSION_ID/CODEX_THREAD_ID, "
            "Claude Code's CLAUDECODE), failing rather than guessing if that's "
            "ambiguous or absent"
        ),
    )
    p.add_argument(
        "--shared",
        action="store_true",
        help=(
            "write to .claude/settings.json (git-committed, team-wide) instead "
            "of the default .claude/settings.local.json (gitignored, personal) "
            "-- claude-code only"
        ),
    )
    p.set_defaults(func=_run_enable_project)


def _run_harness_action(
    command: str,
    action: Callable[[], tuple[Path, Enum]],
    error_types: tuple[type[Exception], ...],
    success_message: Callable[[Path, Enum], str],
) -> int:
    """Run one adapter's install/remove `action`, printing forseti's uniform
    `"forseti {command}: ..."` success/error line. Shared by `enable-project`
    and `disable-project`'s per-harness branches, which differ only in which
    adapter function to call and how to phrase the outcome.
    """
    try:
        path, outcome = action()
    except error_types as exc:
        print(f"forseti {command}: {exc}", file=sys.stderr)
        return 1
    print(f"forseti {command}: {success_message(path, outcome)}")
    return 0


def _run_enable_project(args: argparse.Namespace) -> int:
    harness = args.harness
    if harness is None:
        detected = detect_harness()
        if detected is None:
            print(
                "forseti enable-project: could not determine the harness "
                "automatically (no unambiguous session env vars found); pass "
                "--harness codex or --harness claude-code",
                file=sys.stderr,
            )
            return 1
        harness = detected.value

    if harness == Harness.CODEX.value:
        if args.shared:
            print(
                "forseti enable-project: --shared has no effect for "
                "--harness codex (there is only one .codex/config.toml)",
                file=sys.stderr,
            )
            return 1
        return _run_harness_action(
            "enable-project",
            lambda: codex_install.install(args.project_dir),
            (codex_install.ProjectConfigError, OSError),
            lambda path, outcome: (
                f"Codex verify-gate hook {outcome.value} at {path} (harness: "
                "codex). Codex skips a hook until you trust it -- run `/hooks` "
                "in Codex and trust the PostToolUse entry."
            ),
        )

    return _run_harness_action(
        "enable-project",
        lambda: claude_code_install.install(args.project_dir, shared=args.shared),
        (claude_code_install.ProjectSettingsError, OSError),
        lambda path, outcome: (
            f"Claude Code verify-gate hooks {outcome.value} at {path} "
            "(harness: claude-code)"
        ),
    )


def _add_disable_project_parser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    p = sub.add_parser(
        "disable-project",
        help="remove forseti's own verify-gate hook entries for one harness",
        description=(
            "Migration cleanup: remove only forseti's own hook entries for the "
            "given --harness from a project (e.g. after `enable-project` "
            "targeted the wrong one), leaving foreign hooks and unrelated keys "
            "untouched. Never auto-detects the harness and never creates a file "
            "that wasn't already there (#212)."
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
        "--harness",
        choices=[h.value for h in Harness],
        required=True,
        help="which harness's forseti-owned entries to remove",
    )
    p.add_argument(
        "--shared",
        action="store_true",
        help=(
            "target .claude/settings.json instead of the default "
            ".claude/settings.local.json -- claude-code only"
        ),
    )
    p.set_defaults(func=_run_disable_project)


def _run_disable_project(args: argparse.Namespace) -> int:
    if args.harness == Harness.CODEX.value:
        if args.shared:
            print(
                "forseti disable-project: --shared has no effect for --harness codex",
                file=sys.stderr,
            )
            return 1
        return _run_harness_action(
            "disable-project",
            lambda: codex_install.remove(args.project_dir),
            (codex_install.ProjectConfigError, OSError),
            lambda path, outcome: f"Codex verify-gate hook {outcome.value} at {path}",
        )

    return _run_harness_action(
        "disable-project",
        lambda: claude_code_install.remove(args.project_dir, shared=args.shared),
        (claude_code_install.ProjectSettingsError, OSError),
        lambda path, outcome: (
            f"Claude Code verify-gate hooks {outcome.value} at {path}"
        ),
    )


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
    _add_submit_property_parser(sub)
    _add_check_parser(sub)
    _add_semantic_loop_parser(sub)
    _add_claude_code_hook_parser(sub)
    _add_codex_hook_parser(sub)
    _add_enable_project_parser(sub)
    _add_disable_project_parser(sub)
    _add_mcp_parser(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments[0] not in {"claude-code-hook", "codex-hook", "mcp"}:
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

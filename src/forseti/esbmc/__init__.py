"""Thin typed wrapper around the ESBMC bounded model checker.

`verify` runs ESBMC on a source and returns one of the sealed `EsbmcResult`
variants: `Verified | Violated | Unknown | Error`. A `Violated` carries both the
raw counterexample text and a typed `Counterexample` parsed by a frontend-aware
parser (C first).
"""

from .cex_parser import Frontend, parse_counterexample
from .counterexample import (
    Assignment,
    Counterexample,
    SourceLoc,
    Step,
    ViolatedProperty,
)
from .render import EXIT_CODES, render_result, result_to_dict
from .result import (
    Error,
    EsbmcResult,
    RunMeta,
    Unknown,
    UnknownReason,
    Verdict,
    Verified,
    Violated,
)
from .runner import VERIFY_GRACE_S, build_argv, classify, verify
from .units import (
    CallerOpenings,
    ListUnitsError,
    Param,
    Unit,
    find_definition_brace,
    list_caller_openings,
    list_units,
    mask_comments,
    parse_address_escapes,
    parse_asm_statements,
    parse_definitions,
    parse_external_callers,
    parse_implicit_invocations,
    parse_symbol_aliases,
    parse_units,
    rename_all_declarations_and_definitions,
)
from .verify_cli import (
    add_esbmc_invocation_arguments,
    add_verify_arguments,
    verify_kwargs,
)

__all__ = [
    "EXIT_CODES",
    "VERIFY_GRACE_S",
    "Assignment",
    "CallerOpenings",
    "Counterexample",
    "Error",
    "EsbmcResult",
    "Frontend",
    "ListUnitsError",
    "Param",
    "RunMeta",
    "SourceLoc",
    "Step",
    "Unit",
    "Unknown",
    "UnknownReason",
    "Verdict",
    "Verified",
    "Violated",
    "ViolatedProperty",
    "add_esbmc_invocation_arguments",
    "add_verify_arguments",
    "build_argv",
    "classify",
    "find_definition_brace",
    "list_caller_openings",
    "list_units",
    "mask_comments",
    "parse_address_escapes",
    "parse_asm_statements",
    "parse_counterexample",
    "parse_definitions",
    "parse_external_callers",
    "parse_implicit_invocations",
    "parse_symbol_aliases",
    "parse_units",
    "rename_all_declarations_and_definitions",
    "render_result",
    "result_to_dict",
    "verify",
    "verify_kwargs",
]

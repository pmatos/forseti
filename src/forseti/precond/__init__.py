"""Signature-driven memory-precondition synthesis and verification (RFC-0003 S2).

A structural, LLM-free path — kept separate from the functional-property
proposer (RFC-0003 D1). `synth` reads an L0 memory precondition off a unit's type
signature and renders a sidecar C harness; the verify driver (added next) runs it
with unwinding assertions on + a k-ladder + a non-vacuity check and reports an
*honestly labelled* verdict ("VERIFIED assuming valid caller pointers").
"""

from .synth import (
    DEFAULT_INCLUDES,
    DEFAULT_MAX_LEN,
    NON_VACUITY_LABEL,
    ParamPlan,
    ParamRole,
    SynthError,
    UnitPlan,
    plan_unit,
    render_sidecar,
)
from .verify import (
    ASSESSMENT_EXIT_CODES,
    DEFAULT_LADDER_CAP,
    DEFAULT_TIMEOUT_S,
    Assessment,
    PreconditionResult,
    PreconditionUnavailable,
    synthesize,
    verify_precondition,
)

__all__ = [
    "ASSESSMENT_EXIT_CODES",
    "DEFAULT_INCLUDES",
    "DEFAULT_LADDER_CAP",
    "DEFAULT_MAX_LEN",
    "DEFAULT_TIMEOUT_S",
    "NON_VACUITY_LABEL",
    "Assessment",
    "ParamPlan",
    "ParamRole",
    "PreconditionResult",
    "PreconditionUnavailable",
    "SynthError",
    "UnitPlan",
    "plan_unit",
    "render_sidecar",
    "synthesize",
    "verify_precondition",
]

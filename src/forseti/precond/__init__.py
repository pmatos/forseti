"""Signature-driven memory-precondition synthesis and verification (RFC-0003).

A structural, LLM-free path — kept separate from the functional-property
proposer (RFC-0003 D1). `synth` reads an L0 memory precondition off a unit's type
signature and renders both a sidecar C harness (which **assumes** it) and an
obligation-injected copy of the translation unit (which **checks** it). The
verify driver (S2) runs the sidecar with unwinding assertions on + a k-ladder +
a non-vacuity check and reports an *honestly labelled* verdict ("VERIFIED
assuming valid caller pointers"); the discharge driver (S3) then checks the
obligation at every caller in the TU and upgrades that verdict to *discharged*
only when every caller was checked and every check passed.
"""

from .discharge import (
    discharge_precondition,
    emit_obligations,
)
from .model import (
    CallerCheck,
    CallerOutcome,
    DischargeResult,
)
from .synth import (
    DEFAULT_INCLUDES,
    DEFAULT_MAX_LEN,
    NON_VACUITY_LABEL,
    OBLIGATION_LABEL_PREFIX,
    OBLIGATION_SITE_LABEL_PREFIX,
    ParamPlan,
    ParamRole,
    SynthError,
    UnitPlan,
    inject_obligations,
    obligation_expr,
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
    "OBLIGATION_LABEL_PREFIX",
    "OBLIGATION_SITE_LABEL_PREFIX",
    "Assessment",
    "CallerCheck",
    "CallerOutcome",
    "DischargeResult",
    "ParamPlan",
    "ParamRole",
    "PreconditionResult",
    "PreconditionUnavailable",
    "SynthError",
    "UnitPlan",
    "discharge_precondition",
    "emit_obligations",
    "inject_obligations",
    "obligation_expr",
    "plan_unit",
    "render_sidecar",
    "synthesize",
    "verify_precondition",
]

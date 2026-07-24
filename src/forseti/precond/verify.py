"""Verify a unit against its synthesised memory precondition (RFC-0003 S2).

Ties the pieces together: read the unit's signature (`list_units`), synthesise a
sidecar harness (`synth`), and verify it **with unwinding assertions on** (so a
short bound is a distinct *unwinding assertion* failure, not a fake proof),
**force-malloc-success** (so the materialised object is actually reached), a
**k-ladder** (climb the bound until the verdict settles), and a **non-vacuity
check** (a reachable call site, so a VERIFIED is not a contradictory-assumption
"proof"). The verdict is **honestly labelled**: a pass here is "VERIFIED
*assuming* valid caller pointers" — an undischarged precondition (discharge is
S3), never a full verdict.

Soundness posture (RFC-0003): the classification of what is checked is
**signature-based**, never counterexample-text-based — we materialise a valid
object, we never pattern-match "dereference failure" to silence it. An
under-unwound loop and a real out-of-bounds both print FAILED, but they are told
apart structurally (the violated *property* is an "unwinding assertion" vs a
memory check): the former escalates the ladder, only the latter is a VIOLATED.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from forseti.esbmc import (
    EsbmcResult,
    ListUnitsError,
    Unit,
    Unknown,
    UnknownReason,
    Verified,
    Violated,
    list_units,
    verify,
)
from forseti.orchestrator.ladder import validated_ladder, verify_ladder
from forseti.orchestrator.ports import VerifyPort

from .synth import (
    DEFAULT_MAX_LEN,
    NON_VACUITY_LABEL,
    UnitPlan,
    plan_unit,
    render_sidecar,
)

# Per-run esbmc budget. Heavier than the scalar gate (these harnesses unwind real
# loops), so a comfortable default; the caller can lower it.
DEFAULT_TIMEOUT_S = 60.0

# The top rung of the k-ladder. The ladder climbs from `max_len + 1` (enough for
# the symbolic-length loop) doubling up to this cap, so a unit with a larger
# *internal* fixed loop (e.g. SHA-1's 80-round transform) still reaches a bound
# that fully unwinds it; a loop past the cap settles as an honest UNKNOWN.
DEFAULT_LADDER_CAP = 256

# esbmc flags the sidecar path always adds: materialise the object unconditionally
# (a NULL-returning malloc + guarded early-return would be a real-but-useless pass).
_FORCE_MALLOC = ("--force-malloc-success",)


class Assessment(Enum):
    """The honestly-labelled outcome of a memory-precondition verification."""

    ASSUMED_VERIFIED = "assumed_verified"  # VERIFIED under an undischarged precond
    VIOLATED = "violated"  # a real memory counterexample under the precond
    VACUOUS = "vacuous"  # the call site was unreachable — not a pass
    UNKNOWN = "unknown"  # ladder exhausted / non-vacuity inconclusive
    NEEDS_CONTRACT = "needs_contract"  # L0 could not synthesise a precondition (L2)
    ERROR = "error"  # tooling failure (no such unit, bad parse, ...)


@dataclass(frozen=True)
class PreconditionResult:
    """The verdict plus the provenance that keeps it honest."""

    function: str
    assessment: Assessment
    detail: str
    settled_k: int | None = None
    max_len: int | None = None
    esbmc_result: EsbmcResult | None = None
    plan: UnitPlan | None = None

    @property
    def label(self) -> str:
        """The one-line honest verdict."""
        if self.assessment is Assessment.ASSUMED_VERIFIED:
            return (
                "VERIFIED (assuming valid caller pointers — undischarged; "
                f"up to k={self.settled_k}, len<={self.max_len})"
            )
        if self.assessment is Assessment.VIOLATED:
            return (
                "VIOLATED (real counterexample under the synthesised "
                f"precondition, k={self.settled_k})"
            )
        if self.assessment is Assessment.VACUOUS:
            return (
                "VACUOUS (synthesised precondition makes the call unreachable "
                "— not a pass)"
            )
        if self.assessment is Assessment.NEEDS_CONTRACT:
            return f"NEEDS_CONTRACT ({self.detail})"
        if self.assessment is Assessment.UNKNOWN:
            return f"UNKNOWN ({self.detail})"
        return f"ERROR ({self.detail})"

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "function": self.function,
            "assessment": self.assessment.value,
            "assumed": self.assessment is Assessment.ASSUMED_VERIFIED,
            "detail": self.detail,
            "settled_k": self.settled_k,
            "max_len": self.max_len,
        }
        if self.plan is not None:
            payload["params"] = [
                {"name": p.var, "type": p.param.type, "role": p.role.value}
                for p in self.plan.params
            ]
        if isinstance(self.esbmc_result, Violated):
            payload["counterexample"] = self.esbmc_result.raw_counterexample
        return payload


def _is_under_unwound(result: Violated) -> bool:
    """True iff the violation is an under-unwound loop, not a real memory bug.

    With unwinding assertions on, a bound below a loop's trip count fails an
    "unwinding assertion" property; a genuine out-of-bounds fails a memory
    property ("dereference failure", "array bounds violated") that never carries
    that phrase. esbmc reports the first violated property, and a reachable real
    bug is surfaced ahead of the loop's unwinding assertion — so this phrase means
    "explore further at a higher k", exactly the ladder's escalate signal.
    """
    return "unwinding assertion" in result.raw_counterexample


def _escalating_port(raw: VerifyPort) -> VerifyPort:
    """Wrap `raw` so an under-unwound FAILED becomes an escalate-the-ladder UNKNOWN."""

    def port(source: Path, *, unwind: int) -> EsbmcResult:
        result = raw(source, unwind=unwind)
        if isinstance(result, Violated) and _is_under_unwound(result):
            return Unknown(result.meta, UnknownReason.UNDER_UNWOUND)
        return result

    return port


def _default_raw_verify(*, timeout_s: float, esbmc_bin: str) -> VerifyPort:
    """The real sidecar verify: assertions on + force-malloc-success."""

    def raw(source: Path, *, unwind: int) -> EsbmcResult:
        return verify(
            source,
            unwind=unwind,
            timeout_s=timeout_s,
            extra_flags=_FORCE_MALLOC,
            esbmc_bin=esbmc_bin,
            no_unwinding_assertions=False,
        )

    return raw


def _ladder(max_len: int, cap: int) -> tuple[int, ...]:
    """`max_len + 1` doubling up to `cap` — strictly increasing, `cap` included."""
    rungs = [max_len + 1]
    while rungs[-1] < cap:
        rungs.append(min(rungs[-1] * 2, cap))
    return validated_ladder(rungs[0], tuple(rungs[1:]))


class PreconditionUnavailable(Exception):
    """A precondition could not be synthesised (no such unit / main / L2).

    Carries the `Assessment` and human `detail` so both `verify_precondition`
    (which turns it into a `PreconditionResult`) and `synthesize`/the CLI (which
    turn it into an exit code) report the *same* reason.
    """

    def __init__(
        self, assessment: Assessment, detail: str, plan: UnitPlan | None = None
    ) -> None:
        super().__init__(detail)
        self.assessment = assessment
        self.detail = detail
        self.plan = plan


def _plan_for(
    source: Path, function: str, lister: Callable[[Path], list[Unit]]
) -> UnitPlan:
    """Resolve `source::function` to a materialisable plan, or raise.

    The shared front half of both entry points: list the units, refuse a source
    that defines ``main`` (the sidecar ``#include``\\ s it), require the function
    to exist, and require every pointer to have an L0 plan (else ``NEEDS_CONTRACT``).
    """
    try:
        units = {u.name: u for u in lister(source)}
    except ListUnitsError as exc:
        raise PreconditionUnavailable(Assessment.ERROR, str(exc)) from exc
    if "main" in units:
        raise PreconditionUnavailable(
            Assessment.ERROR,
            f"{source} defines main(); the sidecar #includes the source, which "
            "would duplicate main",
        )
    unit = units.get(function)
    if unit is None:
        raise PreconditionUnavailable(
            Assessment.ERROR, f"no definition of {function}() in {source}"
        )
    plan = plan_unit(unit)
    if not plan.resolvable:
        raise PreconditionUnavailable(
            Assessment.NEEDS_CONTRACT,
            f"unresolved pointer parameter(s): {', '.join(plan.unresolved_params)}",
            plan,
        )
    return plan


# The CLI's assessment → process-status contract (parallels esbmc's EXIT_CODES).
# A pass is 0; every other outcome is a distinct non-zero status a shell/CI can
# branch on — an assumed-but-vacuous or unsynthesisable unit is never a silent 0.
ASSESSMENT_EXIT_CODES: dict[Assessment, int] = {
    Assessment.ASSUMED_VERIFIED: 0,
    Assessment.VIOLATED: 1,
    Assessment.UNKNOWN: 2,
    Assessment.ERROR: 3,
    Assessment.VACUOUS: 4,
    Assessment.NEEDS_CONTRACT: 5,
}


def verify_precondition(
    source: Path,
    *,
    function: str,
    max_len: int = DEFAULT_MAX_LEN,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    esbmc_bin: str = "esbmc",
    ladder_cap: int = DEFAULT_LADDER_CAP,
    work_dir: Path | None = None,
    raw_verify: VerifyPort | None = None,
    list_units_fn: Callable[[Path], list[Unit]] | None = None,
) -> PreconditionResult:
    """Synthesise a memory precondition for `source::function` and verify it.

    Returns a `PreconditionResult` with an honestly-labelled `assessment`. A unit
    with an unresolved pointer shape is ``NEEDS_CONTRACT`` (never rendered wrong);
    a source that defines ``main`` is refused (the sidecar ``#include``\\ s it).
    `raw_verify` and `list_units_fn` inject the two esbmc calls for tests;
    production uses `list_units` + assertions-on/force-malloc-success `verify`.
    When `work_dir` is None a temporary directory holds the generated harnesses
    (the sidecar ``#include``\\ s the source by absolute path, so it can live
    anywhere).
    """
    lister = list_units_fn or (lambda src: list_units(src, esbmc_bin=esbmc_bin))
    try:
        plan = _plan_for(source, function, lister)
    except PreconditionUnavailable as exc:
        return PreconditionResult(function, exc.assessment, exc.detail, plan=exc.plan)

    raw = raw_verify or _default_raw_verify(timeout_s=timeout_s, esbmc_bin=esbmc_bin)
    if work_dir is not None:
        return _run(source, function, plan, max_len, ladder_cap, raw, work_dir)
    with tempfile.TemporaryDirectory(prefix="forseti-precond-") as tmp:
        return _run(source, function, plan, max_len, ladder_cap, raw, Path(tmp))


def synthesize(
    source: Path,
    *,
    function: str,
    max_len: int = DEFAULT_MAX_LEN,
    esbmc_bin: str = "esbmc",
    list_units_fn: Callable[[Path], list[Unit]] | None = None,
) -> str:
    """Return the sidecar C harness text for `source::function` (no ESBMC verify).

    The pure render path exposed for inspection (``forseti synth --emit-only``).
    Raises `PreconditionUnavailable` when the unit is missing, the source defines
    ``main``, or a pointer shape is unresolved (L2) — the caller maps that to an
    exit code, never a silent empty emit.
    """
    lister = list_units_fn or (lambda src: list_units(src, esbmc_bin=esbmc_bin))
    plan = _plan_for(source, function, lister)
    return render_sidecar(plan, str(source.resolve()), max_len=max_len)


def _run(
    source: Path,
    function: str,
    plan: UnitPlan,
    max_len: int,
    ladder_cap: int,
    raw: VerifyPort,
    work_dir: Path,
) -> PreconditionResult:
    include = str(source.resolve())
    primary = work_dir / f"{function}__precond.c"
    primary.write_text(render_sidecar(plan, include, max_len=max_len))

    ladder = _ladder(max_len, ladder_cap)
    port = _escalating_port(raw)
    settled = None
    for attempt in verify_ladder(primary, verify=port, ladder=ladder):
        settled = attempt
    assert settled is not None  # verify_ladder yields at least one attempt

    result, k = settled.result, settled.k

    def outcome(
        assessment: Assessment, detail: str, evidence: EsbmcResult
    ) -> PreconditionResult:
        return PreconditionResult(
            function,
            assessment,
            detail,
            settled_k=k,
            max_len=max_len,
            esbmc_result=evidence,
            plan=plan,
        )

    if isinstance(result, Violated):
        return outcome(
            Assessment.VIOLATED,
            "a pointer access is out of bounds under the synthesised precondition",
            result,
        )
    if isinstance(result, Unknown):
        return outcome(
            Assessment.UNKNOWN,
            f"inconclusive up to k={k} (reason: {result.reason.value})",
            result,
        )
    if not isinstance(result, Verified):  # Error
        return outcome(Assessment.ERROR, result.message, result)

    # VERIFIED — now discharge non-vacuity at the settled bound.
    return _assess_non_vacuity(
        source, function, plan, max_len, k, raw, work_dir, result
    )


def _assess_non_vacuity(
    source: Path,
    function: str,
    plan: UnitPlan,
    max_len: int,
    k: int,
    raw: VerifyPort,
    work_dir: Path,
    primary_result: Verified,
) -> PreconditionResult:
    """A VERIFIED is only ASSUMED_VERIFIED if the call site is reachable."""
    nv = work_dir / f"{function}__precond_nonvacuity.c"
    nv.write_text(
        render_sidecar(plan, str(source.resolve()), max_len=max_len, non_vacuity=True)
    )
    probe = raw(nv, unwind=k)

    def outcome(assessment: Assessment, detail: str) -> PreconditionResult:
        return PreconditionResult(
            function,
            assessment,
            detail,
            settled_k=k,
            max_len=max_len,
            esbmc_result=primary_result,
            plan=plan,
        )

    if isinstance(probe, Violated) and NON_VACUITY_LABEL in probe.raw_counterexample:
        return outcome(
            Assessment.ASSUMED_VERIFIED,
            "verified up to k assuming valid caller pointers; call site reachable",
        )
    if isinstance(probe, Verified):
        return outcome(
            Assessment.VACUOUS,
            "the assert-at-call-site was unreachable: the synthesised precondition "
            "is contradictory (a vacuous proof)",
        )
    return outcome(
        Assessment.UNKNOWN,
        "could not confirm call-site reachability "
        f"(non-vacuity probe: {probe.verdict.value})",
    )

"""How a precond sidecar harness is produced and run (RFC-0003 S2/S3).

Both precond drivers run the same recipe: render a sidecar for a `UnitPlan`,
write it into the work directory under a caller-chosen stem, hand the file to
ESBMC. They run it in **two different modes**, and the difference is
load-bearing:

* `SidecarRunner.climb` walks the k-ladder through `escalating_port`, so an
  under-unwound FAILED escalates the bound instead of reading as a real
  counterexample.
* `SidecarRunner.probe` runs the labelled ``assert(0)`` **once, unescalated**,
  at the bound the climb already settled on. A probe's VIOLATED *is* its success
  signal (the assert fired, so the site was reached); routing it through
  `escalating_port` would remap a trace that also mentions "unwinding assertion"
  into an `Unknown`, silently demoting a reachable site to `INCONCLUSIVE` — and
  every caller fails closed on that, so the demotion is indistinguishable from an
  honest inconclusive.

`escalating_port` therefore appears in exactly one function body in this package,
and `probe` has no parameter that could reach it. Taking `at: SettledRun` rather
than a bare `k: int` is the same idea applied to the bound: a probe cannot name a
bound the ladder never settled on.

Nothing here decides what a verdict *means*. `climb` hands back the settled
`EsbmcResult` and `probe` hands back a `ProbeReachability`; each driver maps those
onto its own vocabulary (`Assessment` in `verify.py`, `CallerOutcome` in
`discharge.py`). This module owns *how a sidecar is run*, never *what the answer
is worth*.

It is also the one home for the two policies that decide what "run" means —
`escalating_port` (the under-unwound remap) and `precondition_ladder` (the rung
shape) — which lived in `verify.py` until the recipe was extracted here.
`verify.py` imports this module, so they cannot live there and be used here.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from forseti.esbmc import EsbmcResult, Unknown, UnknownReason, Violated
from forseti.orchestrator.ladder import climb_to_terminal, validated_ladder
from forseti.orchestrator.ports import VerifyPort

from .reachability import ProbeReachability, classify_site_probe
from .synth import (
    NON_VACUITY_LABEL,
    OBLIGATION_SITE_LABEL_PREFIX,
    UnitPlan,
    render_sidecar,
)


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


def escalating_port(raw: VerifyPort) -> VerifyPort:
    """Wrap `raw` so an under-unwound FAILED becomes an escalate-the-ladder UNKNOWN."""

    def port(source: Path, *, unwind: int) -> EsbmcResult:
        result = raw(source, unwind=unwind)
        if isinstance(result, Violated) and _is_under_unwound(result):
            return Unknown(result.meta, UnknownReason.UNDER_UNWOUND)
        return result

    return port


def precondition_ladder(max_len: int, cap: int) -> tuple[int, ...]:
    """`max_len + 1` doubling up to `cap` — strictly increasing, `cap` included."""
    rungs = [max_len + 1]
    while rungs[-1] < cap:
        rungs.append(min(rungs[-1] * 2, cap))
    return validated_ladder(rungs[0], tuple(rungs[1:]))


class ProbeSite(Enum):
    """Which labelled ``assert(0)`` a site probe asks ESBMC to reach.

    The two arms are not independent knobs: *who emits the assert* and *which
    label reads it back* are one decision. `AFTER_SIDECAR_CALL`'s assert is
    emitted by ``render_sidecar(non_vacuity=True)`` into the harness ``main``
    after the call; `AT_OBLIGATION_ENTRY`'s is emitted by `inject_obligations`
    into the *included* translation-unit copy, at the callee's entry — so its
    sidecar is an ordinary one. Pairing them by hand is how a probe silently
    stops matching its own assert, which `classify_site_probe` reads as
    INCONCLUSIVE and every caller fails closed on; naming the pair makes the
    mismatch unrepresentable.
    """

    AFTER_SIDECAR_CALL = "after_sidecar_call"
    AT_OBLIGATION_ENTRY = "at_obligation_entry"

    @property
    def label(self) -> str:
        """The ``__ESBMC_assert`` label whose presence in the trace means REACHED.

        `AT_OBLIGATION_ENTRY` is deliberately the bare
        `OBLIGATION_SITE_LABEL_PREFIX`, not a callee-qualified label:
        `classify_site_probe` substring-matches, and the probe harness contains
        exactly one injected site, so the prefix is exact enough.
        """
        return (
            NON_VACUITY_LABEL
            if self is ProbeSite.AFTER_SIDECAR_CALL
            else OBLIGATION_SITE_LABEL_PREFIX
        )

    @property
    def non_vacuity(self) -> bool:
        """Whether `render_sidecar` is the thing that emits this probe's assert."""
        return self is ProbeSite.AFTER_SIDECAR_CALL


@dataclass(frozen=True)
class SettledRun:
    """The terminal rung of one harness's k-ladder: the verdict and its bound."""

    harness: Path
    result: EsbmcResult
    k: int


@dataclass(frozen=True)
class ProbeRun:
    """One site probe's reading, beside the raw verdict the reading came from.

    `result` is kept because an INCONCLUSIVE probe is reported by its raw verdict
    (``probe.result.verdict.value``) at both call sites: the reading is folded in,
    the evidence is not thrown away.
    """

    harness: Path
    reachability: ProbeReachability
    result: EsbmcResult


@dataclass(frozen=True)
class SidecarRunner:
    """What one driver run holds fixed across every sidecar it emits.

    Bound once per driver run by `sidecar_runner`. Frozen and stateless — it
    caches nothing and records nothing between calls, so two `climb`s on the same
    runner are independent.

    `plan` is deliberately **not** a field: it is the one thing that is not
    constant over a run. `discharge` re-plans per caller, so binding it would
    force a rebinding ceremony at exactly the call site this seam shortens.

    `raw` is the *unescalated* port: `climb` wraps it, `probe` does not.

    Lifetime: when `sidecar_runner` created `work_dir`, the runner is only valid
    inside that ``with`` block; afterwards `work_dir` no longer exists.
    """

    work_dir: Path
    max_len: int
    ladder_cap: int
    raw: VerifyPort

    @property
    def ladder(self) -> tuple[int, ...]:
        """The k-ladder every `climb` on this runner climbs."""
        return precondition_ladder(self.max_len, self.ladder_cap)

    def climb(self, *, stem: str, plan: UnitPlan, include: str) -> SettledRun:
        """Write `stem`'s sidecar and climb the ladder through the **escalating** port.

        `stem` is the emitted filename stem, used verbatim: the harness is
        ``work_dir / f"{stem}.c"`` and nothing derives a nicer name from it.
        `include` is forwarded opaquely as `render_sidecar`'s `source_include` —
        the resolved source at one site, an obligation-injected copy at another;
        this module never resolves, joins or inspects it.

        Raises `SynthError` (from `render_sidecar`) if `plan` is unresolvable, and
        `ValueError` if `max_len`/`ladder_cap` cannot form an increasing ladder.
        Whatever `raw` raises propagates unchanged.
        """
        harness = self._write(stem, plan, include)
        settled = climb_to_terminal(
            harness, verify=escalating_port(self.raw), ladder=self.ladder
        )
        return SettledRun(harness, settled.result, settled.k)

    def probe(
        self,
        *,
        stem: str,
        plan: UnitPlan,
        include: str,
        at: SettledRun,
        site: ProbeSite,
    ) -> ProbeRun:
        """Write `stem`'s probe sidecar and run it once, **unescalated**, at `at.k`.

        One call to the raw port — never the escalating one, and never a ladder:
        the bound is the one `at` already settled on, which is why `at` is a
        `SettledRun` rather than an `int` a caller could mis-supply. `site` names
        the assert and the label together (see `ProbeSite`).
        """
        harness = self._write(stem, plan, include, non_vacuity=site.non_vacuity)
        result = self.raw(harness, unwind=at.k)
        return ProbeRun(harness, classify_site_probe(result, label=site.label), result)

    def _write(
        self, stem: str, plan: UnitPlan, include: str, *, non_vacuity: bool = False
    ) -> Path:
        harness = self.work_dir / f"{stem}.c"
        harness.write_text(
            render_sidecar(plan, include, max_len=self.max_len, non_vacuity=non_vacuity)
        )
        return harness


@contextmanager
def sidecar_runner(
    *,
    work_dir: Path | None,
    max_len: int,
    ladder_cap: int,
    raw: VerifyPort,
    prefix: str,
) -> Iterator[SidecarRunner]:
    """Bind one driver run's sidecar context, owning a temp dir when needed.

    A caller-supplied `work_dir` is used as-is and **never removed** — it is the
    caller's directory, and a test inspects the harnesses in it after the run.
    With `work_dir=None` the generated harnesses live in a temporary directory
    that is removed on exit (the sidecar ``#include``\\ s its source by absolute
    path, so it can live anywhere).
    """
    if work_dir is not None:
        yield SidecarRunner(work_dir, max_len, ladder_cap, raw)
        return
    with tempfile.TemporaryDirectory(prefix=prefix) as tmp:
        yield SidecarRunner(Path(tmp), max_len, ladder_cap, raw)

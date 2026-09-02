"""Interpreting an ``__ESBMC_assert(0, "<label>")`` site probe into reachability.

The precond stack proves a call site is *reachable* by injecting a labelled
``assert(0)`` at it and asking ESBMC to reach it. The reading of the resulting
verdict is inverted from the usual one: a **VIOLATED** whose trace carries the
label is the *success* signal (the assert fired, so the site was reached),
while a **VERIFIED** means the assert never fired and the site is unreachable.

Both the non-vacuity check (`verify.py`) and the per-caller discharge
(`discharge.py`) run this same probe and read it the same way. This module is
the one home for that reading, so the inversion and the fail-closed default are
stated — and tested — once. `synth.py` is the matching single home for
*emitting* the probe.
"""

from __future__ import annotations

from enum import Enum

from forseti.esbmc import EsbmcResult, Verified, Violated


class ProbeReachability(Enum):
    """The outcome of one labelled site probe, in *this* harness at *its* bound k.

    Scoped deliberately: `REACHED` means the labelled ``assert(0)`` fired in the
    sidecar harness that was just run, **not** that the site is reachable from
    every caller in general — a callee's own self-harness trips its entry probe
    trivially (the PR #175 self-caller case). Callers map this onto their own
    domain vocabulary (`Assessment`, `CallerOutcome`).
    """

    REACHED = "reached"
    """A VIOLATED carrying the probe label: the ``assert(0)`` was reached."""

    UNREACHABLE = "unreachable"
    """A VERIFIED: the ``assert(0)`` was never reached in this harness."""

    INCONCLUSIVE = "inconclusive"
    """UNKNOWN, ERROR, or a VIOLATED *without* the label — no evidence either
    way. Callers must map this to UNKNOWN/UNCHECKED, never a pass."""


def classify_site_probe(result: EsbmcResult, *, label: str) -> ProbeReachability:
    """Read a site-probe verdict into `ProbeReachability`.

    `label` is the probe's ``__ESBMC_assert`` label; the match is a raw-trace
    substring (the caller's existing convention), not the typed model. An empty
    label would substring-match every trace, turning any VIOLATED into a false
    `REACHED` — an unsound `DISCHARGED`/`ASSUMED_VERIFIED` upgrade — so it is
    refused rather than silently accepted.
    """
    if not label:
        raise ValueError("classify_site_probe needs a non-empty probe label")
    if isinstance(result, Violated) and label in result.raw_counterexample:
        return ProbeReachability.REACHED
    if isinstance(result, Verified):
        return ProbeReachability.UNREACHABLE
    return ProbeReachability.INCONCLUSIVE

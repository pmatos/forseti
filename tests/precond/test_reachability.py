"""Tests for `forseti.precond.reachability` — the assert(0) site-probe interpreter.

The interpreter is a pure function over an `EsbmcResult`, so every arm is
exercised directly with a hand-built verdict — no ESBMC, no tempdir, no fake
port. This is the deepening's payoff: the `Violated`-with-label => REACHED
inversion, and the fail-closed collapse of the three "not a clean reach"
inputs (`Unknown`, `Error`, and an unlabelled `Violated`) into INCONCLUSIVE,
are pinned in one place instead of re-derived at each call site.
"""

from __future__ import annotations

import pytest

from forseti.esbmc import Error, RunMeta, Unknown, UnknownReason, Verified, Violated
from forseti.precond.reachability import ProbeReachability, classify_site_probe

LABEL = "forseti:probe-test"


def _meta() -> RunMeta:
    return RunMeta("8.3.0", ("esbmc",), 0, 0.0, "", "")


def test_labelled_violated_is_reached() -> None:
    """A VIOLATED whose trace carries the probe label = the assert(0) fired."""
    probe = Violated(_meta(), f"Violated property:\n  {LABEL}\n  assertion 0", None)
    assert classify_site_probe(probe, label=LABEL) is ProbeReachability.REACHED


def test_verified_is_unreachable() -> None:
    """A VERIFIED probe = the assert(0) never fired = the site is unreachable."""
    assert (
        classify_site_probe(Verified(_meta()), label=LABEL)
        is ProbeReachability.UNREACHABLE
    )


def test_unlabelled_violated_is_inconclusive() -> None:
    """The footgun: a VIOLATED without *our* label is not a reach — fail closed.

    A different property fired (a real memory bug in the harness, say); the
    site probe assert(0) was never reached, so we cannot claim REACHED.
    """
    probe = Violated(_meta(), "Violated property:\n  dereference failure", None)
    assert classify_site_probe(probe, label=LABEL) is ProbeReachability.INCONCLUSIVE


def test_unknown_is_inconclusive() -> None:
    """An UNKNOWN probe proves nothing about reachability — never a pass."""
    probe = Unknown(_meta(), UnknownReason.TIMEOUT)
    assert classify_site_probe(probe, label=LABEL) is ProbeReachability.INCONCLUSIVE


def test_error_is_inconclusive() -> None:
    """A tooling ERROR is not a verdict about the code — fail closed."""
    probe = Error(_meta(), "esbmc invocation failed")
    assert classify_site_probe(probe, label=LABEL) is ProbeReachability.INCONCLUSIVE


def test_empty_label_raises() -> None:
    """An empty label substring-matches every trace, turning any VIOLATED into a
    false REACHED (an unsound DISCHARGED / ASSUMED_VERIFIED upgrade). Refuse it."""
    probe = Violated(_meta(), "anything", None)
    with pytest.raises(ValueError, match="non-empty probe label"):
        classify_site_probe(probe, label="")

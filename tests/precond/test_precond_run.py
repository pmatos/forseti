"""Tests for `forseti.precond.run` — the sidecar run seam itself (no ESBMC).

The two precond drivers used to spell this recipe out twice each. These tests pin
the seam directly, and cover three things the driver suites structurally cannot:
they dispatch canned verdicts by sniffing the harness filename and **ignore
`unwind` entirely**, so nothing there can tell which bound a probe ran at, nor
whether the probe went through the escalating port or the raw one.
"""

from __future__ import annotations

from pathlib import Path

from forseti.esbmc import (
    EsbmcResult,
    RunMeta,
    Unit,
    Unknown,
    UnknownReason,
    Verified,
    Violated,
)
from forseti.esbmc.units import Param
from forseti.precond.reachability import ProbeReachability
from forseti.precond.run import (
    ProbeSite,
    SidecarRunner,
    sidecar_runner,
)
from forseti.precond.synth import (
    NON_VACUITY_LABEL,
    OBLIGATION_SITE_LABEL_PREFIX,
    plan_unit,
)

UPDATE = Unit(
    "sha1_update",
    (
        Param("ctx", "sha1_ctx *"),
        Param("data", "const uint8_t *"),
        Param("len", "unsigned long"),
    ),
)
PLAN = plan_unit(UPDATE)


def _meta() -> RunMeta:
    return RunMeta("8.3.0", ("esbmc",), 0, 0.0, "", "")


def _verified() -> Verified:
    return Verified(_meta())


def _violated(text: str) -> Violated:
    return Violated(_meta(), text, None)


def _unwinding() -> Violated:
    return _violated("Violated property:\n  unwinding assertion loop 4")


class _Recorder:
    """A `VerifyPort` that answers from `verdicts` and records every `(path, k)`."""

    def __init__(self, verdicts: list[EsbmcResult]) -> None:
        self._verdicts = list(verdicts)
        self.calls: list[tuple[Path, int]] = []

    def __call__(self, source: Path, *, unwind: int) -> EsbmcResult:
        self.calls.append((source, unwind))
        return self._verdicts.pop(0) if self._verdicts else _verified()


def _runner(
    tmp: Path, raw: _Recorder, *, max_len: int = 8, cap: int = 32
) -> SidecarRunner:
    return SidecarRunner(work_dir=tmp, max_len=max_len, ladder_cap=cap, raw=raw)


# --- the emitted harness ----------------------------------------------------


def test_climb_emits_the_stem_verbatim_under_work_dir(tmp_path: Path) -> None:
    raw = _Recorder([_verified()])
    settled = _runner(tmp_path, raw).climb(
        stem="sha1_update__precond", plan=PLAN, include="/abs/sha1.c"
    )
    assert settled.harness == tmp_path / "sha1_update__precond.c"
    assert settled.harness.exists()
    # The filenames are load-bearing: two driver test modules dispatch on them.
    assert raw.calls[0][0].name == "sha1_update__precond.c"


def test_include_is_forwarded_opaquely(tmp_path: Path) -> None:
    raw = _Recorder([_verified()])
    settled = _runner(tmp_path, raw).climb(
        stem="c__discharge", plan=PLAN, include="/tmp/x__obligations.c"
    )
    assert '#include "/tmp/x__obligations.c"' in settled.harness.read_text()


# --- the ladder -------------------------------------------------------------


def test_climb_walks_the_precondition_ladder(tmp_path: Path) -> None:
    raw = _Recorder([_unwinding(), _unwinding(), _verified()])
    settled = _runner(tmp_path, raw).climb(
        stem="u__precond", plan=PLAN, include="/abs/u.c"
    )
    # max_len=8 → rungs 9, 18, 32; settles at the third.
    assert [k for _p, k in raw.calls] == [9, 18, 32]
    assert settled.k == 32
    assert isinstance(settled.result, Verified)


def test_climb_escalates_an_under_unwound_violation(tmp_path: Path) -> None:
    """The laddered run goes through `escalating_port`: a FAILED that is only an
    under-unwound loop must escalate, not read as a real counterexample."""
    raw = _Recorder([_unwinding(), _verified()])
    settled = _runner(tmp_path, raw).climb(
        stem="u__precond", plan=PLAN, include="/abs/u.c"
    )
    assert [k for _p, k in raw.calls] == [9, 18]
    assert isinstance(settled.result, Verified)


def test_an_exhausted_ladder_settles_unknown_never_a_pass(tmp_path: Path) -> None:
    raw = _Recorder([_unwinding(), _unwinding(), _unwinding()])
    settled = _runner(tmp_path, raw).climb(
        stem="u__precond", plan=PLAN, include="/abs/u.c"
    )
    assert isinstance(settled.result, Unknown)
    assert settled.result.reason is UnknownReason.UNDER_UNWOUND
    assert settled.k == 32


# --- the probe --------------------------------------------------------------


def test_probe_runs_once_at_the_settled_bound(tmp_path: Path) -> None:
    raw = _Recorder([_unwinding(), _verified()])
    runner = _runner(tmp_path, raw)
    settled = runner.climb(stem="u__precond", plan=PLAN, include="/abs/u.c")
    raw.calls.clear()
    runner.probe(
        stem="u__precond_nonvacuity",
        plan=PLAN,
        include="/abs/u.c",
        at=settled,
        site=ProbeSite.AFTER_SIDECAR_CALL,
    )
    assert len(raw.calls) == 1
    assert raw.calls[0][1] == settled.k == 18


def test_probe_uses_the_raw_port_not_the_escalating_one(tmp_path: Path) -> None:
    """The one fixture that tells the two ports apart.

    A probe's VIOLATED *is* its success signal. This counterexample carries both
    the probe label and the phrase `escalating_port` remaps on, so:
    raw            → REACHED (the assert fired, the site is reachable)
    escalating_port → Unknown → INCONCLUSIVE (a reachable site silently demoted)
    A seam that routed the probe through the escalating port would pass every
    existing driver test and still be wrong here.
    """
    both = _violated(
        f"Violated property:\n  {NON_VACUITY_LABEL}\n  unwinding assertion loop 4"
    )
    raw = _Recorder([_verified(), both])
    runner = _runner(tmp_path, raw)
    settled = runner.climb(stem="u__precond", plan=PLAN, include="/abs/u.c")
    probe = runner.probe(
        stem="u__precond_nonvacuity",
        plan=PLAN,
        include="/abs/u.c",
        at=settled,
        site=ProbeSite.AFTER_SIDECAR_CALL,
    )
    assert probe.reachability is ProbeReachability.REACHED


def test_probe_keeps_the_raw_verdict_beside_the_reading(tmp_path: Path) -> None:
    raw = _Recorder([_verified(), Unknown(_meta(), UnknownReason.TIMEOUT)])
    runner = _runner(tmp_path, raw)
    settled = runner.climb(stem="u__precond", plan=PLAN, include="/abs/u.c")
    probe = runner.probe(
        stem="u__precond_nonvacuity",
        plan=PLAN,
        include="/abs/u.c",
        at=settled,
        site=ProbeSite.AFTER_SIDECAR_CALL,
    )
    assert probe.reachability is ProbeReachability.INCONCLUSIVE
    assert probe.result.verdict.value == "unknown"


# --- ProbeSite: the label and the emitter are one decision ------------------


def test_after_sidecar_call_emits_its_own_assert_and_reads_its_own_label() -> None:
    assert ProbeSite.AFTER_SIDECAR_CALL.label == NON_VACUITY_LABEL
    assert ProbeSite.AFTER_SIDECAR_CALL.non_vacuity is True


def test_at_obligation_entry_reads_the_injected_assert_it_does_not_emit_one() -> None:
    assert ProbeSite.AT_OBLIGATION_ENTRY.label == OBLIGATION_SITE_LABEL_PREFIX
    assert ProbeSite.AT_OBLIGATION_ENTRY.non_vacuity is False


def test_non_vacuity_probe_renders_the_assert_into_the_harness(tmp_path: Path) -> None:
    raw = _Recorder([_verified(), _verified()])
    runner = _runner(tmp_path, raw)
    settled = runner.climb(stem="u__precond", plan=PLAN, include="/abs/u.c")
    probe = runner.probe(
        stem="u__precond_nonvacuity",
        plan=PLAN,
        include="/abs/u.c",
        at=settled,
        site=ProbeSite.AFTER_SIDECAR_CALL,
    )
    assert NON_VACUITY_LABEL in probe.harness.read_text()


def test_obligation_site_probe_renders_an_ordinary_sidecar(tmp_path: Path) -> None:
    """Its assert is injected into the *included* TU copy, not the harness."""
    raw = _Recorder([_verified(), _verified()])
    runner = _runner(tmp_path, raw)
    settled = runner.climb(stem="c__discharge", plan=PLAN, include="/abs/o.c")
    probe = runner.probe(
        stem="c__discharge_site",
        plan=PLAN,
        include="/abs/site.c",
        at=settled,
        site=ProbeSite.AT_OBLIGATION_ENTRY,
    )
    text = probe.harness.read_text()
    assert NON_VACUITY_LABEL not in text
    assert '#include "/abs/site.c"' in text


# --- the binder -------------------------------------------------------------


def test_a_supplied_work_dir_survives_the_run(tmp_path: Path) -> None:
    raw = _Recorder([_verified()])
    with sidecar_runner(
        work_dir=tmp_path, max_len=8, ladder_cap=32, raw=raw, prefix="forseti-x-"
    ) as runner:
        runner.climb(stem="u__precond", plan=PLAN, include="/abs/u.c")
    assert tmp_path.exists()
    assert (tmp_path / "u__precond.c").exists()


def test_an_owned_temp_dir_is_created_and_removed() -> None:
    raw = _Recorder([_verified()])
    with sidecar_runner(
        work_dir=None, max_len=8, ladder_cap=32, raw=raw, prefix="forseti-precond-"
    ) as runner:
        held = runner.work_dir
        runner.climb(stem="u__precond", plan=PLAN, include="/abs/u.c")
        assert held.exists()
        assert held.name.startswith("forseti-precond-")
    assert not held.exists()

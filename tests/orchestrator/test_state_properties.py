"""Property-based tests for the loop state machine (`next_state` + `run_loop`).

`test_state.py` pins the four verdict->state arrows by example; these assert the
*invariants* around them:

* `next_state` depends only on the verdict *variant*, never on its payload, and
  is total and deterministic;
* `run_loop` records `next_state(result)` as the state of every iteration (the
  "single source of truth" claim in `state.py`), always halts in a terminal
  state, and calls `fix` exactly once per `Violated` round.

The verify/fix ports are faked — no esbmc binary, no disk.
"""

from __future__ import annotations

from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st

from forseti.esbmc import (
    Error,
    EsbmcResult,
    RunMeta,
    Unknown,
    UnknownReason,
    Verified,
    Violated,
)
from forseti.orchestrator import GiveUpReason, LoopState, next_state, run_loop

SRC = Path("kernel.c")
_TERMINAL = {LoopState.DONE, LoopState.UNKNOWN, LoopState.GIVE_UP}


def _meta() -> RunMeta:
    return RunMeta(
        esbmc_version="8.3.0",
        argv=("esbmc", "kernel.c", "--unwind", "8"),
        exit_code=0,
        duration_s=0.0,
        stdout="",
        stderr="",
    )


# Arbitrary provenance: the mapping must ignore every field of it.
_metas = st.builds(
    RunMeta,
    esbmc_version=st.text(),
    argv=st.lists(st.text()).map(tuple),
    exit_code=st.integers(),
    duration_s=st.floats(allow_nan=False, allow_infinity=False),
    stdout=st.text(),
    stderr=st.text(),
)

_results = st.one_of(
    _metas.map(Verified),
    st.builds(Violated, _metas, st.text()),
    st.builds(Unknown, _metas, st.sampled_from(list(UnknownReason))),
    st.builds(Error, _metas, st.text()),
)


@given(_metas)
def test_verified_maps_to_done_regardless_of_meta(meta: RunMeta) -> None:
    assert next_state(Verified(meta)) is LoopState.DONE


@given(_metas, st.text())
def test_violated_maps_to_fix_regardless_of_payload(meta: RunMeta, raw: str) -> None:
    assert next_state(Violated(meta, raw)) is LoopState.FIX


@given(_metas, st.sampled_from(list(UnknownReason)))
def test_unknown_maps_to_unknown_regardless_of_reason(
    meta: RunMeta, reason: UnknownReason
) -> None:
    assert next_state(Unknown(meta, reason)) is LoopState.UNKNOWN


@given(_metas, st.text())
def test_error_maps_to_give_up_regardless_of_message(meta: RunMeta, msg: str) -> None:
    assert next_state(Error(meta, msg)) is LoopState.GIVE_UP


@given(_results)
def test_next_state_is_total_and_deterministic(result: EsbmcResult) -> None:
    first = next_state(result)
    assert isinstance(first, LoopState)
    assert first is next_state(result)  # pure: same input, same output


# --- run_loop invariants over synthesized verdict sequences ---------------


class _ScriptedVerify:
    """A VerifyPort replaying a scripted list of verdicts in order."""

    def __init__(self, results: list[EsbmcResult]) -> None:
        self._results = list(results)
        self.calls = 0

    def __call__(self, source: Path, *, unwind: int) -> EsbmcResult:
        self.calls += 1
        return self._results.pop(0)


class _NoopFix:
    """A FixPort that leaves the source untouched and counts calls."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, source: Path, violated: Violated) -> Path:
        self.calls += 1
        return source


_TERMINAL_VERDICT: dict[str, tuple[EsbmcResult, LoopState]] = {
    "verified": (Verified(_meta()), LoopState.DONE),
    "unknown": (Unknown(_meta(), UnknownReason.TIMEOUT), LoopState.UNKNOWN),
    "error": (Error(_meta(), "boom"), LoopState.GIVE_UP),
}


@given(
    prefix=st.integers(min_value=0, max_value=8),
    terminal=st.sampled_from(sorted(_TERMINAL_VERDICT)),
)
def test_run_loop_records_next_state_and_halts(prefix: int, terminal: str) -> None:
    # With an empty ladder, each round consumes exactly one verdict: `prefix`
    # Violated rounds (each calls fix), then one terminal verdict.
    verdict, expected_final = _TERMINAL_VERDICT[terminal]
    script: list[EsbmcResult] = [Violated(_meta(), "[Counterexample]\n")] * prefix
    script.append(verdict)
    verify = _ScriptedVerify(script)
    fix = _NoopFix()

    run = run_loop(SRC, verify=verify, fix=fix, unwind=1, max_iterations=prefix + 5)

    assert verify.calls == prefix + 1  # script consumed exactly, no over-pop
    assert fix.calls == prefix  # fix once per Violated round
    assert len(run.iterations) == prefix + 1
    assert run.final_state is expected_final
    assert run.final_state in _TERMINAL
    # Every recorded state is exactly what next_state maps the verdict to.
    for it in run.iterations:
        assert it.state is next_state(it.result)


@given(max_iterations=st.integers(min_value=1, max_value=8))
def test_run_loop_exhausts_budget_on_persistent_violation(max_iterations: int) -> None:
    script: list[EsbmcResult] = [
        Violated(_meta(), "[Counterexample]\n")
    ] * max_iterations
    verify = _ScriptedVerify(script)
    fix = _NoopFix()

    run = run_loop(SRC, verify=verify, fix=fix, unwind=1, max_iterations=max_iterations)

    assert run.final_state is LoopState.GIVE_UP
    assert run.give_up_reason is GiveUpReason.MAX_ITERATIONS_EXCEEDED
    assert len(run.iterations) == max_iterations
    assert verify.calls == max_iterations
    assert fix.calls == max_iterations

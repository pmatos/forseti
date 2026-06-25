"""Behavioural tests for the pure loop-state transition `next_state`.

Each test feeds `next_state` one `EsbmcResult` variant and asserts the loop
state it maps to. No subprocess, no esbmc binary — the verdict objects are
built directly, mirroring tests/esbmc/test_classify.py.
"""

from forseti.esbmc import Error, RunMeta, Unknown, UnknownReason, Verified, Violated
from forseti.orchestrator import LoopState, next_state


def meta() -> RunMeta:
    return RunMeta(
        esbmc_version="8.3.0",
        argv=("esbmc", "f.c", "--unwind", "8", "--no-unwinding-assertions"),
        exit_code=0,
        duration_s=0.0,
        stdout="",
        stderr="",
    )


def test_verified_maps_to_done() -> None:
    assert next_state(Verified(meta())) is LoopState.DONE


def test_violated_maps_to_fix() -> None:
    assert next_state(Violated(meta(), "[Counterexample]\n")) is LoopState.FIX


def test_unknown_maps_to_unknown_halt() -> None:
    # A distinct, honest halt — never a silent pass (roadmap Risk 1).
    result = Unknown(meta(), UnknownReason.TIMEOUT)
    assert next_state(result) is LoopState.UNKNOWN


def test_error_maps_to_give_up() -> None:
    # A tooling/invocation failure is not a verdict about the code: give up.
    assert next_state(Error(meta(), "esbmc binary not found")) is LoopState.GIVE_UP

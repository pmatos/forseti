"""Contract tests for the #28 fix-request seam.

`FixRequest`/`FixProvider`/the applier are exercised through their public
surface with no esbmc binary, no network: a hand-built `Counterexample` stands
in for a real ESBMC trace, and a recorded provider replays known-good source.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from forseti.esbmc import (
    Assignment,
    Counterexample,
    EsbmcResult,
    RunMeta,
    SourceLoc,
    Step,
    Verified,
    ViolatedProperty,
    Violated,
)
from forseti.orchestrator import (
    FixRequest,
    LoopState,
    ProviderFixPort,
    RecordedFixProvider,
    run_loop,
)

SRC = Path("kernel.c")
EXAMPLES = Path(__file__).resolve().parents[2] / "examples"


def meta() -> RunMeta:
    return RunMeta(
        esbmc_version="8.3.0",
        argv=("esbmc", "kernel.c", "--unwind", "8", "--no-unwinding-assertions"),
        exit_code=0,
        duration_s=0.0,
        stdout="",
        stderr="",
    )


def counterexample() -> Counterexample:
    loc = SourceLoc(file="kernel.c", line=10, column=4, function="my_abs")
    step = Step(
        number=1,
        loc=loc,
        assignments=(Assignment(lhs="x", value="-9223372036854775808", binary=None),),
    )
    prop = ViolatedProperty(
        loc=loc,
        description="assertion my_abs(x) >= 0",
        expression="my_abs(x) >= 0",
        cwe=(),
    )
    return Counterexample(steps=(step,), violated_property=prop)


def violated(cex: Counterexample | None = None) -> Violated:
    return Violated(meta(), "[Counterexample]\n", cex)


def test_from_violation_carries_cex_fields() -> None:
    cex = counterexample()
    request = FixRequest.from_violation(SRC, "int64_t my_abs(...)", violated(cex))

    assert request.source == SRC
    assert request.source_text == "int64_t my_abs(...)"
    assert request.counterexample is cex
    assert request.raw_counterexample == "[Counterexample]\n"
    assert request.violated_property is cex.violated_property
    assert request.inputs == cex.inputs
    assert request.steps == cex.steps


def test_from_violation_none_cex_falls_back_to_raw() -> None:
    request = FixRequest.from_violation(SRC, "src", violated(None))

    assert request.counterexample is None
    assert request.violated_property is None
    assert request.inputs == ()
    assert request.steps == ()
    assert request.raw_counterexample == "[Counterexample]\n"


def test_recorded_provider_returns_mapped_text(tmp_path: Path) -> None:
    replacement = tmp_path / "fixed.c"
    replacement.write_text("int fixed(void){return 0;}\n")
    provider = RecordedFixProvider({SRC: replacement})

    patched = provider.propose_fix(FixRequest.from_violation(SRC, "src", violated()))

    assert patched == "int fixed(void){return 0;}\n"
    assert provider.calls == 1


def test_recorded_provider_unmapped_source_raises(tmp_path: Path) -> None:
    provider = RecordedFixProvider({SRC: tmp_path / "fixed.c"})
    request = FixRequest.from_violation(Path("other.c"), "src", violated())

    with pytest.raises(KeyError):
        provider.propose_fix(request)


class StubProvider:
    """A minimal FixProvider returning a fixed patch, isolating the applier."""

    def __init__(self, patch: str) -> None:
        self._patch = patch

    def propose_fix(self, request: FixRequest) -> str:
        return self._patch


def test_provider_fix_port_writes_and_returns_versioned_path(tmp_path: Path) -> None:
    source = tmp_path / "kernel.c"
    source.write_text("orig\n")
    work_dir = tmp_path / "work"
    fix = ProviderFixPort(StubProvider("patched\n"), work_dir=work_dir)

    dest1 = fix(source, violated())

    assert work_dir.is_dir()
    assert dest1.parent == work_dir
    assert dest1.name == "kernel.fix1.c"
    assert dest1.read_text() == "patched\n"

    dest2 = fix(source, violated())
    assert dest2.name == "kernel.fix2.c"


class FakeVerify:
    """A VerifyPort replaying scripted verdicts (the test_loop.py shape)."""

    def __init__(self, results: list[EsbmcResult]) -> None:
        self._results = list(results)

    def __call__(self, source: Path, *, unwind: int) -> EsbmcResult:
        assert self._results, "FakeVerify over-popped: script exhausted"
        return self._results.pop(0)


def test_scripted_provider_drives_loop_abs_to_fixed(tmp_path: Path) -> None:
    # Acceptance: a scripted provider turns abs.c -> abs_fixed.c through the real
    # run_loop, and the applied patch is what gets re-verified to DONE. No esbmc:
    # FakeVerify supplies VIOLATED then VERIFIED.
    unit = tmp_path / "abs.c"
    shutil.copy(EXAMPLES / "abs.c", unit)
    provider = RecordedFixProvider({unit: EXAMPLES / "abs_fixed.c"})
    fix = ProviderFixPort(provider, work_dir=tmp_path / "work")
    verify = FakeVerify([violated(), Verified(meta())])

    run = run_loop(unit, verify=verify, fix=fix, unwind=1, max_iterations=2)

    assert run.final_state is LoopState.DONE
    assert provider.calls == 1
    # The path the final (VERIFIED) pass ran on is the applier's output; read it
    # back from the run rather than guessing the work/abs.fix1.c filename.
    applied = run.iterations[-1].source
    assert applied.read_text() == (EXAMPLES / "abs_fixed.c").read_text()

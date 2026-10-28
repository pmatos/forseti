"""Shared semantic-check preparation and the three operations consuming it."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest

from forseti.core.check import check_source
from forseti.core.events import PROPERTY_CHECK_START
from forseti.esbmc import EsbmcResult, RunMeta, Unknown, UnknownReason, Violated
from forseti.grading.discriminate import SafetyOutcome, SafetyReading, score_pair
from forseti.grading.grade import grade_unit
from forseti.orchestrator import SemanticHarnessWriter, Unit, VerifyPort, semantic_check
from forseti.orchestrator.semantic_check import SemanticCheckPlan
from forseti.properties import (
    Property,
    PropertyKind,
    PropertyStatus,
    PropertyStore,
    Provenance,
)


@pytest.mark.parametrize("operation", ["check", "pair", "grade"])
@pytest.mark.parametrize(
    ("max_len", "tail", "expected"),
    [
        (20, None, [4, 8, 16, 21]),
        (20, (), [4]),
        (None, None, [4, 8, 16]),
        (20, (7,), [4, 7]),
    ],
)
def test_operations_share_default_and_explicit_bound_policy(
    tmp_path: Path,
    operation: Literal["check", "pair", "grade"],
    max_len: int | None,
    tail: tuple[int, ...] | None,
    expected: list[int],
) -> None:
    source = tmp_path / "f.c"
    source.write_text("int f(int x) { return x; }\n")
    prop = Property(
        "p",
        f"{source}::f",
        PropertyKind.SEMANTIC,
        "result == x",
        PropertyStatus.CANDIDATE,
        Provenance("test", "1"),
    )
    calls: dict[Path, list[int]] = {}

    def verify_for(original: Path) -> VerifyPort:
        def port(harness: Path, *, unwind: int) -> EsbmcResult:
            calls.setdefault(original, []).append(unwind)
            return Unknown(RunMeta("test", (), 0, 0.0, "", ""), UnknownReason.TIMEOUT)

        return port

    root = tmp_path / "store"
    with PropertyStore.open(root) as store:
        store.add(prop)
        if operation == "check":
            check_source(
                source,
                function="f",
                store_root=root,
                max_len=max_len,
                unwind_ladder=tail,
                verify_port=verify_for(source),
            )
        elif operation == "pair":
            buggy = tmp_path / "buggy.c"
            buggy.write_text(source.read_text())
            score_pair(
                buggy=buggy,
                fixed=source,
                symbol="f",
                properties=[prop],
                safety=lambda _path, _symbol: SafetyReading(SafetyOutcome.PASSED, "ok"),
                verify_for=verify_for,
                work_dir=tmp_path / "work",
                max_len=max_len,
                unwind_ladder=tail,
            )
            assert calls[buggy] == expected
        else:
            grade_unit(
                source,
                "f",
                store=store,
                work_dir=tmp_path / "work",
                max_len=max_len,
                unwind_ladder=tail,
                verify_for=verify_for,
            )
    assert calls[source] == expected


def test_core_still_records_start_before_rejecting_an_invalid_ladder(
    tmp_path: Path,
) -> None:
    source = tmp_path / "f.c"
    source.write_text("int f(void) { return 0; }\n")
    root = tmp_path / "store"
    with pytest.raises(ValueError, match="increasing"):
        check_source(source, function="f", store_root=root, unwind_ladder=(4,))
    assert PROPERTY_CHECK_START in (root / "events.jsonl").read_text()


def test_grade_preserves_configuration_error_precedence(tmp_path: Path) -> None:
    with PropertyStore.open(tmp_path / "store") as store:
        for max_len, message in [(-1, "max_len"), (8, "max_mutants")]:
            with pytest.raises(ValueError, match=message):
                grade_unit(
                    tmp_path / "missing.c",
                    "f",
                    store=store,
                    work_dir=tmp_path / "work",
                    max_len=max_len,
                    max_mutants=0,
                    unwind_ladder=(4,),
                )


def test_plan_keeps_ladder_validation_at_the_callers_chosen_phase() -> None:
    plan = SemanticCheckPlan(unwind=8, unwind_ladder=(8,), max_len=0)
    assert plan.ladder == (8,)
    assert plan.max_len == 0
    with pytest.raises(ValueError, match="increasing"):
        plan.validate_ladder()
    with pytest.raises(ValueError, match="max_len must be >= 0, got -1"):
        SemanticCheckPlan(max_len=-1)


def test_plan_defers_writer_selection_and_preserves_injected_adapters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = SemanticHarnessWriter(max_len=3)
    sources: list[Path] = []

    def port(_path: Path, *, unwind: int) -> EsbmcResult:
        return Violated(
            RunMeta("test", (), 0, 0.0, "", ""), "unwinding assertion", None
        )

    def factory(source: Path) -> VerifyPort:
        sources.append(source)
        return port

    def unexpected(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("must not construct the default writer")

    monkeypatch.setattr(semantic_check, "SemanticHarnessWriter", unexpected)
    unused = SemanticCheckPlan()
    unused.validate_ladder()  # an excluded pair never needs a writer or verifier
    plan = SemanticCheckPlan(render=writer, verify_for=factory)
    assert sources == []
    assert plan.writer is writer
    assert plan.verify_for(tmp_path / "f.c") is port
    assert sources == [tmp_path / "f.c"]
    # An injected adapter is authoritative, even for unwinding failures.
    assert isinstance(plan.verify_for(tmp_path / "f.c")(tmp_path, unwind=4), Violated)


@pytest.mark.parametrize("max_len", [0, 20, None])
def test_plan_writer_uses_the_same_length_cap_as_its_ladder(
    tmp_path: Path, max_len: int | None
) -> None:
    plan = SemanticCheckPlan(max_len=max_len)
    source = tmp_path / "f.c"
    source.write_text("void f(int *buf, unsigned len) { }\n")
    unit = Unit.from_path(source, "f")
    prop = Property(
        "p",
        unit.unit_id,
        PropertyKind.SEMANTIC,
        "1",
        PropertyStatus.CANDIDATE,
        Provenance("test", "1"),
    )
    rendered = plan.writer.render(unit, prop)
    assert rendered.length_bounds == (() if max_len is None else (("len", max_len),))
    if max_len is not None:
        assert (plan.ladder[-1] if plan.ladder else 4) > max_len
    plan.validate_ladder()


def test_plan_binds_each_source_and_only_remaps_under_unwinding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[Path, dict[str, object]]] = []
    meta = RunMeta("test", (), 0, 0.0, "", "")
    results: list[EsbmcResult] = [
        Violated(meta, "unwinding assertion", None),
        Violated(meta, "real postcondition failure", None),
        Unknown(meta, UnknownReason.TIMEOUT),
    ]

    def raw(path: Path, **kwargs: object) -> EsbmcResult:
        seen.append((path, kwargs))
        return results.pop(0)

    monkeypatch.setattr(semantic_check, "verify", raw)
    plan = SemanticCheckPlan(
        timeout_s=0.25, esbmc_bin="chosen-esbmc", extra_flags=("-Iproject", "--flag")
    )
    harness = tmp_path / "work" / "h.c"
    fixed = plan.verify_for(tmp_path / "fixed" / "f.c")
    buggy = plan.verify_for(tmp_path / "buggy" / "f.c")
    assert seen == []
    first = fixed(harness, unwind=4)
    assert isinstance(first, Unknown)
    assert first.reason is UnknownReason.UNDER_UNWOUND
    assert first.meta is meta
    second = buggy(harness, unwind=8)
    assert isinstance(second, Violated)
    assert second.raw_counterexample == "real postcondition failure"
    third = fixed(harness, unwind=16)
    assert isinstance(third, Unknown)
    assert third.reason is UnknownReason.TIMEOUT
    for (path, kwargs), parent, unwind in zip(
        seen, ("fixed", "buggy", "fixed"), (4, 8, 16), strict=True
    ):
        assert path == harness
        assert kwargs == {
            "unwind": unwind,
            "timeout_s": 0.25,
            "esbmc_bin": "chosen-esbmc",
            "extra_flags": ("-Iproject", "--flag", f"-I{tmp_path / parent}"),
            "no_unwinding_assertions": False,
        }

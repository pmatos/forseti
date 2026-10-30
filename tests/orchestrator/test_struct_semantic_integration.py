"""End-to-end (real ESBMC): semantic properties over struct-pointer parameters (#422).

`rb_push(ring_t *r, int v)` and `sha1_update(sha1_ctx *ctx, data, len)` are checked
through the ordinary semantic writer, which binds the struct as one nondet object.
A property over a field is VERIFIED up to k on the clean ring buffer and VIOLATED on
its staged-defect twin; with no domain the nondet ring can be a state no caller
builds, and the verdict says so. Skipped when esbmc is not on PATH.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from forseti.orchestrator import (
    PropertyCheckRun,
    PropertyOutcome,
    Unit,
    check_properties,
)
from forseti.orchestrator.semantic_check import FixedProperties, SemanticCheckPlan
from forseti.precond.reachability import ProbeReachability
from forseti.properties import (
    Property,
    PropertyKind,
    PropertyStatus,
    Provenance,
    make_property_id,
)

pytestmark = pytest.mark.skipif(
    shutil.which("esbmc") is None, reason="esbmc binary not on PATH"
)

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
IN_RANGE = ("r->count <= 4", "r->head < 4", "r->tail < 4")


def _semantic(unit: Unit, expression: str, domain: tuple[str, ...] = ()) -> Property:
    return Property(
        property_id=make_property_id(
            unit.unit_id, PropertyKind.SEMANTIC, expression, domain
        ),
        unit_id=unit.unit_id,
        kind=PropertyKind.SEMANTIC,
        expression=expression,
        status=PropertyStatus.CANDIDATE,
        provenance=Provenance("test", "v1"),
        domain=domain,
    )


def _run(unit: Unit, prop: Property, tmp_path: Path) -> PropertyCheckRun:
    plan = SemanticCheckPlan(max_len=4, timeout_s=60)
    return check_properties(
        unit,
        store=FixedProperties([prop]),
        render=plan.writer,
        verify=plan.verify_for(unit.path),
        work_dir=tmp_path / "work",
        unwind=plan.unwind,
        unwind_ladder=plan.ladder,
    )


def test_a_field_property_holds_on_the_clean_ring_buffer(tmp_path: Path) -> None:
    unit = Unit.from_path(EXAMPLES / "ring_buffer.c", "rb_push")
    prop = _semantic(unit, "result == 0 || r->count > 0", IN_RANGE)

    (verdict,) = _run(unit, prop, tmp_path).verdicts

    assert verdict.outcome is PropertyOutcome.HELD
    assert verdict.probe is ProbeReachability.REACHED
    assumption = verdict.to_dict()["assumption"]
    assert "r (ring_t)" in assumption and "nondet" in assumption
    assert verdict.harness_source is not None
    assert "ring_t r__obj = nondet_ring_t();" in verdict.harness_source


def test_a_capacity_property_is_violated_on_the_staged_defect_twin(
    tmp_path: Path,
) -> None:
    clean = Unit.from_path(EXAMPLES / "ring_buffer.c", "rb_push")
    twin = Unit.from_path(EXAMPLES / "ring_buffer_bug.c", "rb_push")
    for unit, expected in (
        (clean, PropertyOutcome.HELD),
        (twin, PropertyOutcome.VIOLATED),
    ):
        prop = _semantic(unit, "r->count <= 4", IN_RANGE)
        (verdict,) = _run(unit, prop, tmp_path / unit.path.stem).verdicts
        assert verdict.outcome is expected
        assert verdict.assumption is not None


def test_without_a_domain_the_nondet_ring_is_a_state_no_caller_builds(
    tmp_path: Path,
) -> None:
    unit = Unit.from_path(EXAMPLES / "ring_buffer.c", "rb_push")
    prop = _semantic(unit, "result == 0 || r->count > 0")

    (verdict,) = _run(unit, prop, tmp_path).verdicts

    assert verdict.outcome is PropertyOutcome.VIOLATED
    assert verdict.assumption is not None


def test_sha1_update_renders_and_reaches_esbmc(tmp_path: Path) -> None:
    unit = Unit.from_path(EXAMPLES / "sha1.c", "sha1_update")
    prop = _semantic(unit, "ctx->count < 1004", ("ctx->count < 1000", "len >= 1"))

    (verdict,) = _run(unit, prop, tmp_path).verdicts

    assert verdict.outcome is PropertyOutcome.HELD
    assert verdict.skip_reason is None
    assert verdict.length_bounds == (("len", 4),)
    assert verdict.assumption is not None

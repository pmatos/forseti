"""The kill-rate scorer end to end against the real ESBMC and the real store.

Skipped when `esbmc` is not on PATH. `examples/abs_fixed.c::my_abs` is scalar and
loop-free, so its verdicts do not depend on the ESBMC version; the assertions pin
invariants (every held property is graded and stored, every survivor is named,
the counts add up), not mutant counts or rates.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from forseti.core.grade import grade_source
from forseti.grading.grade import GradeOutcome
from forseti.properties import (
    GradingVerdict,
    Property,
    PropertyKind,
    PropertyStatus,
    PropertyStore,
    Provenance,
    make_property_id,
)
from forseti.unit_id import make_unit_id

pytestmark = pytest.mark.skipif(
    shutil.which("esbmc") is None, reason="esbmc binary not on PATH"
)

KERNEL = Path(__file__).parents[2] / "examples" / "abs_fixed.c"
DOMAIN = ("x > INT64_MIN",)


def add(store: PropertyStore, unit_id: str, expression: str) -> str:
    prop = Property(
        make_property_id(unit_id, PropertyKind.SEMANTIC, expression, DOMAIN),
        unit_id,
        PropertyKind.SEMANTIC,
        expression,
        PropertyStatus.CANDIDATE,
        Provenance("test", "1"),
        domain=DOMAIN,
    )
    store.add(prop)
    return prop.property_id


def test_every_held_property_of_a_kernel_is_graded_and_stored(tmp_path: Path) -> None:
    root = tmp_path / ".forseti"
    unit_id = make_unit_id(KERNEL, "my_abs")
    with PropertyStore.open(root) as store:
        held_a = add(store, unit_id, "result >= 0")
        held_b = add(store, unit_id, "result >= x")
        violated = add(store, unit_id, "result == x")  # false for a negative x

    run = grade_source(KERNEL, function="my_abs", store_root=root)

    by_id = {g.prop.property_id: g for g in run.properties}
    assert by_id[violated].outcome is GradeOutcome.NOT_HELD
    assert run.generated > 0
    payload = json.loads(json.dumps(run.to_dict()))
    assert payload["limitations"]
    for property_id in (held_a, held_b):
        grade = by_id[property_id]
        assert grade.outcome is GradeOutcome.GRADED
        counts = grade.counts()
        assert sum(counts.values()) == run.generated
        assert counts["killed"] >= 1
        assert grade.kill_rate == counts["killed"] / (
            counts["killed"] + counts["survived"]
        )
        assert grade.grading is not None
        for reading in grade.readings:
            if reading.outcome.value == "survived":
                assert reading.mutant_id in grade.reason

    with PropertyStore.open(root) as store:
        stored = {p.property_id: p for p in store.list_for_unit(unit_id)}
        rows = store._conn.execute(
            "SELECT grading_kill_rate FROM properties WHERE status = 'graded'"
        ).fetchall()
    assert len(rows) == 2
    assert all(row[0] is not None for row in rows)
    for property_id in (held_a, held_b):
        grading = stored[property_id].grading
        assert stored[property_id].status is PropertyStatus.GRADED
        assert grading is not None
        assert grading.verdict is GradingVerdict.HELD
        assert grading.kill_rate == by_id[property_id].kill_rate
        assert grading.reason == by_id[property_id].reason
    assert stored[violated].status is PropertyStatus.CANDIDATE
    assert stored[violated].grading is None

    events = (root / "events.jsonl").read_text().splitlines()
    assert any('"type": "grading.mutant"' in line for line in events)

    again = grade_source(KERNEL, function="my_abs", store_root=root)
    assert {
        g.prop.property_id
        for g in again.properties
        if g.outcome is GradeOutcome.NOT_CANDIDATE
    } == {
        held_a,
        held_b,
    }

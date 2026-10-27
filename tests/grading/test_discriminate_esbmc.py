"""The discrimination scorer end to end against the real ESBMC.

Skipped when `esbmc` is not on PATH. The hand-written clamp pair is scalar,
loop-free and overflow-free, so its verdicts do not depend on the ESBMC version.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from forseti.grading.discriminate import (
    Absorption,
    Discrimination,
    ExclusionReason,
    PairOutcome,
    load_properties_file,
    score_pair,
)
from forseti.properties import Property, PropertyKind, PropertyStatus, Provenance

pytestmark = pytest.mark.skipif(
    shutil.which("esbmc") is None, reason="esbmc binary not on PATH"
)

FIXTURES = Path(__file__).parent / "fixtures"
EXAMPLES = Path(__file__).parents[2] / "examples"
TRIGGER = {"x": 10, "lo": 0, "hi": 5}


def prop(property_id: str, expression: str, *domain: str) -> Property:
    return Property(
        property_id,
        "clamp.c::clamp",
        PropertyKind.SEMANTIC,
        expression,
        PropertyStatus.CANDIDATE,
        Provenance("test", "1"),
        domain=domain,
    )


def test_hand_written_pair_has_a_discriminating_property(tmp_path: Path) -> None:
    props = [
        prop("above", "x <= hi || result == hi", "lo <= hi"),
        prop("floor", "result >= lo", "lo <= hi"),
        prop("absorbed", "x <= hi || result == hi", "lo <= hi", "x <= hi"),
        prop("empty_domain", "result >= lo", "lo > hi && lo < hi"),
        prop("false_alarm", "result == x"),
    ]
    got = score_pair(
        buggy=FIXTURES / "clamp_bug.c",
        fixed=FIXTURES / "clamp.c",
        symbol="clamp",
        properties=props,
        work_dir=tmp_path / "work",
        trigger=TRIGGER,
    )

    assert got.usable
    assert got.outcome is PairOutcome.DISCRIMINATED
    by_id = {s.prop.property_id: s for s in got.scores}
    assert [s.prop.property_id for s in got.discriminating] == ["above"]
    assert by_id["above"].fixed.outcome.value == "held"
    assert by_id["above"].buggy.outcome.value == "violated"
    assert by_id["floor"].discrimination is Discrimination.NOT_DISCRIMINATING
    assert by_id["false_alarm"].fixed.outcome.value == "violated"
    # a property whose domain admits no input is vacuous, never a pass
    assert by_id["empty_domain"].fixed.outcome.value == "vacuous"
    assert by_id["empty_domain"].discrimination is Discrimination.INCONCLUSIVE
    # M4: the trigger lies outside `x <= hi`, which is why that property holds
    assert by_id["absorbed"].buggy.outcome.value == "held"
    assert by_id["absorbed"].absorption is Absorption.ABSORBED
    assert by_id["floor"].absorption is Absorption.IN_DOMAIN
    assert by_id["above"].absorption is Absorption.NOT_APPLICABLE
    json.dumps(got.to_dict())


def test_properties_load_from_a_run_file_and_score(tmp_path: Path) -> None:
    run_file = tmp_path / "proposal.json"
    run_file.write_text(
        json.dumps(
            {
                "accepted": [
                    {
                        "expression": "x <= hi || result == hi",
                        "domain": ["lo <= hi"],
                    }
                ]
            }
        )
    )
    got = score_pair(
        buggy=FIXTURES / "clamp_bug.c",
        fixed=FIXTURES / "clamp.c",
        symbol="clamp",
        properties=load_properties_file(run_file, unit_id="clamp.c::clamp"),
        work_dir=tmp_path / "work",
    )
    assert got.outcome is PairOutcome.DISCRIMINATED


@pytest.mark.parametrize(
    ("clean", "bug", "symbol"),
    [
        ("utf8_decode", "utf8_decode_bug", "utf8_decode"),
        ("murmurhash", "murmurhash_bug", "murmur3_32"),
        ("sha1", "sha1_bug", "sha1_update"),
        ("frame_checksum", "frame_checksum_bug", "sum_bytes"),
    ],
)
def test_memory_bug_pairs_are_excluded_not_scored(
    tmp_path: Path, clean: str, bug: str, symbol: str
) -> None:
    got = score_pair(
        buggy=EXAMPLES / f"{bug}.c",
        fixed=EXAMPLES / f"{clean}.c",
        symbol=symbol,
        properties=[prop("any", "1")],
        work_dir=tmp_path / "work",
    )
    assert got.outcome is PairOutcome.EXCLUDED
    assert got.exclusion is not None
    assert got.exclusion.reason is ExclusionReason.S0_CATCHES_BUG
    assert got.scores == ()


def test_signed_overflow_is_caught_by_s0(tmp_path: Path) -> None:
    got = score_pair(
        buggy=EXAMPLES / "abs.c",
        fixed=EXAMPLES / "abs_fixed.c",
        symbol="my_abs",
        properties=[prop("any", "1")],
        work_dir=tmp_path / "work",
    )
    assert got.outcome is PairOutcome.EXCLUDED
    assert got.exclusion is not None
    assert got.exclusion.reason is ExclusionReason.S0_CATCHES_BUG

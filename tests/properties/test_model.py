"""Tests for the property model: enums, `to_dict`, transitions, content id."""

from __future__ import annotations

from forseti.properties import (
    Grading,
    GradingVerdict,
    InvalidStatusTransition,
    Property,
    PropertyKind,
    PropertyStatus,
    Provenance,
    is_valid_transition,
    make_property_id,
)


def make_prop(
    *,
    unit_id: str = "examples/abs.c::my_abs",
    kind: PropertyKind = PropertyKind.SEMANTIC,
    expression: str = "result >= 0",
    domain: tuple[str, ...] = ("x > INT64_MIN",),
    status: PropertyStatus = PropertyStatus.CANDIDATE,
    grading: Grading | None = None,
    description: str | None = None,
) -> Property:
    return Property(
        property_id=make_property_id(unit_id, kind, expression, domain),
        unit_id=unit_id,
        kind=kind,
        expression=expression,
        status=status,
        provenance=Provenance("proposer-v1", "1"),
        domain=domain,
        grading=grading,
        description=description,
    )


def test_enum_values_are_stable() -> None:
    assert PropertyKind.SEMANTIC.value == "semantic"
    assert PropertyKind.REACHABILITY.value == "reachability"
    assert [s.value for s in PropertyStatus] == [
        "candidate",
        "graded",
        "accepted",
        "rejected",
    ]
    assert [v.value for v in GradingVerdict] == ["held", "violated", "unknown"]


def test_to_dict_roundtrips_fields_without_grading() -> None:
    prop = make_prop(description="abs is non-negative")
    d = prop.to_dict()

    assert d == {
        "property_id": prop.property_id,
        "unit_id": "examples/abs.c::my_abs",
        "kind": "semantic",
        "expression": "result >= 0",
        "status": "candidate",
        "provenance": {"prompt_id": "proposer-v1", "prompt_version": "1"},
        "domain": ["x > INT64_MIN"],
        "grading": None,
        "description": "abs is non-negative",
    }


def test_to_dict_with_grading_expands_nested() -> None:
    prop = make_prop(
        status=PropertyStatus.GRADED,
        grading=Grading(GradingVerdict.HELD, 0.75, "killed 3/4 mutants"),
    )
    d = prop.to_dict()

    assert d["grading"] == {
        "verdict": "held",
        "kill_rate": 0.75,
        "reason": "killed 3/4 mutants",
    }


def test_to_dict_grading_reason_may_be_none() -> None:
    prop = make_prop(grading=Grading(GradingVerdict.UNKNOWN, 0.0))
    assert prop.to_dict()["grading"] == {
        "verdict": "unknown",
        "kill_rate": 0.0,
        "reason": None,
    }


def test_valid_transitions() -> None:
    assert is_valid_transition(PropertyStatus.CANDIDATE, PropertyStatus.GRADED)
    assert is_valid_transition(PropertyStatus.CANDIDATE, PropertyStatus.REJECTED)
    assert is_valid_transition(PropertyStatus.GRADED, PropertyStatus.ACCEPTED)
    assert is_valid_transition(PropertyStatus.GRADED, PropertyStatus.REJECTED)


def test_invalid_transitions() -> None:
    # skips, backward moves, and every move out of a terminal.
    assert not is_valid_transition(PropertyStatus.CANDIDATE, PropertyStatus.ACCEPTED)
    assert not is_valid_transition(PropertyStatus.GRADED, PropertyStatus.CANDIDATE)
    for terminal in (PropertyStatus.ACCEPTED, PropertyStatus.REJECTED):
        for target in PropertyStatus:
            assert not is_valid_transition(terminal, target)


def test_no_self_transitions() -> None:
    for status in PropertyStatus:
        assert not is_valid_transition(status, status)


def test_invalid_status_transition_carries_context() -> None:
    exc = InvalidStatusTransition(PropertyStatus.CANDIDATE, PropertyStatus.ACCEPTED)
    assert exc.current is PropertyStatus.CANDIDATE
    assert exc.requested is PropertyStatus.ACCEPTED
    assert "candidate -> accepted" in str(exc)


def test_make_property_id_is_deterministic() -> None:
    a = make_property_id("u::f", PropertyKind.SEMANTIC, "r >= 0", ("x > 0",))
    b = make_property_id("u::f", PropertyKind.SEMANTIC, "r >= 0", ("x > 0",))
    assert a == b
    assert len(a) == 16


def test_make_property_id_distinguishes_every_component() -> None:
    base = make_property_id("u::f", PropertyKind.SEMANTIC, "r >= 0", ("x > 0",))
    assert base != make_property_id("u::g", PropertyKind.SEMANTIC, "r >= 0", ("x > 0",))
    assert base != make_property_id(
        "u::f", PropertyKind.REACHABILITY, "r >= 0", ("x > 0",)
    )
    assert base != make_property_id("u::f", PropertyKind.SEMANTIC, "r > 0", ("x > 0",))
    # same predicate, different precondition domain -> a distinct property.
    assert base != make_property_id("u::f", PropertyKind.SEMANTIC, "r >= 0", ("x < 0",))
    assert base != make_property_id("u::f", PropertyKind.SEMANTIC, "r >= 0", ())


def test_domain_defaults_to_empty() -> None:
    prop = Property(
        property_id="x",
        unit_id="u::f",
        kind=PropertyKind.SEMANTIC,
        expression="r >= 0",
        status=PropertyStatus.CANDIDATE,
        provenance=Provenance("p", "1"),
    )
    assert prop.domain == ()
    assert prop.to_dict()["domain"] == []


def test_reachability_property_is_representable() -> None:
    # #63 is parked (ADR-0009 D2): a reachability property must round-trip through
    # the model, but nothing here proposes or verifies it.
    prop = make_prop(kind=PropertyKind.REACHABILITY, expression="ERROR", domain=())
    assert prop.kind is PropertyKind.REACHABILITY
    assert prop.to_dict()["kind"] == "reachability"

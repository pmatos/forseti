"""Tests for the SQLite property store: round-trips, transitions, durability."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from forseti.properties import (
    DuplicateProperty,
    Grading,
    GradingVerdict,
    InvalidStatusTransition,
    Property,
    PropertyKind,
    PropertyNotFound,
    PropertyStatus,
    PropertyStore,
    Provenance,
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


def mem_store() -> PropertyStore:
    return PropertyStore(sqlite3.connect(":memory:"))


def test_open_creates_db_file(tmp_path: Path) -> None:
    store = PropertyStore.open(root=tmp_path)
    try:
        assert (tmp_path / "forseti.db").is_file()
    finally:
        store.close()
    # a second open on the same root is fine (schema is idempotent).
    PropertyStore.open(root=tmp_path).close()


def test_open_creates_missing_parent_dir(tmp_path: Path) -> None:
    nested = tmp_path / "a" / "b"
    PropertyStore.open(root=nested).close()
    assert (nested / "forseti.db").is_file()


def test_add_get_roundtrip_without_grading() -> None:
    store = mem_store()
    prop = make_prop(description="abs non-negative")
    store.add(prop)
    assert store.get(prop.property_id) == prop


def test_add_get_roundtrip_with_grading() -> None:
    store = mem_store()
    prop = make_prop(
        status=PropertyStatus.GRADED,
        grading=Grading(GradingVerdict.HELD, 0.5, "2/4 mutants"),
    )
    store.add(prop)
    assert store.get(prop.property_id) == prop


def test_add_roundtrip_preserves_empty_domain() -> None:
    store = mem_store()
    prop = make_prop(domain=())
    store.add(prop)
    got = store.get(prop.property_id)
    assert got is not None
    assert got.domain == ()


def test_get_unknown_returns_none() -> None:
    assert mem_store().get("nope") is None


def test_add_duplicate_raises() -> None:
    store = mem_store()
    prop = make_prop()
    store.add(prop)
    with pytest.raises(DuplicateProperty):
        store.add(prop)


def test_list_for_unit_scopes_and_orders() -> None:
    store = mem_store()
    p1 = make_prop(expression="result >= 0")
    p2 = make_prop(expression="result == result")
    other = make_prop(unit_id="examples/ring.c::push", expression="ok")
    store.add(p1)
    store.add(p2)
    store.add(other)

    unit = store.list_for_unit("examples/abs.c::my_abs")
    assert unit == (p1, p2)  # insertion order, other unit excluded
    assert store.list_for_unit("examples/ring.c::push") == (other,)
    assert store.list_for_unit("no/such::unit") == ()


def test_update_status_applies_and_persists() -> None:
    store = mem_store()
    prop = make_prop()
    store.add(prop)

    updated = store.update_status(prop.property_id, PropertyStatus.GRADED)
    assert updated.status is PropertyStatus.GRADED
    reloaded = store.get(prop.property_id)
    assert reloaded is not None
    assert reloaded.status is PropertyStatus.GRADED


def test_update_status_invalid_transition_rolls_back() -> None:
    store = mem_store()
    prop = make_prop()
    store.add(prop)

    with pytest.raises(InvalidStatusTransition):
        store.update_status(prop.property_id, PropertyStatus.ACCEPTED)

    # the row is untouched -- still candidate.
    reloaded = store.get(prop.property_id)
    assert reloaded is not None
    assert reloaded.status is PropertyStatus.CANDIDATE


def test_update_status_missing_id_raises() -> None:
    with pytest.raises(PropertyNotFound):
        mem_store().update_status("nope", PropertyStatus.GRADED)


def test_terminal_status_is_enforced() -> None:
    store = mem_store()
    prop = make_prop()
    store.add(prop)
    store.update_status(prop.property_id, PropertyStatus.GRADED)
    store.update_status(prop.property_id, PropertyStatus.ACCEPTED)
    with pytest.raises(InvalidStatusTransition):
        store.update_status(prop.property_id, PropertyStatus.REJECTED)


def test_durability_across_connections(tmp_path: Path) -> None:
    prop = make_prop()
    with PropertyStore.open(root=tmp_path) as store:
        store.add(prop)
    # a fresh connection to the same db sees the committed row.
    with PropertyStore.open(root=tmp_path) as reopened:
        assert reopened.get(prop.property_id) == prop


def test_context_manager_closes(tmp_path: Path) -> None:
    with PropertyStore.open(root=tmp_path) as store:
        store.add(make_prop())
        conn = store._conn
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")  # connection closed on __exit__


def test_ddl_check_guards_kind_against_raw_sql() -> None:
    # The typed API can never write a bad enum; the DDL CHECK is the backstop for
    # raw SQL. Locking it proves the constraint exists independent of the API.
    store = mem_store()
    with pytest.raises(sqlite3.IntegrityError):
        store._conn.execute(
            "INSERT INTO properties "
            "(property_id, unit_id, kind, expression, status, "
            " prompt_id, prompt_version) "
            "VALUES ('id', 'u::f', 'bogus', 'e', 'candidate', 'p', '1')"
        )

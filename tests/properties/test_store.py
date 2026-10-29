"""Tests for the SQLite property store: round-trips, transitions, durability."""

from __future__ import annotations

import dataclasses
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from forseti.properties import (
    DuplicateProperty,
    Grading,
    GradingRequired,
    GradingVerdict,
    InvalidStatusTransition,
    Property,
    PropertyKind,
    PropertyNotFound,
    PropertyStatus,
    PropertyStore,
    Provenance,
    WrongPropertyKind,
    make_invariant,
    make_property_id,
)

HELD_HALF = Grading(GradingVerdict.HELD, 0.5, "killed 1/2; survived: m")


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


def test_add_get_roundtrip_preserves_provider_and_model() -> None:
    store = mem_store()
    prop = dataclasses.replace(
        make_prop(),
        provenance=Provenance("proposer-v1", "1", provider="codex", model="gpt-5.1"),
    )
    store.add(prop)
    got = store.get(prop.property_id)
    assert got is not None
    assert got.provenance.provider == "codex"
    assert got.provenance.model == "gpt-5.1"


def test_ensure_schema_migrates_a_pre_provider_model_db(tmp_path: Path) -> None:
    """A `forseti.db` created before #213 lacks `provider`/`model` columns.

    `_ensure_schema`'s `ALTER TABLE` migration must add them in place -- an
    insert against a project that upgraded forseti in place (not a fresh
    `.forseti/`) must not fail with "no such column".
    """
    db_path = tmp_path / "forseti.db"
    pre_213_schema = """
    CREATE TABLE properties (
        property_id       TEXT PRIMARY KEY,
        unit_id           TEXT NOT NULL,
        kind              TEXT NOT NULL,
        expression        TEXT NOT NULL,
        domain            TEXT NOT NULL DEFAULT '[]',
        description       TEXT,
        status            TEXT NOT NULL,
        prompt_id         TEXT NOT NULL,
        prompt_version    TEXT NOT NULL,
        grading_verdict   TEXT,
        grading_kill_rate REAL,
        grading_reason    TEXT
    );
    """
    conn = sqlite3.connect(db_path)
    conn.executescript(pre_213_schema)
    conn.close()

    store = PropertyStore(sqlite3.connect(db_path))
    try:
        prop = make_prop()
        store.add(prop)
        got = store.get(prop.property_id)
        assert got is not None
        assert got.provenance.provider == ""
        assert got.provenance.model == ""
    finally:
        store.close()


def test_migrate_columns_tolerates_concurrent_alter_race(tmp_path: Path) -> None:
    """Two `PropertyStore`s racing to migrate the same pre-#213 db: the loser's
    `ALTER TABLE` must not fail once the column is actually there, since
    `PRAGMA table_info` (the missing-column check) isn't itself a lock and
    both openers can read the column as absent before either writes (issue
    #252 review).
    """
    db_path = tmp_path / "forseti.db"
    pre_213_schema = """
    CREATE TABLE properties (
        property_id       TEXT PRIMARY KEY,
        unit_id           TEXT NOT NULL,
        kind              TEXT NOT NULL,
        expression        TEXT NOT NULL,
        domain            TEXT NOT NULL DEFAULT '[]',
        description       TEXT,
        status            TEXT NOT NULL,
        prompt_id         TEXT NOT NULL,
        prompt_version    TEXT NOT NULL,
        grading_verdict   TEXT,
        grading_kill_rate REAL,
        grading_reason    TEXT
    );
    """
    setup_conn = sqlite3.connect(db_path)
    setup_conn.executescript(pre_213_schema)
    setup_conn.close()

    class RacingConnection(sqlite3.Connection):
        """Runs the same `ALTER` on a second connection right before this
        one's own attempt -- the exact interleaving two concurrent
        `PropertyStore`s opening the same pre-migration db could hit.
        """

        _fired = False

        def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
            if not self._fired and sql.startswith(
                "ALTER TABLE properties ADD COLUMN provider"
            ):
                self._fired = True
                racer = sqlite3.connect(db_path)
                racer.execute(sql)
                racer.commit()
                racer.close()
            return super().execute(sql, parameters)

    conn = sqlite3.connect(db_path, factory=RacingConnection)
    store = PropertyStore(conn)
    try:
        prop = make_prop()
        store.add(prop)
        got = store.get(prop.property_id)
        assert got is not None
        assert got.provenance.provider == ""
    finally:
        store.close()


def test_migrate_columns_reraises_a_genuine_alter_failure(tmp_path: Path) -> None:
    """The race tolerance must not swallow a real migration failure: if the
    column still isn't there after the `ALTER` raises, re-raise (issue #252
    review).
    """
    db_path = tmp_path / "forseti.db"

    class BoomingConnection(sqlite3.Connection):
        def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
            if sql.startswith("ALTER TABLE properties ADD COLUMN model"):
                raise sqlite3.OperationalError("disk I/O error")
            return super().execute(sql, parameters)

    conn = sqlite3.connect(db_path, factory=BoomingConnection)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE properties (
            property_id TEXT PRIMARY KEY,
            provider    TEXT NOT NULL DEFAULT ''
        );
        """
    )
    store = PropertyStore.__new__(PropertyStore)
    store._conn = conn  # type: ignore[attr-defined]

    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
        store._migrate_columns()  # type: ignore[attr-defined]


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


def test_list_for_unit_filters_by_status() -> None:
    store = mem_store()
    cand = make_prop(expression="a", status=PropertyStatus.CANDIDATE)
    graded = make_prop(expression="b", status=PropertyStatus.GRADED, grading=HELD_HALF)
    rejected = make_prop(expression="c", status=PropertyStatus.REJECTED)
    for prop in (cand, graded, rejected):
        store.add(prop)

    unit = "examples/abs.c::my_abs"
    # None (the default) is unchanged: every row, in insertion order.
    assert store.list_for_unit(unit) == (cand, graded, rejected)
    # A status subset scopes the read, preserving insertion order.
    assert store.list_for_unit(
        unit, {PropertyStatus.CANDIDATE, PropertyStatus.GRADED}
    ) == (cand, graded)
    assert store.list_for_unit(unit, {PropertyStatus.REJECTED}) == (rejected,)
    # An empty collection selects no status -> no rows (never an `IN ()`).
    assert store.list_for_unit(unit, ()) == ()


def test_update_status_applies_and_persists() -> None:
    store = mem_store()
    prop = make_prop(status=PropertyStatus.GRADED, grading=HELD_HALF)
    store.add(prop)

    updated = store.update_status(prop.property_id, PropertyStatus.ACCEPTED)
    assert updated.status is PropertyStatus.ACCEPTED
    assert updated.grading == HELD_HALF
    reloaded = store.get(prop.property_id)
    assert reloaded is not None
    assert reloaded.status is PropertyStatus.ACCEPTED


def test_update_status_to_graded_without_a_grading_is_refused() -> None:
    store = mem_store()
    prop = make_prop()
    store.add(prop)

    with pytest.raises(GradingRequired):
        store.update_status(prop.property_id, PropertyStatus.GRADED)

    assert store.get(prop.property_id) == prop


def test_update_status_to_graded_keeps_a_stored_grading() -> None:
    store = mem_store()
    prop = make_prop(grading=HELD_HALF)  # a candidate that already carries one
    store.add(prop)

    updated = store.update_status(prop.property_id, PropertyStatus.GRADED)

    assert updated.status is PropertyStatus.GRADED
    assert updated.grading == HELD_HALF


def test_add_graded_without_a_grading_is_refused_not_a_duplicate() -> None:
    store = mem_store()
    with pytest.raises(GradingRequired):
        store.add(make_prop(status=PropertyStatus.GRADED))
    assert store.list_for_unit("examples/abs.c::my_abs") == ()


def test_grade_writes_grading_and_status_together() -> None:
    store = mem_store()
    prop = make_prop()
    store.add(prop)

    graded = store.grade(prop.property_id, HELD_HALF)

    assert graded.status is PropertyStatus.GRADED
    assert graded.grading == HELD_HALF
    assert store.get(prop.property_id) == graded


@pytest.mark.parametrize("bad", [-0.1, 1.5, float("nan")])
def test_grade_rejects_an_out_of_range_kill_rate(bad: float) -> None:
    store = mem_store()
    prop = make_prop()
    store.add(prop)

    with pytest.raises(ValueError, match="kill_rate"):
        store.grade(prop.property_id, Grading(GradingVerdict.HELD, bad))

    assert store.get(prop.property_id) == prop


def test_grade_unknown_id_raises() -> None:
    with pytest.raises(PropertyNotFound):
        mem_store().grade("nope", HELD_HALF)


@pytest.mark.parametrize("status", [PropertyStatus.REJECTED, PropertyStatus.ACCEPTED])
def test_grade_refuses_a_terminal_property(status: PropertyStatus) -> None:
    store = mem_store()
    prop = make_prop(status=status)
    store.add(prop)

    with pytest.raises(InvalidStatusTransition):
        store.grade(prop.property_id, HELD_HALF)

    assert store.get(prop.property_id) == prop


def test_grade_refuses_a_second_grading() -> None:
    store = mem_store()
    prop = make_prop()
    store.add(prop)
    store.grade(prop.property_id, HELD_HALF)

    with pytest.raises(InvalidStatusTransition):
        store.grade(prop.property_id, Grading(GradingVerdict.HELD, 1.0, "again"))

    reloaded = store.get(prop.property_id)
    assert reloaded is not None
    assert reloaded.grading == HELD_HALF


@pytest.mark.parametrize("racer", [PropertyStatus.REJECTED, PropertyStatus.GRADED])
def test_grade_refuses_a_row_that_changed_after_it_was_read(
    monkeypatch: pytest.MonkeyPatch, racer: PropertyStatus
) -> None:
    store = mem_store()
    prop = make_prop(expression="a")
    store.add(prop)
    store._conn.execute(
        "UPDATE properties SET status = ?, grading_verdict = 'held', "
        "grading_kill_rate = 1.0, grading_reason = 'x' WHERE property_id = ?",
        (racer.value, prop.property_id),
    )
    store._conn.commit()
    real_read = store._read_status
    monkeypatch.setattr(
        store,
        "_read_status",
        lambda pid: (PropertyStatus.CANDIDATE, False),
    )

    with pytest.raises(InvalidStatusTransition):
        store.grade(prop.property_id, HELD_HALF)

    monkeypatch.setattr(store, "_read_status", real_read)
    stored = store.get(prop.property_id)
    assert stored is not None
    assert stored.status is racer
    assert stored.grading is not None
    assert stored.grading.reason == "x"


def test_graded_rows_always_carry_a_kill_rate() -> None:
    store = mem_store()
    store.add(make_prop(expression="a"))
    store.grade(make_prop(expression="a").property_id, HELD_HALF)
    store.add(make_prop(expression="b"))

    rows = store._conn.execute(
        "SELECT grading_kill_rate FROM properties WHERE status = 'graded'"
    ).fetchall()
    assert rows
    assert all(row["grading_kill_rate"] is not None for row in rows)


def test_trigger_guards_graded_without_grading_against_raw_sql() -> None:
    # The Python checks are the API; the triggers are the backstop for any other
    # writer, and they exist on a database created before they did.
    store = mem_store()
    store.add(make_prop())
    with pytest.raises(sqlite3.IntegrityError, match="requires a grading"):
        store._conn.execute("UPDATE properties SET status = 'graded'")
    with pytest.raises(sqlite3.IntegrityError, match="requires a grading"):
        store._conn.execute(
            "INSERT INTO properties "
            "(property_id, unit_id, kind, expression, status, "
            " prompt_id, prompt_version) "
            "VALUES ('id', 'u::f', 'semantic', 'e', 'graded', 'p', '1')"
        )


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
        mem_store().update_status("nope", PropertyStatus.REJECTED)


def test_terminal_status_is_enforced() -> None:
    store = mem_store()
    prop = make_prop()
    store.add(prop)
    store.grade(prop.property_id, HELD_HALF)
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


def test_recipe_round_trips_through_the_store(tmp_path: Path) -> None:
    prop = dataclasses.replace(
        make_prop(), provenance=Provenance("semantic", "1", "p", "m", "blind/v1")
    )
    with PropertyStore.open(tmp_path) as store:
        store.add(prop)
    with PropertyStore.open(tmp_path) as store:
        got = store.get(prop.property_id)
    assert got is not None
    assert got.provenance == prop.provenance
    assert got.provenance.to_dict()["recipe"] == "blind/v1"


def test_ensure_schema_migrates_a_pre_recipe_db_keeping_its_rows(
    tmp_path: Path,
) -> None:
    """A `forseti.db` written before #388 has no `recipe` column; its rows keep
    reading back with no recipe and new rows can carry one."""
    db_path = tmp_path / "forseti.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE properties (
            property_id       TEXT PRIMARY KEY,
            unit_id           TEXT NOT NULL,
            kind              TEXT NOT NULL,
            expression        TEXT NOT NULL,
            domain            TEXT NOT NULL DEFAULT '[]',
            description       TEXT,
            status            TEXT NOT NULL,
            prompt_id         TEXT NOT NULL,
            prompt_version    TEXT NOT NULL,
            provider          TEXT NOT NULL DEFAULT '',
            model             TEXT NOT NULL DEFAULT '',
            grading_verdict   TEXT,
            grading_kill_rate REAL,
            grading_reason    TEXT
        );
        INSERT INTO properties (property_id, unit_id, kind, expression, status,
                                prompt_id, prompt_version, provider, model)
        VALUES ('old', 'u.c::f', 'semantic', 'result > 0', 'candidate',
                'semantic', '1', 'claude -p', 'sonnet');
        """
    )
    conn.commit()
    conn.close()

    with PropertyStore(sqlite3.connect(db_path)) as store:
        old = store.get("old")
        assert old is not None
        assert old.provenance == Provenance("semantic", "1", "claude -p", "sonnet")
        assert old.provenance.recipe == ""

        new = dataclasses.replace(
            make_prop(), provenance=Provenance("semantic", "1", "p", "m", "sighted/v1")
        )
        store.add(new)
        got = store.get(new.property_id)
        assert got is not None
        assert got.provenance.recipe == "sighted/v1"

    with PropertyStore(sqlite3.connect(db_path)) as reopened:
        assert reopened.get("old") is not None


TYPE_KEY = "examples/ring_buffer.c::ring_t"
INV = "self->count <= CAP"


def make_inv(
    *,
    type_key: str = TYPE_KEY,
    expression: str = INV,
    status: PropertyStatus = PropertyStatus.CANDIDATE,
) -> Property:
    inv = make_invariant(
        type_key=type_key,
        expression=expression,
        provenance=Provenance("host-submitted", "1", provider="hand", model="human"),
        description="count never exceeds CAP",
    )
    return dataclasses.replace(inv, status=status)


def test_type_invariant_roundtrips_through_its_own_table() -> None:
    store = mem_store()
    inv = make_inv()
    store.add_type_invariant(inv)
    assert store.list_for_type(TYPE_KEY) == (inv,)
    assert store.list_for_type("examples/other.c::ring_t") == ()


def test_type_invariants_are_per_type_and_invisible_to_unit_reads() -> None:
    store = mem_store()
    a, b = make_inv(), make_inv(type_key="examples/other.c::ring_t")
    unit = make_prop(unit_id=TYPE_KEY)
    store.add_type_invariant(a)
    store.add_type_invariant(b)
    store.add(unit)
    assert store.list_for_type(TYPE_KEY) == (a,)
    assert store.list_for_unit(TYPE_KEY) == (unit,)
    assert store.get(a.property_id) is None  # not a per-unit property


def test_list_for_type_keeps_insertion_order_and_filters_by_status() -> None:
    store = mem_store()
    first = make_inv(expression="self->count <= CAP")
    second = make_inv(expression="self->tail < CAP", status=PropertyStatus.ACCEPTED)
    store.add_type_invariant(first)
    store.add_type_invariant(second)
    assert store.list_for_type(TYPE_KEY) == (first, second)
    assert store.list_for_type(TYPE_KEY, {PropertyStatus.ACCEPTED}) == (second,)
    assert store.list_for_type(TYPE_KEY, ()) == ()


def test_duplicate_type_invariant_is_refused() -> None:
    store = mem_store()
    store.add_type_invariant(make_inv())
    with pytest.raises(DuplicateProperty):
        store.add_type_invariant(make_inv())


def test_kinds_are_routed_not_coerced() -> None:
    store = mem_store()
    with pytest.raises(WrongPropertyKind, match="add_type_invariant"):
        store.add(make_inv())
    with pytest.raises(WrongPropertyKind, match="add"):
        store.add_type_invariant(make_prop())
    with pytest.raises(GradingRequired):
        store.add_type_invariant(make_inv(status=PropertyStatus.GRADED))
    assert store.list_for_type(TYPE_KEY) == ()


def test_open_migrates_an_existing_db_by_adding_the_type_table(tmp_path: Path) -> None:
    """A `forseti.db` from before #387 has `properties` but no invariant table.

    Opening it must keep every existing row and gain the table in place; the
    `properties.kind` CHECK is untouched (no table rebuild).
    """
    store = PropertyStore.open(root=tmp_path)
    old = make_prop()
    store.add(old)
    store.close()
    conn = sqlite3.connect(tmp_path / "forseti.db")
    conn.execute("DROP TABLE type_invariants")
    conn.commit()
    conn.close()

    reopened = PropertyStore.open(root=tmp_path)
    try:
        assert reopened.get(old.property_id) == old
        inv = make_inv()
        reopened.add_type_invariant(inv)
        assert reopened.list_for_type(TYPE_KEY) == (inv,)
    finally:
        reopened.close()


def test_open_is_idempotent_over_an_existing_type_table(tmp_path: Path) -> None:
    for _ in range(2):
        PropertyStore.open(root=tmp_path).close()
    store = PropertyStore.open(root=tmp_path)
    try:
        store.add_type_invariant(make_inv())
    finally:
        store.close()
    with PropertyStore.open(root=tmp_path) as again:
        assert len(again.list_for_type(TYPE_KEY)) == 1


def test_type_invariant_is_accepted_or_rejected_and_then_final() -> None:
    store = mem_store()
    keep = make_inv(expression="self->count <= CAP")
    drop = make_inv(expression="self->tail < CAP")
    store.add_type_invariant(keep)
    store.add_type_invariant(drop)

    accepted = store.update_type_invariant_status(
        keep.property_id, PropertyStatus.ACCEPTED
    )
    rejected = store.update_type_invariant_status(
        drop.property_id, PropertyStatus.REJECTED
    )

    assert accepted == dataclasses.replace(keep, status=PropertyStatus.ACCEPTED)
    assert rejected == dataclasses.replace(drop, status=PropertyStatus.REJECTED)
    assert store.list_for_type(TYPE_KEY, {PropertyStatus.ACCEPTED}) == (accepted,)
    for target in (PropertyStatus.REJECTED, PropertyStatus.ACCEPTED):
        with pytest.raises(InvalidStatusTransition):
            store.update_type_invariant_status(keep.property_id, target)
        with pytest.raises(InvalidStatusTransition):
            store.update_type_invariant_status(drop.property_id, target)
    assert store.list_for_type(TYPE_KEY) == (accepted, rejected)


@pytest.mark.parametrize("target", [PropertyStatus.GRADED, PropertyStatus.CANDIDATE])
def test_type_invariant_refuses_grading_and_self_transitions(
    target: PropertyStatus,
) -> None:
    store = mem_store()
    inv = make_inv()
    store.add_type_invariant(inv)
    with pytest.raises(InvalidStatusTransition):
        store.update_type_invariant_status(inv.property_id, target)
    assert store.list_for_type(TYPE_KEY) == (inv,)


def test_type_invariant_status_of_an_unknown_or_unit_property_is_not_found() -> None:
    store = mem_store()
    unit = make_prop()
    store.add(unit)
    for property_id in ("nope", unit.property_id):
        with pytest.raises(PropertyNotFound):
            store.update_type_invariant_status(property_id, PropertyStatus.ACCEPTED)
    assert store.get(unit.property_id) == unit


def test_unit_status_updates_never_touch_type_invariants() -> None:
    store = mem_store()
    inv = make_inv()
    store.add_type_invariant(inv)
    with pytest.raises(PropertyNotFound):
        store.update_status(inv.property_id, PropertyStatus.REJECTED)
    assert store.list_for_type(TYPE_KEY) == (inv,)


def test_list_for_type_prefix_reads_every_type_of_one_file() -> None:
    store = mem_store()
    ring = make_inv(status=PropertyStatus.ACCEPTED)
    node = make_inv(
        type_key="examples/ring_buffer.c::struct node",
        expression="self->v >= 0",
        status=PropertyStatus.ACCEPTED,
    )
    pending = make_inv(expression="self->tail < CAP")
    other = make_inv(type_key="examples/ring_buffer.cc::ring_t")
    for inv in (ring, node, pending, other):
        store.add_type_invariant(inv)

    prefix = "examples/ring_buffer.c::"
    assert store.list_for_type_prefix(prefix) == (ring, node, pending)
    assert store.list_for_type_prefix(prefix, {PropertyStatus.ACCEPTED}) == (
        ring,
        node,
    )
    assert store.list_for_type_prefix(prefix, ()) == ()
    assert store.list_for_type_prefix("examples/none.c::") == ()


def test_list_for_type_prefix_treats_sql_wildcards_literally() -> None:
    store = mem_store()
    store.add_type_invariant(make_inv(type_key="a_c.c::ring_t"))
    assert store.list_for_type_prefix("a%c.c::") == ()
    assert store.list_for_type_prefix("a_c.c::") != ()


def test_get_type_invariant_reads_only_the_type_table() -> None:
    store = mem_store()
    inv, unit = make_inv(), make_prop()
    store.add_type_invariant(inv)
    store.add(unit)
    assert store.get_type_invariant(inv.property_id) == inv
    assert store.get_type_invariant(unit.property_id) is None
    assert store.get_type_invariant("nope") is None

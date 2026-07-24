"""SQLite-backed store of proposed properties, keyed by unit (`path::symbol`).

Supersedes the SQLite-deferred note in `orchestrator/persistence.py` (JSONL was
kept "until a query workload actually needs it" -- per-unit property lookup with
lifecycle updates is that workload). The two coexist: JSONL is the append-only
run trace; this is the queryable property state. Stdlib only (`sqlite3`, `json`)
-- no new dependency, `.forseti/forseti.db` per ADR-0009 D1.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Collection
from pathlib import Path
from typing import Any

from .model import (
    Grading,
    GradingVerdict,
    InvalidStatusTransition,
    Property,
    PropertyKind,
    PropertyStatus,
    Provenance,
    is_valid_transition,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS properties (
    property_id       TEXT PRIMARY KEY,
    unit_id           TEXT NOT NULL,
    kind              TEXT NOT NULL CHECK (kind IN ('semantic','reachability')),
    expression        TEXT NOT NULL,
    domain            TEXT NOT NULL DEFAULT '[]',
    description       TEXT,
    status            TEXT NOT NULL
                        CHECK (status IN ('candidate','graded','accepted','rejected')),
    prompt_id         TEXT NOT NULL,
    prompt_version    TEXT NOT NULL,
    grading_verdict   TEXT CHECK (grading_verdict IN ('held','violated','unknown')),
    grading_kill_rate REAL,
    grading_reason    TEXT,
    created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    -- grading is all-or-nothing (reason may stay NULL when graded):
    CHECK ((grading_verdict IS NULL) = (grading_kill_rate IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_properties_unit        ON properties(unit_id);
CREATE INDEX IF NOT EXISTS idx_properties_unit_status ON properties(unit_id, status);
"""

_INSERT = """
INSERT INTO properties (
    property_id, unit_id, kind, expression, domain, description, status,
    prompt_id, prompt_version, grading_verdict, grading_kill_rate, grading_reason
) VALUES (
    :property_id, :unit_id, :kind, :expression, :domain, :description, :status,
    :prompt_id, :prompt_version, :grading_verdict, :grading_kill_rate, :grading_reason
)
"""


class PropertyStoreError(Exception):
    """Base for store-level errors."""


class DuplicateProperty(PropertyStoreError):
    """`add` of a `property_id` already present (content-id collision = dedup)."""

    def __init__(self, property_id: str) -> None:
        self.property_id = property_id
        super().__init__(f"property already stored: {property_id}")


class PropertyNotFound(PropertyStoreError):
    """A mutation named a `property_id` the store does not hold."""

    def __init__(self, property_id: str) -> None:
        self.property_id = property_id
        super().__init__(f"no such property: {property_id}")


def _property_to_row(prop: Property) -> dict[str, Any]:
    grading = prop.grading
    return {
        "property_id": prop.property_id,
        "unit_id": prop.unit_id,
        "kind": prop.kind.value,
        "expression": prop.expression,
        "domain": json.dumps(list(prop.domain)),
        "description": prop.description,
        "status": prop.status.value,
        "prompt_id": prop.provenance.prompt_id,
        "prompt_version": prop.provenance.prompt_version,
        "grading_verdict": grading.verdict.value if grading is not None else None,
        "grading_kill_rate": grading.kill_rate if grading is not None else None,
        "grading_reason": grading.reason if grading is not None else None,
    }


def _row_to_property(row: sqlite3.Row) -> Property:
    verdict = row["grading_verdict"]
    grading = (
        None
        if verdict is None
        else Grading(
            verdict=GradingVerdict(verdict),
            kill_rate=float(row["grading_kill_rate"]),
            reason=row["grading_reason"],
        )
    )
    return Property(
        property_id=str(row["property_id"]),
        unit_id=str(row["unit_id"]),
        kind=PropertyKind(row["kind"]),
        expression=str(row["expression"]),
        status=PropertyStatus(row["status"]),
        provenance=Provenance(
            prompt_id=str(row["prompt_id"]),
            prompt_version=str(row["prompt_version"]),
        ),
        domain=tuple(str(item) for item in json.loads(row["domain"])),
        grading=grading,
        description=row["description"],
    )


class PropertyStore:
    """SQLite-backed store of proposed properties, keyed by unit (`path::symbol`)."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        connection.row_factory = sqlite3.Row
        self._conn = connection
        self._ensure_schema()

    @classmethod
    def open(cls, root: Path = Path(".forseti")) -> PropertyStore:
        """Production factory: resolve `root/forseti.db`, creating `root` if absent."""
        root.mkdir(parents=True, exist_ok=True)
        return cls(sqlite3.connect(root / "forseti.db"))

    def _ensure_schema(self) -> None:
        with self._conn:
            self._conn.executescript(_SCHEMA)
            self._conn.execute("PRAGMA user_version = 1")

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> PropertyStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def add(self, prop: Property) -> None:
        """Insert `prop`; raise `DuplicateProperty` if its id is already stored."""
        try:
            with self._conn:
                self._conn.execute(_INSERT, _property_to_row(prop))
        except sqlite3.IntegrityError as exc:
            raise DuplicateProperty(prop.property_id) from exc

    def get(self, property_id: str) -> Property | None:
        """Return the property with `property_id`, or None if absent."""
        cur = self._conn.execute(
            "SELECT * FROM properties WHERE property_id = ?", (property_id,)
        )
        row = cur.fetchone()
        return None if row is None else _row_to_property(row)

    def list_for_unit(
        self,
        unit_id: str,
        statuses: Collection[PropertyStatus] | None = None,
    ) -> tuple[Property, ...]:
        """Properties for `unit_id`, in insertion order; `()` if none.

        `statuses` scopes the read to a lifecycle subset (covered by the
        `idx_properties_unit_status` index): `None` returns every row (the
        default, unchanged); a collection returns only rows whose status is in
        it, and an *empty* collection returns `()` (no status is selected).
        Callers that must not feed terminal rows into a downstream verdict
        (`check_properties`, #84) pass the valid-input subset here rather than
        filter after the read.
        """
        if statuses is None:
            cur = self._conn.execute(
                "SELECT * FROM properties WHERE unit_id = ? ORDER BY rowid", (unit_id,)
            )
        elif not statuses:
            return ()
        else:
            placeholders = ",".join("?" * len(statuses))
            cur = self._conn.execute(
                "SELECT * FROM properties WHERE unit_id = ? "
                f"AND status IN ({placeholders}) ORDER BY rowid",
                (unit_id, *(status.value for status in statuses)),
            )
        return tuple(_row_to_property(row) for row in cur.fetchall())

    def update_status(self, property_id: str, new_status: PropertyStatus) -> Property:
        """Atomically validate and apply a status transition; return the result.

        Raises `PropertyNotFound` (unknown id) or `InvalidStatusTransition`
        (disallowed move) -- both before any write, so a rejected transition
        leaves the row untouched.
        """
        with self._conn:
            cur = self._conn.execute(
                "SELECT status FROM properties WHERE property_id = ?", (property_id,)
            )
            row = cur.fetchone()
            if row is None:
                raise PropertyNotFound(property_id)
            current = PropertyStatus(row["status"])
            if not is_valid_transition(current, new_status):
                raise InvalidStatusTransition(current, new_status)
            self._conn.execute(
                "UPDATE properties SET status = ? WHERE property_id = ?",
                (new_status.value, property_id),
            )
        updated = self.get(property_id)
        assert updated is not None  # just written under the same connection
        return updated

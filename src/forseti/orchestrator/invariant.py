"""The two seams that let `check_properties` check a representation invariant (#387).

`check_properties` renders and verifies whatever its `HarnessWriterPort` renders for
the properties its `PropertyStorePort` lists for the unit, so a representation
invariant needs only a writer and a store view -- the k-ladder, the post-call
vacuity probe (a vacuous invariant such as ``0`` comes back ``vacuous``, never
``held``) and the verdict shape are the driver's, unchanged.

`InvariantHarnessWriter` renders one invariant against the function under check;
`InvariantsOfType` lists the invariants stored for the *type* whatever unit asks,
because an invariant is per type and shared by every function taking that type.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence

from forseti.properties import (
    HarnessError,
    InvariantRole,
    Property,
    PropertyKind,
    PropertyStatus,
    PropertyStore,
    build_invariant_harness,
)
from forseti.unit_id import unit_id_symbol

from .ports import RenderedHarness, Unit


class InvariantHarnessWriter:
    """The `HarnessWriterPort` for `REP_INVARIANT` properties.

    `role` defaults to the one `infer_role` reads off the function name; `max_len` is
    the semantic writer's `(ptr, len)` cap (#299). A property of any other kind is a
    `HarnessError` -- reported per property as an ``error``, never rendered as
    something it is not.
    """

    def __init__(
        self, max_len: int | None = None, role: InvariantRole | None = None
    ) -> None:
        self._max_len = max_len
        self._role = role

    def render(self, unit: Unit, prop: Property) -> RenderedHarness:
        if prop.kind is not PropertyKind.REP_INVARIANT:
            raise HarnessError(
                f"cannot render a {prop.kind.value} property as a representation "
                "invariant"
            )
        harness = build_invariant_harness(
            unit_source=unit.source_text,
            symbol=unit.symbol,
            type_name=unit_id_symbol(prop.unit_id),
            expression=prop.expression,
            role=self._role,
            max_len=self._max_len,
        )
        return RenderedHarness(
            source_text=harness.source_text,
            length_bounds=harness.length_bounds,
            probe_source_text=harness.probe_source_text,
        )


class InvariantsOfType:
    """A `PropertyStorePort` over the invariants of one type (`path::T`)."""

    def __init__(self, store: PropertyStore, type_key: str) -> None:
        self._store = store
        self._type_key = type_key

    def list_for_unit(
        self,
        unit_id: str,
        statuses: Collection[PropertyStatus] | None = None,
    ) -> Sequence[Property]:
        return self._store.list_for_type(self._type_key, statuses)

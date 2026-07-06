"""Property store: typed properties + per-project SQLite storage (W2.1, #62).

A `Property` is a checkable predicate proposed for one verification unit
(`path::symbol`), persisted in `.forseti/forseti.db` (ADR-0009 D1). Pure model +
storage: no LLM (the proposer is #65/#44), no ESBMC (the harness writer is #64).
The store is the data-model foundation the rest of W2 (Epic #3) hangs off.
"""

from .model import (
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
from .store import (
    DuplicateProperty,
    PropertyNotFound,
    PropertyStore,
    PropertyStoreError,
)

__all__ = [
    "DuplicateProperty",
    "Grading",
    "GradingVerdict",
    "InvalidStatusTransition",
    "Property",
    "PropertyKind",
    "PropertyNotFound",
    "PropertyStatus",
    "PropertyStore",
    "PropertyStoreError",
    "Provenance",
    "is_valid_transition",
    "make_property_id",
]

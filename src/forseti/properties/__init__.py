"""Property store + harness writer: typed properties, SQLite storage, and the
render-to-ESBMC-harness seam (W2.1 #62, W2.3 #64).

A `Property` is a checkable predicate proposed for one verification unit
(`path::symbol`), persisted in `.forseti/forseti.db` (ADR-0009 D1). The model and
store are pure data (no LLM -- the proposer is #65/#44; no ESBMC). `harness`
turns a semantic Property + the unit's signature into compilable ESBMC C *text*,
staying effect-free like `orchestrator.fix` so the loop and tests remain pure.
Together they are the data-model foundation the rest of W2 (Epic #3) hangs off.
"""

from .harness import (
    BufferParam,
    HarnessError,
    Param,
    ScalarParam,
    SemanticSpec,
    UnitSignature,
    extract_signature,
    render_semantic_harness,
    spec_from_property,
)
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
    "BufferParam",
    "DuplicateProperty",
    "Grading",
    "GradingVerdict",
    "HarnessError",
    "InvalidStatusTransition",
    "Param",
    "Property",
    "PropertyKind",
    "PropertyNotFound",
    "PropertyStatus",
    "PropertyStore",
    "PropertyStoreError",
    "Provenance",
    "ScalarParam",
    "SemanticSpec",
    "UnitSignature",
    "extract_signature",
    "is_valid_transition",
    "make_property_id",
    "render_semantic_harness",
    "spec_from_property",
]

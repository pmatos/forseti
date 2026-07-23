"""Property store, harness writer, and LLM proposer: the W2 property pipeline.

A `Property` is a checkable predicate proposed for one verification unit
(`path::symbol`), persisted in `.forseti/forseti.db` (ADR-0009 D1). `model` and
`store` are pure data (no LLM, no ESBMC). `harness` turns a semantic Property +
the unit's signature into compilable ESBMC C *text*, staying effect-free like
`orchestrator.fix`. `proposer` closes the loop (W2.4 #65): it asks an `LLMClient`
(`llm`) for candidate properties under a versioned `prompts` artifact, validates
them, and stores the survivors as candidates -- the one effectful step, behind an
injected client seam so tests stay hermetic. Together they are the property
pipeline the rest of W2 (Epic #3) hangs off.
"""

from .harness import (
    DEFAULT_INCLUDES,
    HARNESS_MACROS,
    BufferParam,
    HarnessError,
    Param,
    ScalarParam,
    SemanticSpec,
    UnitSignature,
    extract_signature,
    render_property_harness,
    render_semantic_harness,
    renderability_reason,
    spec_from_property,
)
from .llm import ClaudeCliClient, LLMClient, LLMError
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
from .prompts import (
    DEFAULT_PROMPT,
    MAX_CANDIDATES_DEFAULT,
    PROMPTS,
    RESULT_IDENT,
    SEMANTIC_V1,
    PromptTemplate,
    render_prompt,
)
from .proposer import (
    CandidateSpec,
    CandidateStore,
    HarnessRenderer,
    ProposalParseError,
    ProposalRequest,
    ProposalResult,
    RejectedCandidate,
    parse_candidates,
    propose_properties,
    validate_candidate,
)
from .store import (
    DuplicateProperty,
    PropertyNotFound,
    PropertyStore,
    PropertyStoreError,
)

__all__ = [
    "DEFAULT_INCLUDES",
    "DEFAULT_PROMPT",
    "HARNESS_MACROS",
    "MAX_CANDIDATES_DEFAULT",
    "PROMPTS",
    "RESULT_IDENT",
    "SEMANTIC_V1",
    "BufferParam",
    "CandidateSpec",
    "CandidateStore",
    "ClaudeCliClient",
    "DuplicateProperty",
    "Grading",
    "GradingVerdict",
    "HarnessError",
    "HarnessRenderer",
    "InvalidStatusTransition",
    "LLMClient",
    "LLMError",
    "Param",
    "PromptTemplate",
    "Property",
    "PropertyKind",
    "PropertyNotFound",
    "PropertyStatus",
    "PropertyStore",
    "PropertyStoreError",
    "ProposalParseError",
    "ProposalRequest",
    "ProposalResult",
    "Provenance",
    "RejectedCandidate",
    "ScalarParam",
    "SemanticSpec",
    "UnitSignature",
    "extract_signature",
    "is_valid_transition",
    "make_property_id",
    "parse_candidates",
    "propose_properties",
    "render_prompt",
    "render_property_harness",
    "render_semantic_harness",
    "renderability_reason",
    "spec_from_property",
    "validate_candidate",
]

"""The property proposer: unit source in, validated candidate properties out.

W2's headline (#65): stop hand-writing properties. `propose_properties` renders
the versioned prompt (`prompts.py`), asks an `LLMClient` (`llm.py`) for candidate
semantic properties, parses and *statically* validates them, and persists the
survivors as `status=CANDIDATE` (#62). No grading here -- kill-rate scoring is
#4, prompt evolution is #5, the CLI/MCP face is #44; this module only turns a
unit into well-formed candidates.

Validation is static and shape-only (no ESBMC, no execution): it rejects unsafe
or vacuous expressions and -- when a `UnitSignature` is supplied -- expressions
that name non-existent identifiers or that would not render to sound C. That last
gate is `renderability_reason` (`harness.py`), the single static authority on
renderability; the proposer consults it directly rather than rendering a trial
harness, so there is no renderer to inject. Effect-free by default (no store ->
pure); the LLM call is the one effect, behind the injected `client` seam so unit
tests stay hermetic. The harness types are a plain runtime import: `harness`
depends only on `model`, so there is no import cycle to route around.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from .cexpr import identifiers, unsafe_reason

# The macro allowlist (`HARNESS_MACROS`) is owned by the harness writer -- the
# module that emits the includes -- so the accepted-without-declaration set and
# the emitted headers cannot drift out of lockstep (#81).
from .harness import (
    HARNESS_MACROS,
    BufferParam,
    SemanticSpec,
    UnitSignature,
    renderability_reason,
)
from .llm import LLMClient
from .model import (
    Property,
    PropertyKind,
    PropertyStatus,
    Provenance,
    make_property_id,
)
from .prompts import DEFAULT_PROMPT, MAX_CANDIDATES_DEFAULT, RESULT_IDENT, render_prompt
from .store import DuplicateProperty

if TYPE_CHECKING:
    from .prompts import PromptTemplate
    from .store import PropertyStore


class CandidateStore(Protocol):
    """The store subset the proposer persists into (real `PropertyStore` fits).

    Structural: only the two members the proposer touches, so tests inject a
    dict-backed fake and #62's concrete store need not be built to exercise #65.
    """

    def get(self, property_id: str) -> Property | None: ...

    def add(self, prop: Property) -> None: ...


@dataclass(frozen=True)
class ProposalRequest:
    """One unit to propose properties for: its id, source, prompt, and signature.

    `signature` is caller-supplied (parsed via `extract_signature` or hand-built):
    when present it unlocks the identifier/parameter checks and the renderability
    gate; when `None` those are skipped and only the source-free static checks
    apply. `source_text` must be a main-free kernel slice -- the same text the
    harness writer inlines.
    """

    unit_id: str
    source_text: str
    prompt: PromptTemplate = DEFAULT_PROMPT
    signature: UnitSignature | None = None

    @property
    def symbol(self) -> str:
        """The function name -- the ``symbol`` half of ``path::symbol``."""
        return self.unit_id.split("::", 1)[1] if "::" in self.unit_id else self.unit_id


@dataclass(frozen=True)
class CandidateSpec:
    """One parsed model candidate, before validation.

    Mirrors the JSON contract the prompt asks for. `kind` is captured only so a
    stray non-semantic candidate can be rejected (ADR-0009 D2); the prompt never
    asks for it, so it is usually `None`.
    """

    expression: str
    domain: tuple[str, ...] = ()
    referenced_params: tuple[str, ...] = ()
    rationale: str = ""
    kind: str | None = None


@dataclass(frozen=True)
class RejectedCandidate:
    """A parsed candidate that failed a static check, with the reason it failed.

    Rejections are *returned*, not raised: a bad candidate is data about the
    model's output, not an error in the proposer.
    """

    spec: CandidateSpec
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "expression": self.spec.expression,
            "domain": list(self.spec.domain),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ProposalResult:
    """The outcome of one proposer run: accepted properties, rejects, provenance.

    `to_dict` is the JSON shape #44 serialises (mirrors `core/verify.py`'s
    payload style). `provider`/`model` record the backend for telemetry; the
    persisted `Property.provenance` carries only prompt id/version by design.
    """

    unit_id: str
    prompt_id: str
    prompt_version: str
    provider: str
    model: str
    accepted: tuple[Property, ...]
    rejected: tuple[RejectedCandidate, ...]
    model_raw: str

    def to_dict(self) -> dict[str, object]:
        return {
            "unit_id": self.unit_id,
            "prompt_id": self.prompt_id,
            "prompt_version": self.prompt_version,
            "provider": self.provider,
            "model": self.model,
            "accepted": [prop.to_dict() for prop in self.accepted],
            "rejected": [rej.to_dict() for rej in self.rejected],
        }


class ProposalParseError(ValueError):
    """The model output was not a well-formed candidate envelope (fail-loud)."""


def propose_properties(
    request: ProposalRequest,
    *,
    client: LLMClient,
    store: CandidateStore | None = None,
    max_candidates: int = MAX_CANDIDATES_DEFAULT,
) -> ProposalResult:
    """Propose candidate properties for `request`'s unit and (optionally) store them.

    Renders the prompt, calls `client.complete` (an `LLMError` propagates -- the
    proposer never silently yields nothing), parses the reply (a
    `ProposalParseError` propagates), then for each candidate: enforces the
    static checks (`validate_candidate`, which -- given a `signature` -- rejects
    candidates that would not render to sound C), builds a `status=CANDIDATE`
    `Property`, and drops batch duplicates by content id. When a `store` is given,
    each survivor is inserted idempotently (its content id makes a re-run a no-op).
    """
    template = request.prompt
    prompt = render_prompt(
        template,
        unit_id=request.unit_id,
        symbol=request.symbol,
        source_text=request.source_text,
        result_ident=RESULT_IDENT,
        max_candidates=max_candidates,
    )
    raw = client.complete(prompt)
    specs = parse_candidates(raw)
    signature = request.signature
    provenance = Provenance(
        prompt_id=template.prompt_id, prompt_version=template.version
    )

    accepted: list[Property] = []
    rejected: list[RejectedCandidate] = []
    seen: set[str] = set()
    for spec in specs:
        if len(accepted) >= max_candidates:
            rejected.append(RejectedCandidate(spec, "over max_candidates"))
            continue
        reason = validate_candidate(spec, signature)
        if reason is not None:
            rejected.append(RejectedCandidate(spec, reason))
            continue
        prop = Property(
            property_id=make_property_id(
                request.unit_id, PropertyKind.SEMANTIC, spec.expression, spec.domain
            ),
            unit_id=request.unit_id,
            kind=PropertyKind.SEMANTIC,
            expression=spec.expression,
            status=PropertyStatus.CANDIDATE,
            provenance=provenance,
            domain=spec.domain,
            description=spec.rationale or None,
        )
        if prop.property_id in seen:
            rejected.append(RejectedCandidate(spec, "duplicate in batch"))
            continue
        seen.add(prop.property_id)
        accepted.append(prop)

    if store is not None:
        for prop in accepted:
            if store.get(prop.property_id) is None:
                # a racing writer may insert the same content id first; idempotent
                with contextlib.suppress(DuplicateProperty):
                    store.add(prop)

    return ProposalResult(
        unit_id=request.unit_id,
        prompt_id=template.prompt_id,
        prompt_version=template.version,
        provider=client.provider,
        model=client.model,
        accepted=tuple(accepted),
        rejected=tuple(rejected),
        model_raw=raw,
    )


def parse_candidates(model_text: str) -> tuple[CandidateSpec, ...]:
    """Parse a model reply into candidate specs; `ProposalParseError` if malformed.

    Tolerates a markdown ```` ```json ... ``` ```` fence and either a
    ``{"candidates":[...]}`` object or a bare top-level list. Every element must
    be an object with a non-empty string ``expression``; ``domain`` /
    ``referenced_params`` (string lists) and ``rationale`` / ``kind`` (strings)
    are optional. Anything else fails loud -- never a silent empty result.
    """
    text = _strip_fences(model_text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProposalParseError(f"model output is not JSON: {exc}") from exc
    if isinstance(data, dict):
        elements = data.get("candidates")
        if elements is None:
            raise ProposalParseError('model JSON object lacks a "candidates" key')
    elif isinstance(data, list):
        elements = data
    else:
        raise ProposalParseError("model JSON is neither an object nor a list")
    if not isinstance(elements, list):
        raise ProposalParseError('"candidates" is not a list')
    return tuple(_parse_element(elem, i) for i, elem in enumerate(elements))


def validate_candidate(
    spec: CandidateSpec, signature: UnitSignature | None
) -> str | None:
    """None if `spec` passes every static check, else the first failure's reason.

    Static and shape-only -- no ESBMC, no execution, no grading. V1 kind (reject
    non-semantic), V2 safety (no statements/assignments/calls, balanced parens),
    V5 non-vacuity (the expression must constrain the result or a parameter; a
    precondition must not mention the result and must constrain something) always
    apply. V3 identifiers, V4 `referenced_params`, and V6 renderability need a
    `signature` and are skipped when it is `None`; V6 delegates to
    `renderability_reason`, the harness's single static authority on whether the
    `(signature, spec)` pair renders to sound C.
    """
    if spec.kind is not None and spec.kind != PropertyKind.SEMANTIC.value:
        return f"non-semantic kind {spec.kind!r} (semantic-only, ADR-0009 D2)"

    unsafe = unsafe_reason(spec.expression)
    if unsafe is not None:
        return f"unsafe expression: {unsafe}"
    for pre in spec.domain:
        unsafe = unsafe_reason(pre)
        if unsafe is not None:
            return f"unsafe domain expr {pre!r}: {unsafe}"

    expr_idents = identifiers(spec.expression)
    if RESULT_IDENT not in expr_idents and not (set(expr_idents) - HARNESS_MACROS):
        return "vacuous expression: references neither a parameter nor the result"
    for pre in spec.domain:
        pre_idents = identifiers(pre)
        if RESULT_IDENT in pre_idents:
            return f"domain expr {pre!r} references the result (unavailable pre-call)"
        if not (set(pre_idents) - HARNESS_MACROS):
            return f"vacuous domain expr {pre!r}: references no parameter"

    if signature is not None:
        params = {p.name for p in signature.params}
        output_params = {
            p.name for p in signature.params if isinstance(p, BufferParam) and p.out
        }
        input_params = params - output_params
        allowed = params | {RESULT_IDENT} | HARNESS_MACROS
        for ident in expr_idents:
            if ident not in allowed:
                return f"expression references unknown identifier {ident!r}"
        # The two signature-dependent emission rules -- a scalar-backed output must
        # be named directly (not `*cp`/`cp[0]`/`*(cp+0)`) and a precondition must
        # not constrain an output -- are owned by the harness writer, the module
        # that emits the C. Delegating keeps that harness knowledge in one place
        # and closes the propose-path evasion the old inline regex left open (#81).
        render_block = renderability_reason(
            signature, SemanticSpec(spec.expression, spec.domain)
        )
        if render_block is not None:
            return render_block
        for pre in spec.domain:
            for ident in identifiers(pre):
                if ident not in (input_params | HARNESS_MACROS):
                    return f"domain expr {pre!r} references unknown ident {ident!r}"
        for name in spec.referenced_params:
            if name not in params:
                return f"referenced_params includes non-parameter {name!r}"
    return None


def _strip_fences(text: str) -> str:
    """Drop a single surrounding markdown code fence, if present."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _parse_element(elem: object, index: int) -> CandidateSpec:
    if not isinstance(elem, dict):
        raise ProposalParseError(f"candidate {index} is not an object")
    expression = elem.get("expression")
    if not isinstance(expression, str) or not expression.strip():
        raise ProposalParseError(f"candidate {index} lacks a non-empty 'expression'")
    return CandidateSpec(
        expression=expression.strip(),
        domain=_str_tuple(elem.get("domain"), index, "domain"),
        referenced_params=_str_tuple(
            elem.get("referenced_params"), index, "referenced_params"
        ),
        rationale=_opt_str(elem.get("rationale")),
        kind=elem.get("kind") if isinstance(elem.get("kind"), str) else None,
    )


def _str_tuple(value: object, index: int, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ProposalParseError(
            f"candidate {index} field {field!r} must be a list of strings"
        )
    return tuple(v.strip() for v in value if v.strip())


def _opt_str(value: object) -> str:
    return value if isinstance(value, str) else ""


if TYPE_CHECKING:
    # mypy-only structural guard: fail type-checking if the concrete store ever
    # drifts from the protocol it must satisfy (mirrors fix.py).
    def _store_is_candidatestore(s: PropertyStore) -> CandidateStore:
        return s

"""The property proposer: unit source in, validated candidate properties out.

W2's headline (#65): stop hand-writing properties. `propose_properties` renders
the versioned prompt (`prompts.py`), asks an `LLMClient` (`llm.py`) for candidate
semantic properties, parses and *statically* validates them, and persists the
survivors as `status=CANDIDATE` (#62). No grading here -- kill-rate scoring is
#4, prompt evolution is #5, the CLI/MCP face is #44; this module only turns a
unit into well-formed candidates.

Validation is static and shape-only (no ESBMC, no execution): it rejects unsafe
or vacuous expressions and -- when a `UnitSignature` is supplied -- expressions
that name non-existent identifiers, and it gates each survivor through the #64
harness writer so only *renderable* candidates are kept. Effect-free by default
(no store, no renderer injected -> pure); the LLM call is the one effect, behind
the injected `client` seam so unit tests stay hermetic. The harness types are a
plain runtime import: `harness` depends only on `model`, so there is no import
cycle to route around.
"""

from __future__ import annotations

import contextlib
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from .harness import (
    HarnessError,
    SemanticSpec,
    UnitSignature,
    render_semantic_harness,
    spec_from_property,
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


class HarnessRenderer(Protocol):
    """The #64 renderer subset the renderability gate calls.

    `render_semantic_harness` satisfies it structurally, so the loop/#44 pass the
    real writer while tests inject a stub.
    """

    def __call__(
        self, *, unit_source: str, signature: UnitSignature, spec: SemanticSpec
    ) -> str: ...


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
    renderer: HarnessRenderer | None = None,
    store: CandidateStore | None = None,
    max_candidates: int = MAX_CANDIDATES_DEFAULT,
) -> ProposalResult:
    """Propose candidate properties for `request`'s unit and (optionally) store them.

    Renders the prompt, calls `client.complete` (an `LLMError` propagates -- the
    proposer never silently yields nothing), parses the reply (a
    `ProposalParseError` propagates), then for each candidate: enforces the
    static checks (`validate_candidate`), builds a `status=CANDIDATE` `Property`,
    drops batch duplicates by content id, and -- when a `renderer` and
    `signature` are both present -- keeps only candidates that render to a
    non-empty harness. When a `store` is given, each survivor is inserted
    idempotently (its content id makes a re-run a no-op).
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
        if renderer is not None and signature is not None:
            gate = _renderability(renderer, request.source_text, signature, prop)
            if gate is not None:
                rejected.append(RejectedCandidate(spec, gate))
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


def _renderability(
    renderer: HarnessRenderer,
    unit_source: str,
    signature: UnitSignature,
    prop: Property,
) -> str | None:
    """None if `prop` renders to a non-empty harness, else a rejection reason.

    A `HarnessError` is the writer's documented "can't render this" signal, so it
    becomes a rejection; any other exception is a real bug and is left to
    propagate rather than masqueraded as an unrenderable candidate.
    """
    try:
        rendered = renderer(
            unit_source=unit_source, signature=signature, spec=spec_from_property(prop)
        )
    except HarnessError as exc:
        return f"unrenderable: {exc}"
    return None if rendered.strip() else "empty harness"


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
    apply. V3 identifiers and V4 `referenced_params` need a `signature` and are
    skipped when it is `None`. V6 renderability is enforced separately, in the
    flow, where the harness writer is available.
    """
    if spec.kind is not None and spec.kind != PropertyKind.SEMANTIC.value:
        return f"non-semantic kind {spec.kind!r} (semantic-only, ADR-0009 D2)"

    unsafe = _unsafe(spec.expression)
    if unsafe is not None:
        return f"unsafe expression: {unsafe}"
    for pre in spec.domain:
        unsafe = _unsafe(pre)
        if unsafe is not None:
            return f"unsafe domain expr {pre!r}: {unsafe}"

    expr_idents = _identifiers(spec.expression)
    if RESULT_IDENT not in expr_idents and not (set(expr_idents) - _MACROS):
        return "vacuous expression: references neither a parameter nor the result"
    for pre in spec.domain:
        pre_idents = _identifiers(pre)
        if RESULT_IDENT in pre_idents:
            return f"domain expr {pre!r} references the result (unavailable pre-call)"
        if not (set(pre_idents) - _MACROS):
            return f"vacuous domain expr {pre!r}: references no parameter"

    if signature is not None:
        params = {p.name for p in signature.params}
        allowed = params | {RESULT_IDENT} | _MACROS
        for ident in expr_idents:
            if ident not in allowed:
                return f"expression references unknown identifier {ident!r}"
        for pre in spec.domain:
            for ident in _identifiers(pre):
                if ident not in (params | _MACROS):
                    return f"domain expr {pre!r} references unknown ident {ident!r}"
        for name in spec.referenced_params:
            if name not in params:
                return f"referenced_params includes non-parameter {name!r}"
    return None


# Standard limit macros (limits.h / stdint.h) a pure property may name without a
# declaration -- the renderer includes the headers that define them.
_MACROS = frozenset(
    [
        "NULL",
        "CHAR_MIN",
        "CHAR_MAX",
        "SCHAR_MIN",
        "SCHAR_MAX",
        "UCHAR_MAX",
        "SHRT_MIN",
        "SHRT_MAX",
        "USHRT_MAX",
        "INT_MIN",
        "INT_MAX",
        "UINT_MAX",
        "LONG_MIN",
        "LONG_MAX",
        "ULONG_MAX",
        "LLONG_MIN",
        "LLONG_MAX",
        "ULLONG_MAX",
        "INT8_MIN",
        "INT8_MAX",
        "UINT8_MAX",
        "INT16_MIN",
        "INT16_MAX",
        "UINT16_MAX",
        "INT32_MIN",
        "INT32_MAX",
        "UINT32_MAX",
        "INT64_MIN",
        "INT64_MAX",
        "UINT64_MAX",
        "INTMAX_MIN",
        "INTMAX_MAX",
        "UINTMAX_MAX",
        "INTPTR_MIN",
        "INTPTR_MAX",
        "UINTPTR_MAX",
        "SIZE_MAX",
        "SSIZE_MAX",
        "PTRDIFF_MIN",
        "PTRDIFF_MAX",
    ]
)

_NUMERIC_LITERAL = re.compile(r"\b0[xX][0-9A-Fa-f]+[uUlL]*\b|\b\d+[uUlL]*\b")
_IDENT = re.compile(r"[A-Za-z_]\w*")
_CALL = re.compile(r"[A-Za-z_]\w*\s*\(")
_RELATIONAL = re.compile(r"[=!<>]=")


def _identifiers(expr: str) -> list[str]:
    """Identifier tokens in `expr`, in first-seen order, numeric literals removed.

    Stripping literals first keeps a hex constant like ``0x1F`` from being misread
    as an identifier ``x1F``.
    """
    return _IDENT.findall(_NUMERIC_LITERAL.sub(" ", expr))


def _unsafe(expr: str) -> str | None:
    """None if `expr` is a pure C boolean expression, else why it is rejected.

    Blocks statement separators, assignments (a bare ``=`` that is not part of a
    relational operator), backticks, function calls, and unbalanced parentheses
    -- everything that would let a "property" smuggle in side effects.
    """
    if ";" in expr:
        return "contains ';'"
    if "`" in expr:
        return "contains a backtick"
    if "=" in _RELATIONAL.sub("", expr):
        return "contains an assignment"
    if _CALL.search(expr):
        return "contains a function call"
    if expr.count("(") != expr.count(")"):
        return "unbalanced parentheses"
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
    # mypy-only structural guards: fail type-checking if a concrete class/function
    # ever drifts from the protocol it must satisfy (mirrors fix.py).
    def _store_is_candidatestore(s: PropertyStore) -> CandidateStore:
        return s

    def _renderer_is_harnessrenderer() -> HarnessRenderer:
        return render_semantic_harness

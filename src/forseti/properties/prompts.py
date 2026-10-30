"""Versioned proposer prompts -- the artifact GEPA (#5) will later evolve.

The proposer (#65) turns a verification unit (`path::symbol` + its C source) into
candidate `Property` objects by asking an LLM. The prompt that does the asking is
a *first-class, versioned artifact*: every proposed property records the
`prompt_id`+`prompt_version` that produced it (via `Provenance`), so a graded
property is always traceable to the exact prompt text. GEPA (#5) evolves a prompt
by registering a *new* `PromptTemplate` with a bumped `version`; a shipped
version's text is immutable, mirroring ADR immutability.

v1 proposes SEMANTIC properties only (postconditions + preconditions);
reachability harnessing is deferred (ADR-0009 D2). `rep-invariant/v1` asks for a
representation invariant over one struct type instead (RFC-0007 kind 1, #420),
written over the reserved identifier `self`. The model is told to name the
call's return value `result` -- the identifier `render_semantic_harness` binds it
to (`SemanticSpec.result_var` default). Pure data + string synthesis: no LLM call
here (that is `llm.py`), stdlib only.
"""

from __future__ import annotations

from dataclasses import dataclass

# The identifier the model must use for the unit's return value in a
# postcondition. Equal to `SemanticSpec.result_var`'s default so a proposed
# property renders through `render_semantic_harness` unchanged (#64).
RESULT_IDENT = "result"

# Default cap on candidates accepted from one proposer run. Keeps a chatty model
# from flooding the store; the caller can override per request.
MAX_CANDIDATES_DEFAULT = 6

# Sentinel tokens substituted into a template. Deliberately NOT `str.format`/
# `string.Template`: C source is full of `{`, `}`, `$` and `%` that would break
# either scheme. `<<...>>` never occurs in C, so plain `str.replace` is safe.
_UNIT = "<<UNIT_ID>>"
_SYMBOL = "<<SYMBOL>>"
_SOURCE = "<<SOURCE>>"
_RESULT = "<<RESULT>>"
_MAX = "<<MAX>>"
_TYPE = "<<TYPE>>"


@dataclass(frozen=True)
class PromptTemplate:
    """A versioned proposer prompt: an id family, a version, and the body text.

    `prompt_id` names the artifact family (e.g. "semantic") and `version` the
    revision (e.g. "1"); together they are recorded on every property this
    template proposes, so provenance ties a candidate back to its exact source.
    `template` carries the sentinel tokens `render_prompt` fills in.
    """

    prompt_id: str
    version: str
    template: str

    @property
    def ref(self) -> str:
        """The registry key / log label, e.g. ``"semantic/v1"``."""
        return f"{self.prompt_id}/v{self.version}"


def render_prompt(
    template: PromptTemplate,
    *,
    unit_id: str,
    symbol: str,
    source_text: str,
    result_ident: str = RESULT_IDENT,
    max_candidates: int = MAX_CANDIDATES_DEFAULT,
    context: str | None = None,
    note: str | None = None,
    type_name: str = "",
) -> str:
    """Fill a template's sentinels for one unit; return the ready-to-send prompt.

    `type_name` fills ``<<TYPE>>`` (the struct type a representation-invariant
    prompt is about); a semantic template has no such token. `context` (a
    recipe's output) is rendered in place of `source_text` when
    present; `None` keeps the prompt byte-identical to the whole-file one. The
    rendered source/context is substituted *last*: the unit's C source may itself
    contain text that looks like a sentinel's replacement, and substituting it
    last means such text is never re-scanned for further tokens. `note` is a
    unit-specific addendum appended after the template (a struct-pointer
    parameter's reading); `None` leaves the template's text untouched, so a unit
    without one is byte-identical to before.
    """
    rendered = (
        template.template.replace(_UNIT, unit_id)
        .replace(_SYMBOL, symbol)
        .replace(_TYPE, type_name)
        .replace(_RESULT, result_ident)
        .replace(_MAX, str(max_candidates))
        .replace(_SOURCE, source_text if context is None else context)
    )
    return rendered if note is None else f"{rendered}\n{note}\n"


_SEMANTIC_V1_TEXT = """\
You propose SEMANTIC properties (behavioural postconditions) for ONE C function
that an ESBMC bounded model checker will check up to a loop bound k. You are NOT
proving anything -- you propose checkable properties and ESBMC returns a verdict.

Function: <<SYMBOL>>   (unit id: <<UNIT_ID>>)
Translation unit:
<<SOURCE>>

Contract you MUST follow for every candidate:
- `expression`: a C boolean expression asserted AFTER one call
  `<<RESULT>> = <<SYMBOL>>(...)`. Refer to the return value ONLY as `<<RESULT>>`.
  Refer to parameters by their exact declared names. Use no other variables.
- `domain`: a list of C boolean preconditions over the PARAMETERS ONLY (never
  `<<RESULT>>`), emitted as __ESBMC_assume(...) BEFORE the call to constrain the
  nondeterministic inputs so the property is not vacuously violated. Give the
  WEAKEST domain that makes the property hold. Use an empty list if none is
  needed.
- `referenced_params`: the exact parameter names your `expression` uses.
- Pure expressions only: no `;`, no assignment, no side effects, no function
  calls except standard limit macros (INT_MIN, INT64_MAX, UINT_MAX, SIZE_MAX...).
- `expression` must reference `<<RESULT>>` and/or a parameter -- never a constant
  tautology such as 1==1.

Return ONLY a JSON object (no prose, no markdown fences):
{"candidates":[
  {"expression":"<<RESULT>> >= 0",
   "domain":["x > INT64_MIN"],
   "referenced_params":["x"],
   "rationale":"abs is non-negative except at the un-negatable minimum"}
]}
Propose up to <<MAX>> distinct, non-overlapping candidates, strongest first.
"""

SEMANTIC_V1 = PromptTemplate(
    prompt_id="semantic", version="1", template=_SEMANTIC_V1_TEXT
)

_REP_INVARIANT_V1_TEXT = """\
You propose a REPRESENTATION INVARIANT for ONE C struct type. An ESBMC bounded
model checker will ASSUME it on entry to each function that takes a mutable
`<<TYPE>> *` and ASSERT it again when that function returns, up to a loop bound k.
An initialiser (init/initialise/initialize in its name) gets no entry assumption:
it is checked to establish the invariant from an arbitrary instance. You are NOT
proving anything -- you propose a checkable predicate and ESBMC returns verdicts.

Type: <<TYPE>>   (type id: <<UNIT_ID>>)
A function taking it, shown for context: <<SYMBOL>>
Translation unit:
<<SOURCE>>

Contract you MUST follow for every candidate:
- `expression`: a pure C boolean expression over the reserved identifier `self`,
  a `const <<TYPE>> *`, e.g. `self->count <= CAP && self->tail < CAP`. Use only
  fields reached through `self`, integer literals and macros/constants defined in
  the source. No other variables, no function calls, no `;`, no assignment, no
  side effects.
- It must be what the code MAINTAINS: true of every instance a correct
  initialiser produces and preserved by every function that mutates the type.
  Describe the shape the code relies on (indices below capacity, counts within
  bounds, a pointer valid whenever a count is positive), not just what the one
  function shown happens to do.
- Do NOT over-strengthen. The invariant is ASSUMED, never discharged at the
  callers that construct the type, and an over-strong one hides real bugs by
  excluding states a caller can reach. When unsure, propose the weaker predicate.
- It must reference `self` -- never a constant tautology such as 1==1.
- Do not add `domain`, `referenced_params` or `kind`: an invariant is over one
  type, not one call.

Return ONLY a JSON object (no prose, no markdown fences):
{"candidates":[
  {"expression":"self->count <= CAP && self->tail < CAP",
   "rationale":"push and pop keep count within capacity and reduce tail modulo CAP"}
]}
Propose up to <<MAX>> distinct candidates, strongest first.
"""

REP_INVARIANT_V1 = PromptTemplate(
    prompt_id="rep-invariant", version="1", template=_REP_INVARIANT_V1_TEXT
)

# Registry keyed by `PromptTemplate.ref`; GEPA (#5) registers new versions here.
PROMPTS: dict[str, PromptTemplate] = {
    SEMANTIC_V1.ref: SEMANTIC_V1,
    REP_INVARIANT_V1.ref: REP_INVARIANT_V1,
}

DEFAULT_PROMPT = SEMANTIC_V1
DEFAULT_INVARIANT_PROMPT = REP_INVARIANT_V1

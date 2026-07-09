"""Versioned proposer prompts -- the artifact GEPA (#5) will later evolve.

The proposer (#65) turns a verification unit (`path::symbol` + its C source) into
candidate `Property` objects by asking an LLM. The prompt that does the asking is
a *first-class, versioned artifact*: every proposed property records the
`prompt_id`+`prompt_version` that produced it (via `Provenance`), so a graded
property is always traceable to the exact prompt text. GEPA (#5) evolves a prompt
by registering a *new* `PromptTemplate` with a bumped `version`; a shipped
version's text is immutable, mirroring ADR immutability.

v1 proposes SEMANTIC properties only (postconditions + preconditions);
reachability harnessing is deferred (ADR-0009 D2). The model is told to name the
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
) -> str:
    """Fill a template's sentinels for one unit; return the ready-to-send prompt.

    `source_text` is substituted *last*: the unit's C source may itself contain
    text that looks like a sentinel's replacement, and substituting it last means
    such text is never re-scanned for further tokens.
    """
    return (
        template.template.replace(_UNIT, unit_id)
        .replace(_SYMBOL, symbol)
        .replace(_RESULT, result_ident)
        .replace(_MAX, str(max_candidates))
        .replace(_SOURCE, source_text)
    )


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

# Registry keyed by `PromptTemplate.ref`; GEPA (#5) registers new versions here.
PROMPTS: dict[str, PromptTemplate] = {SEMANTIC_V1.ref: SEMANTIC_V1}

DEFAULT_PROMPT = SEMANTIC_V1

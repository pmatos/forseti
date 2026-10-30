"""Forseti Core as an MCP server — the substrate every harness shares (RFC-0001).

Claude Code, Codex, and opencode all differ in their hooks but agree on MCP, so
the Core exposes its operations as MCP tools here: `verify` (ESBMC), `propose`
(the LLM property proposer, #65), `submit` (ingest a host-generated candidate,
no LLM call, #213), `check` (verify a unit's stored properties, #213), `grade`
(kill-rate scoring of held properties against mutants, #392), and
`semantic_loop` (the composed ingest -> check -> outcome operation, #213) --
one call in place of `propose`/`submit` and `check` separately. Each tool is a
thin shell over its `forseti.core` entry point and returns the same JSON
payload the CLI's `--json` does, so an adapter sees one shape either way.

This module imports the `mcp` SDK at import time (an optional dependency,
`forseti[mcp]`); the unified CLI imports it lazily so `forseti verify` works
without the SDK installed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from forseti.precond import DEFAULT_MAX_LEN
from forseti.properties import InvariantRole, parse_candidate_list

from .check import (
    DEFAULT_TIMEOUT_S as CHECK_TIMEOUT_S,
)
from .check import (
    DEFAULT_UNWIND as CHECK_DEFAULT_UNWIND,
)
from .check import check_source
from .grade import grade_source
from .invariant import check_invariant_source, submit_invariant_source
from .loop import parse_loop_mode, run_semantic_loop
from .propose import (
    DEFAULT_MAX_CANDIDATES,
    DEFAULT_MODEL,
    DEFAULT_STORE_ROOT,
    propose_invariant_source,
    propose_source,
)
from .propose import (
    DEFAULT_TIMEOUT_S as PROPOSE_TIMEOUT_S,
)
from .submit import DEFAULT_PROMPT_ID, DEFAULT_PROMPT_VERSION, submit_source
from .verify import (
    DEFAULT_TIMEOUT_S,
    DEFAULT_UNWIND,
    Payload,
    result_to_payload,
    verify_source,
)

_KINDS = ("semantic", "rep-invariant")


def _check_kind(kind: str, type_name: str | None) -> bool:
    """True for a representation invariant; `ValueError` on an inconsistent pair."""
    if kind not in _KINDS:
        raise ValueError(f"kind must be one of {', '.join(_KINDS)}, got {kind!r}")
    if kind == "rep-invariant":
        if not type_name:
            raise ValueError("kind rep-invariant requires type_name")
        return True
    if type_name is not None:
        raise ValueError("type_name applies to kind rep-invariant only")
    return False


_INSTRUCTIONS = (
    "Forseti Core: bounded verification with ESBMC plus LLM property proposal. "
    "Call `verify` on a source file after editing it; a VIOLATED verdict carries "
    "a concrete counterexample to fix, and a VERIFIED is only 'verified up to k'. "
    "Call `propose` to generate candidate properties for a unit before checking, "
    "or `submit` to ingest a candidate the host model already generated (no LLM "
    "call). Call `check` to verify a unit's stored properties: held | violated | "
    "unknown | vacuous | error, plus skipped for a deferred reachability "
    "property (`vacuous`: verified, but the post-call point is unreachable at "
    "k, so the property was never evaluated; never a pass). Or call "
    "`semantic_loop` to do ingestion and checking in one call and get back "
    "Core's own worst-outcome-wins outcome, instead of deriving it yourself. "
    "Call `grade` on a unit with held properties to score each by the mutants it "
    "kills (bounded verdicts; unknown, error and skipped mutants are reported "
    "beside the kill rate, never in it)."
)


def verify_tool(
    source: str,
    unwind: int = DEFAULT_UNWIND,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    function: str | None = None,
    esbmc_bin: str = "esbmc",
) -> Payload:
    """Verify a source file with ESBMC and return the typed verdict.

    Args:
        source: Path to the source file to verify.
        unwind: Loop-unwind bound k; a VERIFIED is only "verified up to k".
        timeout_s: Per-run timeout in seconds.
        function: Entry function to verify (defaults to ESBMC's, i.e. main).
        esbmc_bin: The esbmc binary to invoke.

    Returns:
        A JSON object with the verdict plus provenance; VIOLATED adds
        `counterexample`, UNKNOWN adds `reason`, ERROR adds `message`.
    """
    path = Path(source)
    result = verify_source(
        path,
        unwind=unwind,
        timeout_s=timeout_s,
        function=function,
        esbmc_bin=esbmc_bin,
    )
    return result_to_payload(result, path, unwind)


def propose_tool(
    source: str,
    function: str,
    persist: bool = True,
    store_root: str = str(DEFAULT_STORE_ROOT),
    model: str = DEFAULT_MODEL,
    timeout_s: float = PROPOSE_TIMEOUT_S,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
    recipe: str | None = None,
    kind: str = "semantic",
    type_name: str | None = None,
) -> dict[str, object]:
    """Propose candidate properties for a unit and (optionally) store them.

    Args:
        source: Path to the source file defining the unit.
        function: The function under test (the `symbol` of `path::symbol`).
        persist: When true, store each accepted candidate as CANDIDATE; when
            false, a dry run that proposes and validates without writing.
        store_root: The `.forseti` store directory to persist into.
        model: The LLM model the proposer calls.
        timeout_s: Per-call timeout for the proposer's LLM invocation.
        max_candidates: Cap on the number of accepted candidates.
        recipe: Input recipe selecting what the proposer is shown instead of
            the whole file: "baseline" (whole file minus `main` and the header
            comment), "sighted" (the function, its doc comment and the
            types/macros it uses) or "blind" (sighted without the body).
            Omitted: the whole file. Recorded on each stored property.
        kind: "semantic" (default) or "rep-invariant": propose representation
            invariants of the struct type `type_name` instead, over `self`, a
            `const T *`. `function` then names a function taking `T *` (the
            prompt's context). Survivors are stored as candidates; accepting one
            is a human act (`forseti invariant-status`) and it is *assumed*,
            never discharged at the callers that construct the type.
        type_name: With kind "rep-invariant", the struct type (`ring_t`,
            `struct node`).

    Returns:
        A JSON object with the unit id (the type id for an invariant),
        prompt/backend provenance, and the `accepted` and `rejected` candidate
        lists.
    """
    if _check_kind(kind, type_name):
        assert type_name is not None
        return propose_invariant_source(
            Path(source),
            function=function,
            type_name=type_name,
            persist=persist,
            store_root=Path(store_root),
            model=model,
            timeout_s=timeout_s,
            max_candidates=max_candidates,
            recipe=recipe,
        ).to_dict()
    result = propose_source(
        Path(source),
        function=function,
        persist=persist,
        store_root=Path(store_root),
        model=model,
        timeout_s=timeout_s,
        max_candidates=max_candidates,
        recipe=recipe,
    )
    return result.to_dict()


def submit_tool(
    source: str,
    function: str,
    expression: str,
    provider: str,
    model: str,
    domain: list[str] | None = None,
    referenced_params: list[str] | None = None,
    rationale: str = "",
    prompt_id: str = DEFAULT_PROMPT_ID,
    prompt_version: str = DEFAULT_PROMPT_VERSION,
    persist: bool = True,
    store_root: str = str(DEFAULT_STORE_ROOT),
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
    kind: str = "semantic",
    type_name: str | None = None,
) -> dict[str, object]:
    """Ingest one host-generated candidate property -- no LLM call.

    Args:
        source: Path to the source file defining the unit.
        function: The function under test (the `symbol` of `path::symbol`).
        expression: The candidate's C boolean expression (e.g. "result >= 0").
        provider: Who/what produced this candidate (e.g. "codex").
        model: The model that produced this candidate (e.g. "gpt-5.1").
        domain: Preconditions over the parameters, emitted as
            `__ESBMC_assume(...)` before the call.
        referenced_params: Parameter names `expression` uses.
        rationale: Free-text rationale, stored as the property's description.
        prompt_id: Provenance prompt id for this candidate.
        prompt_version: Provenance prompt version for this candidate.
        persist: When true, store the candidate as CANDIDATE if it validates;
            when false, a dry run that validates without writing.
        store_root: The `.forseti` store directory to persist into.
        max_candidates: Cap on the number of accepted candidates (this call
            submits one, so this only matters via a shared batch caller).
        kind: "semantic" (default) or "rep-invariant": store `expression` as a
            representation invariant of the struct type `type_name`, a C
            predicate over `self` (a `const T *`), e.g. `self->count <= CAP`.
            It is per type, so `function`, `domain` and `referenced_params` do
            not apply to it; it is *assumed*, never discharged at the callers
            that construct the type.
        type_name: With kind "rep-invariant", the struct type (`ring_t`,
            `struct node`).

    Returns:
        The same JSON shape `propose` returns: unit id (the type id for an
        invariant), prompt/backend provenance, and the `accepted`/`rejected`
        candidate lists.
    """
    if _check_kind(kind, type_name):
        assert type_name is not None
        if domain or referenced_params:
            raise ValueError(
                "kind rep-invariant is per type: domain and referenced_params "
                "do not apply"
            )
        return submit_invariant_source(
            Path(source),
            type_name=type_name,
            expression=expression,
            provider=provider,
            model=model,
            rationale=rationale,
            prompt_id=prompt_id,
            prompt_version=prompt_version,
            persist=persist,
            store_root=Path(store_root),
        ).to_dict()
    result = submit_source(
        Path(source),
        function=function,
        expression=expression,
        provider=provider,
        model=model,
        domain=tuple(domain or ()),
        referenced_params=tuple(referenced_params or ()),
        rationale=rationale,
        prompt_id=prompt_id,
        prompt_version=prompt_version,
        persist=persist,
        store_root=Path(store_root),
        max_candidates=max_candidates,
    )
    return result.to_dict()


def check_tool(
    source: str,
    function: str,
    store_root: str = str(DEFAULT_STORE_ROOT),
    unwind: int = CHECK_DEFAULT_UNWIND,
    unwind_ladder: list[int] | None = None,
    timeout_s: float = CHECK_TIMEOUT_S,
    esbmc_bin: str = "esbmc",
    max_len: int = DEFAULT_MAX_LEN,
    type_name: str | None = None,
    role: str | None = None,
) -> dict[str, object]:
    """Check a unit's stored, checkable properties against ESBMC.

    Args:
        source: Path to the source file defining the unit.
        function: The function under test (the `symbol` of `path::symbol`).
        store_root: The `.forseti` store directory to read properties from.
        unwind: Loop-unwind bound k for the first attempt.
        unwind_ladder: Bounds tried after `unwind` on an UNKNOWN verdict
            (default: `default_unwind_ladder_above(unwind)`).
        timeout_s: Per-attempt esbmc timeout in seconds.
        esbmc_bin: The esbmc binary to invoke.
        max_len: Cap on a `(ptr, len)` buffer length the property's domain does
            not constrain; a verdict is then scoped to `len<=max_len` (reported
            in its `length_bounds`); an explicit `unwind`/`unwind_ladder` must
            reach `max_len + 1` to settle (the default ladder is extended).
        type_name: A struct type (`ring_t`, `struct node`): check the function
            against the representation invariant(s) stored for that type instead
            of its own properties -- nondet instance, invariant assumed at entry,
            call, invariant asserted at exit. The invariant is *assumed*, never
            discharged at the callers that construct the type; each verdict says
            so in its `assumption` field.
        role: With `type_name`, `method` (assume at entry) or `initialiser`
            (do not); default inferred from the function name.

    Returns:
        A JSON object with the unit id, per-outcome counts, and one verdict
        per checked property: held | violated | unknown | vacuous | error |
        skipped. `vacuous` means ESBMC verified the harness but a probe at the
        same k could not reach the post-call point, so the postcondition was
        never evaluated (the run `outcome` reports it as `unknown`). A `held`
        shows some path reaches the post-call point, not that k covers every
        path.
    """
    # `None` passes through so the check derives the ladder above `unwind`
    # itself; a supplied list is coerced to the tuple it expects.
    ladder = tuple(unwind_ladder) if unwind_ladder is not None else None
    if type_name is None:
        if role is not None:
            raise ValueError("role applies to type_name only")
        run = check_source(
            Path(source),
            function=function,
            store_root=Path(store_root),
            unwind=unwind,
            unwind_ladder=ladder,
            timeout_s=timeout_s,
            esbmc_bin=esbmc_bin,
            max_len=max_len,
        )
    else:
        run = check_invariant_source(
            Path(source),
            function=function,
            type_name=type_name,
            role=None if role is None else InvariantRole(role),
            store_root=Path(store_root),
            unwind=unwind,
            unwind_ladder=ladder,
            timeout_s=timeout_s,
            esbmc_bin=esbmc_bin,
            max_len=max_len,
        )
    return run.to_dict()


def grade_tool(
    source: str,
    function: str,
    store_root: str = str(DEFAULT_STORE_ROOT),
    unwind: int = CHECK_DEFAULT_UNWIND,
    unwind_ladder: list[int] | None = None,
    timeout_s: float = CHECK_TIMEOUT_S,
    esbmc_bin: str = "esbmc",
    max_len: int = DEFAULT_MAX_LEN,
    max_mutants: int | None = None,
    seed: int = 0,
) -> dict[str, object]:
    """Grade a unit's held, candidate properties against mutants of the function.

    Args:
        source: Path to the source file defining the unit.
        function: The function under test (the `symbol` of `path::symbol`).
        store_root: The `.forseti` store directory; each graded property is
            stored there with its kill rate and reason, status `graded`.
        unwind: Loop-unwind bound k for the first attempt.
        unwind_ladder: Bounds tried after `unwind` on an UNKNOWN verdict
            (default: `default_unwind_ladder_above(unwind)`).
        timeout_s: Per-attempt esbmc timeout in seconds.
        esbmc_bin: The esbmc binary to invoke.
        max_len: Cap on a `(ptr, len)` buffer length the property's domain does
            not constrain (see `check`); also bounds the equivalence check.
        max_mutants: Grade against at most this many valid mutants (a seeded
            sample; default: all).
        seed: Seed of the `max_mutants` sample.

    Returns:
        A JSON object: per property, the killed / survived / unknown / error /
        skipped counts, `kill_rate` (killed / (killed + survived); unknown, error
        and skipped are never folded in), a `reason` naming each surviving
        mutant, and the ESBMC time; plus the run `outcome` (graded | partial |
        ungraded), mutant and equivalence totals, and the `limitations`: every
        verdict is bounded (up to k, len<=max_len), and skipping equivalent
        mutants by a bounded check inflates the kill rate. A property with no
        killed or surviving mutant stays a candidate.
    """
    run = grade_source(
        Path(source),
        function=function,
        store_root=Path(store_root),
        unwind=unwind,
        unwind_ladder=tuple(unwind_ladder) if unwind_ladder is not None else None,
        timeout_s=timeout_s,
        esbmc_bin=esbmc_bin,
        max_len=max_len,
        max_mutants=max_mutants,
        seed=seed,
    )
    return run.to_dict()


def semantic_loop_tool(
    source: str,
    function: str,
    mode: str,
    store_root: str = str(DEFAULT_STORE_ROOT),
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
    candidates: list[dict[str, Any]] | None = None,
    provider: str | None = None,
    model: str | None = None,
    prompt_id: str = DEFAULT_PROMPT_ID,
    prompt_version: str = DEFAULT_PROMPT_VERSION,
    propose_model: str = DEFAULT_MODEL,
    claude_bin: str = "claude",
    propose_timeout_s: float = PROPOSE_TIMEOUT_S,
    unwind: int = CHECK_DEFAULT_UNWIND,
    unwind_ladder: list[int] | None = None,
    timeout_s: float = CHECK_TIMEOUT_S,
    esbmc_bin: str = "esbmc",
    max_len: int = DEFAULT_MAX_LEN,
    recipe: str | None = None,
) -> dict[str, object]:
    """The composed semantic-property loop (#213): ingest, then check -- one call.

    Args:
        source: Path to the source file defining the unit.
        function: The function under test (the `symbol` of `path::symbol`).
        mode: One of `"propose"` (ask the LLM proposer), `"submit"` (ingest
            `candidates`, no LLM call), or `"check_only"` / `"check-only"`
            (skip ingestion and check whatever the store already holds).
        store_root: The `.forseti` store directory.
        max_candidates: Cap on accepted candidates.
        candidates: `mode="submit"` only -- a list of candidate objects, each
            `{"expression": ..., "domain": [...], "referenced_params": [...],
            "rationale": ...}`; validated and persisted independently, so a
            rejected candidate stays visible in the `ingestion` list rather
            than vanishing into a merged batch result.
        provider: `mode="submit"` only -- who/what produced these candidates.
        model: `mode="submit"` only -- the model that produced these candidates.
        prompt_id: `mode="submit"` only -- provenance prompt id.
        prompt_version: `mode="submit"` only -- provenance prompt version.
        propose_model: `mode="propose"` only -- LLM model for the proposer.
        claude_bin: `mode="propose"` only -- claude binary to invoke.
        propose_timeout_s: `mode="propose"` only -- per-call timeout for the
            proposer's LLM invocation.
        unwind: Loop-unwind bound k for the first check attempt.
        unwind_ladder: Bounds tried after `unwind` on an UNKNOWN verdict
            (default: `default_unwind_ladder_above(unwind)`).
        timeout_s: Per-attempt esbmc timeout in seconds.
        esbmc_bin: The esbmc binary to invoke.
        max_len: Cap on a `(ptr, len)` buffer length the property's domain does
            not constrain; a verdict is then scoped to `len<=max_len` (reported
            in its `length_bounds`); an explicit `unwind`/`unwind_ladder` must
            reach `max_len + 1` to settle (the default ladder is extended).
        recipe: `mode="propose"` only -- input recipe selecting what the
            proposer is shown instead of the whole file: "baseline" (whole
            file minus `main` and the header comment), "sighted" (the
            function, its doc comment and the types/macros it uses) or "blind"
            (sighted without the body). Omitted: the whole file. Recorded on
            each stored property.

    Returns:
        A JSON object with the unit id, `mode`, the per-candidate ingestion
        results (empty for `check_only`), the check run, and Core's own
        worst-outcome-wins `outcome`: held | violated | unknown | error |
        empty -- the one field to key a gate/report decision on.
    """
    loop_mode = parse_loop_mode(mode)
    specs = parse_candidate_list(candidates or ())
    result = run_semantic_loop(
        Path(source),
        function=function,
        mode=loop_mode,
        store_root=Path(store_root),
        max_candidates=max_candidates,
        candidates=specs,
        provider=provider,
        model=model,
        prompt_id=prompt_id,
        prompt_version=prompt_version,
        propose_model=propose_model,
        claude_bin=claude_bin,
        propose_timeout_s=propose_timeout_s,
        unwind=unwind,
        unwind_ladder=tuple(unwind_ladder) if unwind_ladder is not None else None,
        check_timeout_s=timeout_s,
        esbmc_bin=esbmc_bin,
        max_len=max_len,
        recipe=recipe,
    )
    return result.to_dict()


def build_server(name: str = "forseti") -> MCPServer:
    """An `MCPServer` exposing Forseti Core's tools (`verify`, `propose`,
    `submit`, `check`, `grade`, `semantic_loop`)."""
    server: MCPServer = MCPServer(name, instructions=_INSTRUCTIONS)
    server.add_tool(
        verify_tool,
        name="verify",
        description="Verify a source file with ESBMC; returns a typed verdict.",
    )
    server.add_tool(
        propose_tool,
        name="propose",
        description=(
            "Propose candidate properties for a unit with the LLM proposer; "
            "returns the accepted and rejected candidates."
        ),
    )
    server.add_tool(
        submit_tool,
        name="submit",
        description=(
            "Ingest one host-generated candidate property -- no LLM call; "
            "validated the same way `propose`'s candidates are."
        ),
    )
    server.add_tool(
        check_tool,
        name="check",
        description=(
            "Check a unit's stored properties with ESBMC; returns held | "
            "violated | unknown | vacuous | error (plus skipped) per property; "
            "vacuous = verified but the post-call point is unreachable at k "
            "(not a pass). With type_name, checks the function against that "
            "struct type's stored representation invariant (assumed, never "
            "discharged at callers)."
        ),
    )
    server.add_tool(
        grade_tool,
        name="grade",
        description=(
            "Grade a unit's held candidate properties by mutation: kill rate "
            "(killed / (killed + survived)) and a reason naming each surviving "
            "mutant, stored with status graded. Unknown, error and skipped "
            "(equivalent) mutants are counted beside the rate, never in it. "
            "Bounded verdicts only; skipping equivalent mutants inflates the rate."
        ),
    )
    server.add_tool(
        semantic_loop_tool,
        name="semantic_loop",
        description=(
            "The composed semantic-property loop (#213): ingest candidates per "
            "mode (propose | submit | check_only), then check with ESBMC -- one "
            "call instead of propose/submit and check separately. Returns the "
            "ingestion results, the check run, and Core's own worst-outcome-wins "
            "outcome: held | violated | unknown | error | empty."
        ),
    )
    return server


def serve() -> None:
    """Run the Core MCP server on stdio (the transport all three harnesses use)."""
    build_server().run()

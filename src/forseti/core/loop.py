"""Forseti Core's composed semantic-property loop (#213).

Before this module, an adapter that wanted the full `edited unit -> ingest
candidates -> persist -> render harness -> check -> verdict policy` flow had
to call three separate Core faces itself (`propose_source`/`submit_source`,
then `check_source`) and re-derive a pass/block decision from the raw
`PropertyCheckRun` — exactly the "still drives propose/check itself" gap
issue #213 flags for the Claude Code adapter, and the reason Codex's adapter
had no semantic-property wiring at all before this issue. `run_semantic_loop`
is the single entry point that composes all of it, so an adapter (or its
prompt) makes one call and reads one `outcome`.

Candidate ingestion is explicit, not inferred from the caller's harness, per
three modes:

- `"propose"` — ask the configured LLM proposer (`propose_source`); the
  Claude Code subagent's own use case (a nested `claude -p` call it already
  pays for deliberately, issue #95).
- `"submit"` — ingest `candidates`, host-generated properties with no LLM
  call (`submit_source`, looped once per candidate so a rejected one is
  visible in `SemanticLoopResult.ingestion`, never swallowed into a single
  batch result); the Codex host-model's use case (submitted via MCP or CLI,
  #213's original provider-neutral motivation).
- `"check_only"` — skip ingestion and check whatever the store already
  holds; a re-check after a fix turn, or a Stop-gate surface that only ever
  reads already-persisted candidates.

This module composes `propose_source`/`submit_source`/`check_source`
verbatim — it never re-implements unit-id formatting, persistence, or harness
rendering, and it never re-derives verdict policy: `SemanticLoopResult.outcome`
is `PropertyCheckRun.outcome` (`forseti.orchestrator.check`), Core's own
worst-outcome-wins field. An adapter reads that one value for its
capability-specific gating action (block, suspend, report); it does not
belong here — see `docs/design/0001-harness-portability.md`'s capability
matrix for why that stays adapter-owned.

Not to be confused with `forseti.orchestrator.loop.run_loop`, the *safety*
write -> verify -> fix loop (whole-unit ESBMC, no properties) `forseti verify
--fix` drives; this is the *semantic*-property loop, a sibling at the Core
face layer, not a variant of that driver.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, assert_never

from forseti.core.check import (
    DEFAULT_TIMEOUT_S as CHECK_DEFAULT_TIMEOUT_S,
)
from forseti.core.check import (
    DEFAULT_UNWIND as CHECK_DEFAULT_UNWIND,
)
from forseti.core.check import (
    check_source,
)
from forseti.core.propose import (
    DEFAULT_MAX_CANDIDATES,
    DEFAULT_STORE_ROOT,
    propose_source,
)
from forseti.core.propose import (
    DEFAULT_MODEL as PROPOSE_DEFAULT_MODEL,
)
from forseti.core.propose import (
    DEFAULT_TIMEOUT_S as PROPOSE_DEFAULT_TIMEOUT_S,
)
from forseti.core.submit import (
    DEFAULT_PROMPT_ID,
    DEFAULT_PROMPT_VERSION,
    submit_source,
)
from forseti.orchestrator import PropertyCheckRun, RunOutcome, VerifyPort
from forseti.properties import CandidateSpec, LLMClient, ProposalResult

LoopMode = Literal["propose", "submit", "check_only"]


@dataclass(frozen=True)
class SemanticLoopResult:
    """One composed run: candidate ingestion (if any), then the check that follows.

    `ingestion` is empty in `"check_only"` mode (nothing was proposed or
    submitted this call); it has one entry in `"propose"` mode (one LLM call
    covers every candidate it returns) and one entry per candidate in
    `"submit"` mode (each validated independently, so a rejected candidate
    stays visible instead of vanishing into a merged batch result).
    `outcome` is `check.outcome` verbatim (`PropertyCheckRun`, #213) — the one
    field a caller reads instead of re-deriving verdict severity itself.
    """

    unit_id: str
    mode: LoopMode
    check: PropertyCheckRun
    ingestion: tuple[ProposalResult, ...] = ()

    @property
    def outcome(self) -> RunOutcome:
        return self.check.outcome

    def to_dict(self) -> dict[str, Any]:
        return {
            "unit_id": self.unit_id,
            "mode": self.mode,
            "ingestion": [result.to_dict() for result in self.ingestion],
            "check": self.check.to_dict(),
            "outcome": self.outcome,
        }


def run_semantic_loop(
    source: Path,
    *,
    function: str,
    mode: LoopMode,
    store_root: Path = DEFAULT_STORE_ROOT,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
    # "submit" mode
    candidates: Sequence[CandidateSpec] = (),
    provider: str | None = None,
    model: str | None = None,
    prompt_id: str = DEFAULT_PROMPT_ID,
    prompt_version: str = DEFAULT_PROMPT_VERSION,
    # "propose" mode
    propose_model: str = PROPOSE_DEFAULT_MODEL,
    claude_bin: str = "claude",
    propose_timeout_s: float = PROPOSE_DEFAULT_TIMEOUT_S,
    client: LLMClient | None = None,
    # check phase
    unwind: int = CHECK_DEFAULT_UNWIND,
    unwind_ladder: tuple[int, ...] | None = None,
    check_timeout_s: float | None = CHECK_DEFAULT_TIMEOUT_S,
    extra_flags: Sequence[str] = (),
    esbmc_bin: str = "esbmc",
    verify_port: VerifyPort | None = None,
) -> SemanticLoopResult:
    """Ingest (per `mode`), then check `source`::`function`'s stored properties.

    `mode="submit"` requires at least one `candidates` entry and a nonblank
    `provider`/`model` (the same `BlankProvenanceError` guard `submit_source`
    itself enforces); `mode="propose"` and `mode="check_only"` take no
    `candidates` — passing any is a caller error (`ValueError`), not silently
    ignored. `unwind_ladder` defaults to `None` and forwards through to
    `check_source`, which derives the rungs above `unwind` itself — so a
    caller-chosen `unwind` doesn't collide with the fixed default rungs, the
    same as the `check` CLI/MCP faces.

    Every persisted candidate and every checked property still emits Core's
    own canonical events (`property.proposed`, `property.check.start`,
    `property.verdict` — `core/events.py`) through `propose_source`/
    `submit_source`/`check_source` unchanged; this function adds no event of
    its own; a `gate.decision` from `SemanticLoopResult.outcome` stays the
    adapter's job (capability-specific gating action, not Core policy).
    """
    if mode == "submit":
        if not candidates:
            raise ValueError("mode='submit' requires at least one candidate")
        if not provider or not model:
            raise ValueError("mode='submit' requires a nonblank provider and model")
    elif candidates:
        raise ValueError(f"mode={mode!r} does not take candidates")

    ingestion: tuple[ProposalResult, ...]
    match mode:
        case "propose":
            ingestion = (
                propose_source(
                    source,
                    function=function,
                    persist=True,
                    store_root=store_root,
                    model=propose_model,
                    claude_bin=claude_bin,
                    timeout_s=propose_timeout_s,
                    max_candidates=max_candidates,
                    client=client,
                ),
            )
        case "submit":
            assert provider is not None
            assert model is not None
            # `submit_source` validates one candidate per call, so `max_candidates`
            # must be tracked across calls here -- passing the same bound to every
            # call would let each call's own 1-item batch pass `_accept_reject`'s
            # `len(accepted) >= max_candidates` check and defeat the cap entirely.
            submitted = []
            accepted_count = 0
            for candidate in candidates:
                result = submit_source(
                    source,
                    function=function,
                    expression=candidate.expression,
                    provider=provider,
                    model=model,
                    domain=candidate.domain,
                    referenced_params=candidate.referenced_params,
                    rationale=candidate.rationale,
                    prompt_id=prompt_id,
                    prompt_version=prompt_version,
                    persist=True,
                    store_root=store_root,
                    max_candidates=max(max_candidates - accepted_count, 0),
                )
                accepted_count += len(result.accepted)
                submitted.append(result)
            ingestion = tuple(submitted)
        case "check_only":
            ingestion = ()
        case _:
            assert_never(mode)

    check = check_source(
        source,
        function=function,
        store_root=store_root,
        unwind=unwind,
        # `None` forwards straight through: check_source derives the ladder
        # above `unwind`, the same as the check CLI/MCP faces.
        unwind_ladder=unwind_ladder,
        timeout_s=check_timeout_s,
        extra_flags=extra_flags,
        esbmc_bin=esbmc_bin,
        verify_port=verify_port,
    )
    return SemanticLoopResult(
        unit_id=check.unit_id, mode=mode, ingestion=ingestion, check=check
    )

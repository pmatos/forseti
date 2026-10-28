"""Forseti Core's `check` operation — the harness-neutral property-check face.

`check_source` is to the `check_properties` driver (#66) what `propose_source`
is to the proposer: a thin Core wrapper the unified CLI (and, later, an MCP
tool) shares, so an adapter sees one check shape regardless of transport. It
builds the `Unit` from `source`/`function`, opens the `.forseti`
`PropertyStore` (the same store `forseti propose` writes candidates into),
binds a `VerifyPort` over `forseti.esbmc.verify` with the given ESBMC
invocation knobs, and runs `check_properties` with the real
`SemanticHarnessWriter`. The returned `PropertyCheckRun.to_dict()` *is* the
wire shape (mirrors `core/propose.py`'s `ProposalResult.to_dict()` reuse) — no
separate serializer lives here.

Harnesses are written under `store_root` (default `.forseti/check-work/<a
per-invocation uuid4 subdirectory>/`, already git-ignored — CLAUDE.md), never
beside `source`: `check_properties` writes one `.c` file per property, and a
plain `.c` sitting beside a tracked source is exactly what the Claude Code
adapter's own out-of-band discovery (`git status`-driven) would pick up as a
new, unverified unit — verifying the gate's own generated harness as if it
were source. The per-invocation subdirectory (not the shared `check-work`
root) is what keeps two overlapping `check_source` calls against the same
unit — a subagent's own `forseti check` racing the Stop-gate's — from
overwriting each other's harness mid-run: `check_properties` derives a
deterministic filename from the unit/property IDs, so sharing a directory
would let one invocation clobber or half-read the other's file (issue #95
review). A rendered harness inlines `unit_source` verbatim
(`render_semantic_harness`), so a quoted ``#include "local.h"`` in the unit
misses the harness's own directory and needs `-I<source's dir>` to resolve —
the same `-I` the S3 discharge path already adds for its own generated copy
(`precond/discharge.py`), not a mirrored/staged copy of the source tree.

Known residual: nothing removes a call's uuid4 subdirectory afterward, so
`check-work/` accumulates one small directory per `check_source` invocation
over a long session (the Claude Code Stop-gate calls this once per turn per
checked unit). Deliberately not addressed here: an eager cleanup would have
to avoid deleting a sibling invocation's still-in-flight directory, which is
exactly the race this per-invocation isolation exists to rule out — bounding
or sweeping `check-work/` is a separate, follow-up concern, not a correctness
issue for `check_source` itself (the directory is already git-ignored).

Emits Core's canonical `property.check.start` event before the run and one
`property.verdict` per checked property after (`core/events.py`, #213), so a
trace built from `.forseti/events.jsonl` reads the same regardless of which
harness triggered the check.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from pathlib import Path

from forseti.core.events import PROPERTY_CHECK_START, PROPERTY_VERDICT, record_event
from forseti.core.persistence import open_store
from forseti.core.propose import DEFAULT_STORE_ROOT
from forseti.orchestrator import (
    PropertyCheckRun,
    Unit,
    VerifyPort,
    check_properties,
)

# Preserve the documented Core helper imports while sharing their implementation.
from forseti.orchestrator.semantic_check import (
    DEFAULT_TIMEOUT_S as DEFAULT_TIMEOUT_S,
)
from forseti.orchestrator.semantic_check import (
    DEFAULT_UNWIND as DEFAULT_UNWIND,
)
from forseti.orchestrator.semantic_check import (
    DEFAULT_UNWIND_LADDER as DEFAULT_UNWIND_LADDER,
)
from forseti.orchestrator.semantic_check import (
    SemanticCheckPlan,
)
from forseti.orchestrator.semantic_check import (
    default_unwind_ladder_above as default_unwind_ladder_above,
)
from forseti.orchestrator.semantic_check import (
    validated_max_len as validated_max_len,
)
from forseti.precond import DEFAULT_MAX_LEN

_WORK_SUBDIR = "check-work"


def check_source(
    source: Path,
    *,
    function: str,
    store_root: Path = DEFAULT_STORE_ROOT,
    unwind: int = DEFAULT_UNWIND,
    unwind_ladder: tuple[int, ...] | None = None,
    timeout_s: float | None = DEFAULT_TIMEOUT_S,
    extra_flags: Sequence[str] = (),
    esbmc_bin: str = "esbmc",
    verify_port: VerifyPort | None = None,
    max_len: int | None = DEFAULT_MAX_LEN,
) -> PropertyCheckRun:
    """Check `source`::`function`'s stored, checkable properties with ESBMC.

    Opens `store_root`'s `PropertyStore`, reads the unit's non-terminal
    properties (`properties.CHECKABLE_STATUSES` — a candidate proposed but not
    yet accepted/rejected), renders each semantic one to a
    self-contained harness via the real `SemanticHarnessWriter`, and verifies
    it along `(unwind, *unwind_ladder)` — escalating on `UNKNOWN`, never
    silently settling below the ladder's own terminal verdict. A reachability
    property is `SKIPPED` (deferred, ADR-0009 D2), never verified.

    `unwind_ladder` defaults to `None`, meaning "derive
    `default_unwind_ladder_above(unwind)`" — the rungs of `DEFAULT_UNWIND_LADDER`
    strictly above `unwind`, so a caller-raised `unwind` (e.g. `-k 8`) never
    collides with a fixed default rung to build a non-increasing ladder and raise
    `ValueError` (issue #95 review). An explicit `unwind_ladder` — including `()`
    ("verify once, no escalation") — passes through untouched, so a genuinely
    bad *explicit* ladder still surfaces its clean `ValueError`. This is why the
    three faces that expose their own `unwind` (the CLI, the MCP tool, and the
    semantic loop) can forward their unset ladder straight through instead of
    each re-deriving it.

    Verifies with unwinding assertions ON (`no_unwinding_assertions=False`,
    wrapped in `precond.run.escalating_port` — the same combination S2's
    memory-precondition sidecar already uses), unlike `forseti.esbmc.verify`'s
    own default: with assertions off, an under-unwound loop is silently
    *assumed* to have exited, so a postcondition past the loop can settle
    `Verified` — reported HELD — without the ladder ever seeing the `Unknown`
    that would have escalated it (issue #95 review). `escalating_port` turns
    that specific failure mode (an "unwinding assertion" violation, distinct
    from a real postcondition counterexample) into `Unknown(UNDER_UNWOUND)`
    instead, so the ladder climbs `unwind_ladder` on it like any other
    inconclusive verdict — a real `Violated` still surfaces as one.

    Passing `verify_port` bypasses `escalating_port` and this default outright.

    `max_len` (default `DEFAULT_MAX_LEN`, the same 8 `forseti synth --max-len`
    uses) caps every `(ptr, len)` buffer length a property's domain leaves
    unconstrained at ``len <= max_len`` before the allocation (#299). Without
    it that length is a symbolic, effectively unbounded loop trip count no
    finite `unwind` can settle, so such a property could only ever report
    `unknown`. The cap is *reported*, never silent: each verdict carries the
    `length_bounds` it was checked under, so `held` reads as "held up to k,
    len<=N". A domain clause that mentions the length is authoritative and
    suppresses the cap for that length (`properties.plan_length_bounds`).
    `None` restores the genuinely unconstrained length. The fill loop over `len`
    elements needs ``k > max_len``, so a derived default ladder gains a
    ``max_len + 1`` rung when its own top rung does not clear `max_len`
    (`default_unwind_ladder_above`); an *explicit* `unwind`/`unwind_ladder` is
    taken as given, so one that never exceeds `max_len` reports `unknown`. A
    negative `max_len` raises `ValueError` before any event.

    Harnesses are written under a fresh, per-call subdirectory of
    `store_root/"check-work"` (module docstring: never beside `source`, never
    shared with a concurrent invocation); `-I<source's resolved parent>` is
    appended *after*
    any caller-supplied `extra_flags` so a quoted include in the inlined unit
    source still resolves, without letting a same-named header sitting next to
    `source` shadow a project header for an angle-bracket include that
    `extra_flags`'s own `-I` entries would otherwise resolve first (`-I`
    affects both quote- and angle-bracket lookup in the same order it's
    given — esbmc has no quote-only `-iquote` to separate the two). A raw
    `sqlite3.Error` opening or reading the store
    (e.g. a corrupt `forseti.db`) is translated to `PropertyStoreError`, the
    same domain-level failure `propose_source` raises for the same case.

    `verify_port`, when given, replaces the real ESBMC-bound `VerifyPort`
    outright (`timeout_s`/`extra_flags`/`esbmc_bin` are then unused) — the same
    hermetic-override shape `propose_source`'s `client` param gives the
    proposer, so a test (or a budget-constrained caller like the Claude Code
    adapter's gate) can inject a fake or a differently-bound real one without
    an esbmc binary on PATH.
    """
    unit = Unit.from_path(source, function)
    plan = SemanticCheckPlan(
        unwind=unwind,
        unwind_ladder=unwind_ladder,
        max_len=max_len,
        timeout_s=timeout_s,
        esbmc_bin=esbmc_bin,
        extra_flags=extra_flags,
    )
    writer = plan.writer
    verify_fn = verify_port or plan.verify_for(source)
    with open_store(store_root) as store:
        record_event(store_root, PROPERTY_CHECK_START, unit_id=unit.unit_id)
        run = check_properties(
            unit,
            store=store,
            render=writer,
            verify=verify_fn,
            # A per-invocation subdirectory, not the shared `check-work`
            # root: `check_properties` derives a *deterministic* filename
            # from the unit/property IDs, so two overlapping invocations
            # against the same unit (a subagent's own `forseti check`
            # racing the Stop-gate's) would otherwise write, read, and
            # overwrite the same path -- one process could verify a
            # harness the other just clobbered mid-write, or read a
            # half-written file (issue #95 review).
            work_dir=store_root / _WORK_SUBDIR / uuid.uuid4().hex,
            unwind=unwind,
            unwind_ladder=plan.ladder,
        )
    for verdict in run.verdicts:
        record_event(
            store_root,
            PROPERTY_VERDICT,
            unit_id=verdict.unit_id,
            property_id=verdict.property_id,
            outcome=verdict.outcome.value,
            k=verdict.k,
        )
    return run

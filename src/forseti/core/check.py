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

Harnesses are written under `store_root` (default `.forseti/check-work/`,
already git-ignored — CLAUDE.md), never beside `source`: `check_properties`
writes one `.c` file per property, and a plain `.c` sitting beside a tracked
source is exactly what the Claude Code adapter's own out-of-band discovery
(`git status`-driven) would pick up as a new, unverified unit — verifying the
gate's own generated harness as if it were source. A rendered harness inlines
`unit_source` verbatim (`render_semantic_harness`), so a quoted
``#include "local.h"`` in the unit misses the harness's own directory and
needs `-I<source's dir>` to resolve — the same `-I` the S3 discharge path
already adds for its own generated copy (`precond/discharge.py`), not a
mirrored/staged copy of the source tree.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from functools import partial
from pathlib import Path

from forseti.core.propose import DEFAULT_STORE_ROOT
from forseti.esbmc import verify
from forseti.orchestrator import (
    PropertyCheckRun,
    SemanticHarnessWriter,
    Unit,
    VerifyPort,
    check_properties,
)
from forseti.precond.verify import escalating_port
from forseti.properties import PropertyStore, PropertyStoreError

# The CLI's own default: a human or a subagent invoking `forseti check`
# directly owns its own time budget, so it can afford a short ladder — a
# semantic postcondition over a loop (e.g. "the output is sorted") routinely
# needs a higher k than the safety gate's fixed DEFAULT_K=1 to stop reporting a
# spurious UNKNOWN below the loop's trip count (roadmap Risk 1). This is NOT
# the budget the Claude Code adapter's gate uses for its own (much tighter,
# per-hook-timeout-bounded) call — see `adapters/claude_code/property_gate.py`
# for that one; the two are allowed to differ and each documents its own choice.
DEFAULT_UNWIND = 4
DEFAULT_UNWIND_LADDER: tuple[int, ...] = (8, 16)
DEFAULT_TIMEOUT_S = 110.0
_WORK_SUBDIR = "check-work"


def check_source(
    source: Path,
    *,
    function: str,
    store_root: Path = DEFAULT_STORE_ROOT,
    unwind: int = DEFAULT_UNWIND,
    unwind_ladder: tuple[int, ...] = DEFAULT_UNWIND_LADDER,
    timeout_s: float | None = DEFAULT_TIMEOUT_S,
    extra_flags: Sequence[str] = (),
    esbmc_bin: str = "esbmc",
    verify_port: VerifyPort | None = None,
) -> PropertyCheckRun:
    """Check `source`::`function`'s stored, checkable properties with ESBMC.

    Opens `store_root`'s `PropertyStore`, reads the unit's non-terminal
    properties (`check_properties`'s own `_CHECKABLE_STATUSES` — a candidate
    proposed but not yet accepted/rejected), renders each semantic one to a
    self-contained harness via the real `SemanticHarnessWriter`, and verifies
    it along `(unwind, *unwind_ladder)` — escalating on `UNKNOWN`, never
    silently settling below the ladder's own terminal verdict. A reachability
    property is `SKIPPED` (deferred, ADR-0009 D2), never verified.

    Verifies with unwinding assertions ON (`no_unwinding_assertions=False`,
    wrapped in `precond.verify.escalating_port` — the same combination S2's
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

    Harnesses are written under `store_root/"check-work"` (module docstring:
    never beside `source`); `-I<source's resolved parent>` is appended *after*
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
    verify_fn = verify_port or escalating_port(
        partial(
            verify,
            timeout_s=timeout_s,
            esbmc_bin=esbmc_bin,
            extra_flags=(*extra_flags, f"-I{source.resolve().parent}"),
            no_unwinding_assertions=False,
        )
    )
    try:
        with PropertyStore.open(store_root) as store:
            return check_properties(
                unit,
                store=store,
                render=SemanticHarnessWriter(),
                verify=verify_fn,
                work_dir=store_root / _WORK_SUBDIR,
                unwind=unwind,
                unwind_ladder=unwind_ladder,
            )
    except sqlite3.Error as exc:
        raise PropertyStoreError(
            f"property store error at {store_root}: {exc}"
        ) from exc

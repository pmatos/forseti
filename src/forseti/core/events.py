"""Forseti Core's canonical lifecycle events -- one trace format, every harness (#213).

Each harness adapter has its own local trigger/gate trace (e.g. Claude Code's
`adapters.claude_code.event_log`, keyed by `edit`/`verify`/`gate`/`stop`/`session`).
Those stay -- they are trigger metadata specific to that harness's hook shape --
but the *Core* operations that do the actual semantic-loop work (propose, submit,
check) must emit the same handful of event types regardless of which harness
called them, so a trace is comparable across Claude Code and Codex rather than
becoming three incompatible formats.

`record_event` appends one JSON line to `<store_root>/events.jsonl` -- the same
`.forseti/events.jsonl` the Claude Code adapter's own trace already writes to
when `store_root` is the project's default `.forseti` -- so a project's whole
loop history (adapter-local trigger events interleaved with Core's own) lives in
one append-only file. Best-effort and never raises: a trace failure must not
turn a verdict into an error (mirrors `adapters.claude_code.event_log.log_event`).
The concrete writer and destination helper are re-exported from the shared
`forseti._events` module; this module owns the canonical event vocabulary.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from forseti._events import events_path as events_path
from forseti._events import record_event as record_event

if TYPE_CHECKING:
    from forseti.properties import ProposalResult

# Canonical event-type vocabulary (the `type` field). Dotted names distinguish
# these from an adapter's own short local names (`edit`, `gate`, `stop`, ...) at
# a glance in a shared events.jsonl.
PROPERTY_PROPOSED = "property.proposed"
PROPERTY_CHECK_START = "property.check.start"
PROPERTY_VERDICT = "property.verdict"
GATE_DECISION = "gate.decision"
# One per invocation of `synth`/`discharge`/`semantic-loop`, on every exit path.
# Emitted by the CLI handler, not the engine: `precond` takes no store_root and
# `run_semantic_loop` adds no event of its own (its per-property events are
# unchanged), so library/MCP callers stay silent. This states the policy;
# `core._cli_trace.traced` is the one place that carries it out.
CLI_COMMAND = "cli.command"


def record_property_proposed(
    store_root: Path, result: ProposalResult, *, channel: str
) -> None:
    """One canonical `property.proposed` event per accepted candidate (#213).

    Shared by `core.propose.propose_source` (`channel="llm"`) and
    `core.submit.submit_source` (`channel="submitted"`) -- the two differ only
    in how the candidate was produced, not in what gets traced.
    """
    for prop in result.accepted:
        record_event(
            store_root,
            PROPERTY_PROPOSED,
            unit_id=prop.unit_id,
            property_id=prop.property_id,
            expression=prop.expression,
            provider=result.provider,
            model=result.model,
            channel=channel,
        )

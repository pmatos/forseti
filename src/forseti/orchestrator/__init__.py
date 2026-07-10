"""Forseti orchestrator: the write -> verify -> fix loop core.

A pure, deterministic skeleton that consumes `forseti.esbmc`'s typed
`EsbmcResult` and drives the loop through injected `verify`/`fix` ports. No
LLM, no network, no file I/O in the driver itself — the ports own all effects.
"""

from .check import (
    PropertyCheckRun,
    PropertyOutcome,
    PropertyVerdict,
    SemanticHarnessWriter,
    check_properties,
)
from .fix import FixProvider, FixRequest, ProviderFixPort, RecordedFixProvider
from .ladder import LadderAttempt, validated_ladder, verify_ladder
from .loop import DEFAULT_MAX_ITERATIONS, Iteration, LoopRun, run_loop
from .persistence import persist_property_check, persist_run
from .ports import (
    FixPort,
    HarnessWriterPort,
    PropertyStorePort,
    RenderedHarness,
    Unit,
    VerifyPort,
)
from .report import IterationReport, Report, report_for
from .state import GiveUpReason, LoopState, next_state
from .telemetry import Event, EventSink, JsonlSink, ListSink, NullSink
from .transcript import property_check_transcript, transcript_for

__all__ = [
    "DEFAULT_MAX_ITERATIONS",
    "Event",
    "EventSink",
    "FixPort",
    "FixProvider",
    "FixRequest",
    "GiveUpReason",
    "HarnessWriterPort",
    "Iteration",
    "IterationReport",
    "JsonlSink",
    "LadderAttempt",
    "ListSink",
    "LoopRun",
    "LoopState",
    "NullSink",
    "PropertyCheckRun",
    "PropertyOutcome",
    "PropertyStorePort",
    "PropertyVerdict",
    "ProviderFixPort",
    "RecordedFixProvider",
    "RenderedHarness",
    "Report",
    "SemanticHarnessWriter",
    "Unit",
    "VerifyPort",
    "check_properties",
    "next_state",
    "persist_property_check",
    "persist_run",
    "property_check_transcript",
    "report_for",
    "run_loop",
    "transcript_for",
    "validated_ladder",
    "verify_ladder",
]

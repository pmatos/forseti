"""Forseti orchestrator: the write -> verify -> fix loop core.

A pure, deterministic skeleton that consumes `forseti.esbmc`'s typed
`EsbmcResult` and drives the loop through injected `verify`/`fix` ports. No
LLM, no network, no file I/O in the driver itself — the ports own all effects.
"""

from .fix import FixProvider, FixRequest, ProviderFixPort, RecordedFixProvider
from .loop import DEFAULT_MAX_ITERATIONS, Iteration, LoopRun, run_loop
from .ports import FixPort, VerifyPort
from .report import IterationReport, Report, report_for
from .state import GiveUpReason, LoopState, next_state

__all__ = [
    "LoopState",
    "GiveUpReason",
    "next_state",
    "VerifyPort",
    "FixPort",
    "Iteration",
    "LoopRun",
    "run_loop",
    "DEFAULT_MAX_ITERATIONS",
    "Report",
    "IterationReport",
    "report_for",
    "FixRequest",
    "FixProvider",
    "ProviderFixPort",
    "RecordedFixProvider",
]

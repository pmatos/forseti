"""The loop state machine: the states and the pure verdict->state transition.

`next_state` is the orchestrator's analogue of `forseti.esbmc.classify`: a
pure, total mapper from one ESBMC verdict to the next loop state. The driver
(`run_loop`) owns control flow; this module owns only the transition rule.

State graph::

    WRITE -> VERIFY -> {DONE | FIX | UNKNOWN | GIVE_UP}
    FIX -> VERIFY

WRITE/VERIFY/FIX are driver phases; DONE/UNKNOWN/GIVE_UP are terminal in this
skeleton. UNKNOWN is a distinct, honest halt (never a silent pass); #27 will
later turn it into a raise-k re-VERIFY rather than a stop.
"""

from __future__ import annotations

from enum import Enum
from typing import assert_never

from forseti.esbmc import EsbmcResult, Error, Unknown, Verified, Violated


class LoopState(Enum):
    """A node in the write->verify->fix loop."""

    WRITE = "write"
    VERIFY = "verify"
    FIX = "fix"
    UNKNOWN = "unknown"
    DONE = "done"
    GIVE_UP = "give_up"


def next_state(result: EsbmcResult) -> LoopState:
    """Map one ESBMC verdict to the loop state it leads to.

    The `match` is exhaustive over the sealed `EsbmcResult` union: the final
    `assert_never` makes adding a new verdict arm a mypy error here, so no
    outcome can be silently dropped.
    """
    match result:
        case Verified():
            return LoopState.DONE
        case Violated():
            return LoopState.FIX
        case Unknown():
            return LoopState.UNKNOWN
        case Error():
            return LoopState.GIVE_UP
        case _:
            assert_never(result)

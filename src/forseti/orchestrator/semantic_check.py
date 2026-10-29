"""The shared preparation recipe for semantic checking and differential grading.

The plan couples a length cap, its default ladder, the harness writer and the
source-aware ESBMC binding. Callers retain execution through ``check_properties``
and own stores, work directories, events, safety filtering and timing adapters.
No source is read, harness rendered or verifier invoked by preparation.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Sequence
from functools import partial
from pathlib import Path

from forseti.esbmc import EsbmcResult, verify
from forseti.precond import DEFAULT_MAX_LEN
from forseti.precond.run import escalating_port
from forseti.properties import Property, PropertyStatus

from .check import SemanticHarnessWriter
from .ladder import validated_ladder
from .ports import HarnessWriterPort, VerifyPort

# Interactive checks and grading share these defaults. The Claude Code gate
# selects its own shorter ladder and per-hook budget in property_gate.py.
DEFAULT_UNWIND = 4
DEFAULT_UNWIND_LADDER: tuple[int, ...] = (8, 16)
DEFAULT_TIMEOUT_S = 110.0


def default_unwind_ladder_above(
    unwind: int, max_len: int | None = None
) -> tuple[int, ...]:
    """Default rungs above ``unwind``, reaching past ``max_len`` when given.

    An omitted ladder derives these rungs, while an explicit ladder (including
    ``()``) is authoritative. A buffer fill loop needs ``k > max_len`` (#299),
    so append ``max_len + 1`` when the default rungs would never clear the cap.
    Re-exported by ``core.check`` for its documented CLI/MCP default.
    """
    rungs = tuple(k for k in DEFAULT_UNWIND_LADDER if k > unwind)
    if max_len is not None and max_len >= (rungs[-1] if rungs else unwind):
        rungs += (max_len + 1,)
    return rungs


def validated_max_len(max_len: int | None) -> int | None:
    """Reject a negative cap before it could make a length assumption vacuous."""
    if max_len is not None and max_len < 0:
        raise ValueError(f"max_len must be >= 0, got {max_len}")
    return max_len


def esbmc_verify_for(
    *,
    timeout_s: float | None = DEFAULT_TIMEOUT_S,
    esbmc_bin: str = "esbmc",
    extra_flags: Sequence[str] = (),
) -> Callable[[Path], VerifyPort]:
    """Bind each source's semantic verifier, including its quoted-header lookup.

    Caller include paths stay first. Unwinding assertions remain enabled and
    their violations escalate as UNKNOWN; real counterexamples are unchanged.
    Re-exported by ``grading.discriminate`` for existing callers.
    """

    def factory(source: Path) -> VerifyPort:
        return escalating_port(
            partial(
                verify,
                timeout_s=timeout_s,
                esbmc_bin=esbmc_bin,
                extra_flags=(*extra_flags, f"-I{source.resolve().parent}"),
                no_unwinding_assertions=False,
            )
        )

    return factory


class TimedVerify:
    """A `VerifyPort` that sums the ESBMC time and counts the runs it forwards."""

    def __init__(self, port: VerifyPort) -> None:
        self._port = port
        self.seconds = 0.0
        self.runs = 0

    def __call__(self, source: Path, /, *, unwind: int) -> EsbmcResult:
        result = self._port(source, unwind=unwind)
        self.seconds += result.meta.duration_s
        self.runs += 1
        return result


class FixedProperties:
    """A `PropertyStorePort` over a fixed property list, whatever the unit id."""

    def __init__(self, props: Sequence[Property]) -> None:
        self._props = tuple(props)

    def list_for_unit(
        self, unit_id: str, statuses: Collection[PropertyStatus] | None = None
    ) -> Sequence[Property]:
        return self._props


class SemanticCheckPlan:
    """Prepare compatible bounds and adapters once for a checking operation.

    Construction validates ``max_len`` and derives an omitted ladder, without
    constructing a writer or resolving a source. ``validate_ladder`` is explicit:
    grading calls it before safety checks/source reading, while Core leaves it
    to ``check_properties`` after opening its store and recording its start.
    Injected adapters remain authoritative and bypass the production binding.
    """

    def __init__(
        self,
        *,
        unwind: int = DEFAULT_UNWIND,
        unwind_ladder: tuple[int, ...] | None = None,
        max_len: int | None = DEFAULT_MAX_LEN,
        timeout_s: float | None = DEFAULT_TIMEOUT_S,
        esbmc_bin: str = "esbmc",
        extra_flags: Sequence[str] = (),
        render: HarnessWriterPort | None = None,
        verify_for: Callable[[Path], VerifyPort] | None = None,
    ) -> None:
        self._max_len = validated_max_len(max_len)
        self._unwind = unwind
        self._ladder = (
            unwind_ladder
            if unwind_ladder is not None
            else default_unwind_ladder_above(unwind, max_len)
        )
        self._writer = render or None
        self._verify_for = verify_for or esbmc_verify_for(
            timeout_s=timeout_s, esbmc_bin=esbmc_bin, extra_flags=extra_flags
        )

    @property
    def unwind(self) -> int:
        return self._unwind

    @property
    def max_len(self) -> int | None:
        return self._max_len

    @property
    def ladder(self) -> tuple[int, ...]:
        """The ladder tail; explicit ``()`` still means no escalation."""
        return self._ladder

    @property
    def writer(self) -> HarnessWriterPort:
        """Select the writer only when the caller actually proceeds to checking."""
        if self._writer is None:
            self._writer = SemanticHarnessWriter(max_len=self._max_len)
        return self._writer

    def validate_ladder(self) -> None:
        """Reject a malformed ladder at the caller's existing validation phase."""
        validated_ladder(self._unwind, self._ladder)

    def verify_for(self, source: Path) -> VerifyPort:
        """Select a source-specific verifier; callers may wrap it for timing."""
        return self._verify_for(source)

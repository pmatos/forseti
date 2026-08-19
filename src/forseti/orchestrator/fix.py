"""The #28 fix-request contract: counterexample -> FixProvider -> apply.

The seam by which the loop obtains a fix without owning the agent/LLM. A
`FixRequest` is the violation handed to a fixer (the violated property, the
concrete input seeds, the full trace, and the current source); a `FixProvider`
turns that request into proposed patched *source text* — no disk, no network,
so the agent-backed provider (harness epic #14) is a drop-in against the same
protocol. The disk mechanics (write the patched unit, re-enter VERIFY) live in
the applier, not the provider, so `run_loop` stays effect-free.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from forseti.esbmc import (
    Assignment,
    Counterexample,
    Step,
    Violated,
    ViolatedProperty,
)


@dataclass(frozen=True)
class FixRequest:
    """A violation handed to a fixer: what failed, on what input, in what source.

    Carries the typed `counterexample` when ESBMC's trace parsed and always the
    raw text as a lossless fallback (parsing failure never downgrades the
    verdict, so the request must survive it). The `violated_property`/`inputs`/
    `steps` accessors are None-safe projections of the typed cex.
    """

    source: Path
    source_text: str
    counterexample: Counterexample | None
    raw_counterexample: str

    @property
    def violated_property(self) -> ViolatedProperty | None:
        """The property ESBMC found violated, or `None` when the cex didn't parse."""
        if self.counterexample is None:
            return None
        return self.counterexample.violated_property

    @property
    def inputs(self) -> tuple[Assignment, ...]:
        """The concrete input seeds (`Counterexample.inputs`); `()` without a cex."""
        if self.counterexample is None:
            return ()
        return self.counterexample.inputs

    @property
    def steps(self) -> tuple[Step, ...]:
        """The full ordered trace; `()` when the cex didn't parse."""
        if self.counterexample is None:
            return ()
        return self.counterexample.steps

    @classmethod
    def from_violation(
        cls, source: Path, source_text: str, violated: Violated
    ) -> FixRequest:
        """Build a request from a `Violated` verdict plus the unit's current text.

        `source_text` is passed in (read at the effect boundary, the applier) so
        constructing a `FixRequest` stays pure.
        """
        return cls(
            source=source,
            source_text=source_text,
            counterexample=violated.counterexample,
            raw_counterexample=violated.raw_counterexample,
        )


class FixProvider(Protocol):
    """Turns a `FixRequest` into proposed patched source *text*.

    Pure with respect to disk and network: it returns the candidate source, and
    the applier owns writing it. The real agent-backed provider (harness epic
    #14) implements this same protocol against an LLM call.
    """

    def propose_fix(self, request: FixRequest) -> str: ...


class RecordedFixProvider:
    """A `FixProvider` that replays a pre-recorded fix per source path.

    Backs tests and demos. `mapping` sends a source path to the known-good
    replacement file, returned as text. It is *single-round by contract*: keyed
    on the request's `source`, which after the first apply is the applier's
    written path — an unmapped source raises `KeyError` (fail-loud). Multi-round
    recorded fixes are #14's concern, not this contract's.
    """

    def __init__(self, mapping: dict[Path, Path]) -> None:
        self._mapping = mapping
        self.calls = 0

    def propose_fix(self, request: FixRequest) -> str:
        self.calls += 1
        return self._mapping[request.source].read_text()


class ProviderFixPort:
    """Adapts a `FixProvider` into the loop's `FixPort` — the apply boundary.

    On each call it reads the current source, builds the `FixRequest`, asks the
    provider for patched text, and writes that text to a fresh versioned unit
    (`<stem>.fix<N><suffix>`) beside the original source. Same directory,
    because a non-self-contained C unit with sibling quoted includes
    (`#include "harness.h"`) resolves those relative to the including file's
    own directory; writing the candidate elsewhere breaks that resolution on
    re-verify (issue #39). The residual: a unit that `#include`s itself by its
    own literal filename still reaches the live original, since a fresh
    `.fixN` name can never occupy it.

    Writing a new file per fix keeps every `Iteration.source` a distinct path
    and never mutates `original`. Exclusive creation means a stale leftover
    from a previous run fails loud instead of being silently overwritten.
    Because it returns the next path to verify, `run_loop`'s existing "fix
    then re-verify" loop *is* the apply+re-enter — the driver is unchanged.

    One instance is scoped to one unit, named explicitly by the `original`
    constructor argument. Every `__call__` must pass either that same path or
    one of this instance's own prior outputs (fed back in by `run_loop` on
    round 2+) — an unrelated source fails loud instead of silently writing a
    second unit's fix under the first unit's stem.
    """

    def __init__(self, provider: FixProvider, *, original: Path) -> None:
        self._provider = provider
        self._original = original
        self._attempt = 0

    def _candidate(self, attempt: int) -> Path:
        return (
            self._original.parent
            / f"{self._original.stem}.fix{attempt}{self._original.suffix}"
        )

    def __call__(self, source: Path, violated: Violated) -> Path:
        prior_outputs = {self._candidate(i) for i in range(1, self._attempt + 1)}
        if source != self._original and source not in prior_outputs:
            raise ValueError(
                f"ProviderFixPort is bound to {self._original}; got unrelated "
                f"source {source}. Use a separate ProviderFixPort per unit."
            )
        attempt = self._attempt + 1
        dest = self._candidate(attempt)
        if dest.exists():
            raise FileExistsError(f"candidate already exists: {dest}")
        request = FixRequest.from_violation(source, source.read_text(), violated)
        patched = self._provider.propose_fix(request)
        # Burn the attempt number before writing, not before propose_fix: a
        # propose_fix failure leaves no file behind and should stay retryable
        # under the same number, but a write failure (e.g. disk full) after
        # `open("x")` already created `dest` must not let the next call
        # recompute and collide with that same `dest` forever.
        self._attempt = attempt
        with dest.open("x") as f:
            f.write(patched)
        return dest


if TYPE_CHECKING:
    # Type-check-only structural guards: fail type-checking if a concrete class
    # ever drifts from the protocol it is meant to satisfy (mirrors test_loop.py).
    from .ports import FixPort

    def _provider_is_fixprovider(p: RecordedFixProvider) -> FixProvider:
        return p

    def _applier_is_fixport(a: ProviderFixPort) -> FixPort:
        return a

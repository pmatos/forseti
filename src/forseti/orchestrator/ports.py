"""The injected seams the loop driver depends on.

`VerifyPort` is the subset of `forseti.esbmc.verify`'s signature the driver
calls, so the real `verify` is structurally assignable to it. `FixPort` is the
minimal fix seam: given the current source and the violation, it returns the
path of the next source to verify — performing whatever edit/write it needs
*outside* the driver, so `run_loop` stays pure. The richer
FixRequest/FixProvider contract is #28.

The W2.5 property-check driver (`check_properties`, #66) adds two more seams:
`PropertyStorePort` (the read side of the #62 store — the properties for one
unit) and `HarnessWriterPort` (the #64 renderer projecting a stored property
into a compilable ESBMC harness *text*). Its `Unit`/`RenderedHarness` carriers
live here so the driver need not import #62/#64 directly.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from forseti.esbmc import EsbmcResult, Violated
from forseti.properties import Property, PropertyStatus


class VerifyPort(Protocol):
    """Runs ESBMC on `source` at bound `unwind` and returns the verdict."""

    def __call__(self, source: Path, *, unwind: int) -> EsbmcResult: ...


class FixPort(Protocol):
    """Turns a violation into the next source to verify (effects are its own)."""

    def __call__(self, source: Path, violated: Violated) -> Path: ...


@dataclass(frozen=True)
class Unit:
    """The verification unit under check, keyed `path::symbol` (#66).

    `source_text` is the *main-free kernel slice* defining `symbol` — read at the
    effect boundary (`from_path`) and passed in so harness rendering stays
    disk-free (mirrors `FixRequest.source_text`). Passing the slice, not the
    `examples/*.c` file, is what keeps a hand-written property (the example's own
    `main`/`assert`) out of the checked path.
    """

    unit_id: str  # "path::symbol"
    path: Path  # file defining `symbol`
    symbol: str  # function under test
    source_text: str

    @classmethod
    def from_path(cls, path: Path, symbol: str) -> Unit:  # effect boundary
        """Read `path` and build the unit keyed `path::symbol`."""
        return cls(f"{path}::{symbol}", path, symbol, path.read_text())


@dataclass(frozen=True)
class RenderedHarness:
    """#64's output: a self-contained, compilable ESBMC harness as *text*.

    Mirrors `fix.py`'s "return patched text" seam. Self-contained = embeds the
    unit slice + a nondet `main` + the property encoded as an `__ESBMC_assert`;
    `check_properties` verifies this single file. `language` selects the esbmc
    frontend (C only for now, ADR-0003) and is provenance for the widening to
    come.
    """

    source_text: str
    language: str = "c"


class PropertyStorePort(Protocol):
    """Read side of the #62 store the driver needs: properties for one unit.

    `statuses` scopes the read to a lifecycle subset (`None` = every row); the
    check driver passes the valid-input subset so terminal rows never reach a
    verdict (#84).
    """

    def list_for_unit(
        self,
        unit_id: str,
        statuses: Collection[PropertyStatus] | None = None,
    ) -> Sequence[Property]: ...


class HarnessWriterPort(Protocol):
    """#64: render a stored property into a compilable ESBMC harness (text)."""

    def render(self, unit: Unit, prop: Property) -> RenderedHarness: ...

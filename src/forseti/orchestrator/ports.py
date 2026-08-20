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

from forseti.esbmc import (
    EsbmcResult,
    Violated,
    rename_all_declarations_and_definitions,
)
from forseti.properties import Property, PropertyStatus

_RENAMED_MAIN = "__forseti_unused_main"


class VerifyPort(Protocol):
    """Runs ESBMC on `source` at bound `unwind` and returns the verdict.

    `source` is positional-only: every implementation is called positionally
    (`verify_fn(path, unwind=k)`), and a fixed parameter name would otherwise
    make callables that name it differently (lambdas, fakes) structurally
    incompatible.
    """

    def __call__(self, source: Path, /, *, unwind: int) -> EsbmcResult: ...


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

    `symbol` is the identifier `source_text` actually defines — not necessarily
    the one `from_path` was asked for (see its docstring on checking `main`
    itself); `unit_id` always keeps the *requested* spelling, since that is the
    store's own lookup key.
    """

    unit_id: str  # "path::symbol", the requested spelling
    path: Path  # file defining `symbol`
    symbol: str  # function under test, as it actually appears in `source_text`
    source_text: str

    @classmethod
    def from_path(cls, path: Path, symbol: str) -> Unit:  # effect boundary
        """Read `path` and build the unit keyed `path::symbol`.

        `path` need not already be main-free: a normal executable translation
        unit (helper function + its own `main`) is a legitimate check target,
        not just a hand-written `examples/*.c` kernel slice.
        `rename_all_declarations_and_definitions` renames every genuine `main`
        declaration or definition out of the way (every textual alternative,
        e.g. an inactive ``#if 0`` definition, and any forward-declared
        prototype regardless of whether its signature happens to match —
        issue #95 review) so `source_text` satisfies this class's own
        main-free contract either way — a `main` appearing only in a comment
        is left untouched (issue #95 review: `render_semantic_harness` rejects
        any `unit_source` defining `main`, which previously made every stored
        property for such a unit an unconditional `ERROR`; a *left-behind*
        prototype with a signature that doesn't match the harness's own
        generated ``int main(void)`` is a hard esbmc parse error, not merely
        an `ERROR` verdict, so it has to go too rather than being merely
        tolerated by a looser "is this a definition?" check).

        When `symbol` itself is `"main"` — checking `main`'s own semantic
        properties is a legitimate request — it gets renamed right along with
        every other declaration/definition, so `Unit.symbol` tracks the
        renamed identifier instead of the now-absent `"main"`: otherwise
        `SemanticHarnessWriter` would look up a symbol the rename just erased
        and report every property an `ERROR` (issue #95 review). `unit_id`
        still uses the originally requested `symbol`, matching how the
        property store already keys this unit.
        """
        source_text = rename_all_declarations_and_definitions(
            path.read_text(), "main", _RENAMED_MAIN
        )
        effective_symbol = _RENAMED_MAIN if symbol == "main" else symbol
        return cls(f"{path}::{symbol}", path, effective_symbol, source_text)


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

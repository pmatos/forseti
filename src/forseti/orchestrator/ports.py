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

import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from forseti.esbmc import EsbmcResult, Violated, find_definition_brace
from forseti.properties import Property, PropertyStatus


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
    """

    unit_id: str  # "path::symbol"
    path: Path  # file defining `symbol`
    symbol: str  # function under test
    source_text: str

    @classmethod
    def from_path(cls, path: Path, symbol: str) -> Unit:  # effect boundary
        """Read `path` and build the unit keyed `path::symbol`.

        `path` need not already be main-free: a normal executable translation
        unit (helper function + its own `main`) is a legitimate check target,
        not just a hand-written `examples/*.c` kernel slice. `_rename_own_main`
        renames a genuine `main` *definition* out of the way so `source_text`
        satisfies this class's own main-free contract either way — a mere
        prototype/declaration or a `main` appearing only in a call or comment
        is left untouched (issue #95 review: `render_semantic_harness` rejects
        any `unit_source` defining `main`, which previously made every stored
        property for such a unit an unconditional `ERROR`).
        """
        return cls(
            f"{path}::{symbol}", path, symbol, _rename_own_main(path.read_text())
        )


def _rename_own_main(source_text: str) -> str:
    """`source_text` with a genuine `main` *definition* renamed out of the way.

    Reuses `find_definition_brace` (RFC-0003 S3's own definition-shaped scan —
    comment-aware, skips a prototype or a call site) to find the body brace of
    an actual `main` definition, then renames the *last* `main` token before
    that brace — the definition's own name, not an earlier prototype — leaving
    return type, parameters, attributes, and body untouched. `None` (no
    definition-shaped `main`) leaves `source_text` unchanged.
    """
    brace = find_definition_brace(source_text, "main")
    if brace is None:
        return source_text
    matches = list(re.finditer(r"\bmain\b", source_text[:brace]))
    if not matches:
        return source_text
    name_match = matches[-1]  # nearest to the brace is the definition's own name
    start, end = name_match.span()
    return f"{source_text[:start]}__forseti_unused_main{source_text[end:]}"


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

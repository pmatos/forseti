"""The `path::symbol` unit-id string convention, owned in one place.

A verification unit is keyed `<path>::<symbol>` (ADR-0009): a source path and the
symbol of the function within it. ~10 producers used to hand-format that with an
f-string and one consumer re-parsed it with a bare `split`, so the join and split
rules could drift with nothing to enforce them. This leaf owns them: `make_unit_id`
joins, `unit_id_symbol` recovers the symbol, `unit_id_prefix` builds the
"every unit under this path" match prefix.

Scope is the `::` separator only. This module deliberately does **not** normalize or
validate the path: each producer keeps its own left-operand spelling — Core's raw
unnormalized `source`, the Claude gate's `..`-resolved relative `rel`
(`forseti_gate.unit_id()`), oh_my_pi's own path — so every produced string is
byte-identical to the historical `f"{path}::{symbol}"` and every on-disk store key
is unchanged. A `path` is assumed to be `::`-free (every real producer passes a
filesystem path); that is what makes the round-trip hold and is not checked here.

Stdlib-only and free of `forseti` imports so it stays a dependency-free leaf that
every tier can import without a cycle and without dragging heavier packages into
the latency-sensitive hooks.
"""

from __future__ import annotations

import os

_SEP = "::"


def make_unit_id(path: str | os.PathLike[str], symbol: str) -> str:
    """Join an already-spelled `path` and `symbol` into ``<path>::<symbol>``.

    Byte-identical to the historical ``f"{path}::{symbol}"`` — the same ``str``
    interpolation, never ``os.fspath``, so a `Path` left operand stringifies exactly
    as it did before. `path` is taken verbatim: no normalization, no ``..``
    resolution. `symbol` may be the ``?`` sentinel used for an unknown symbol.
    """
    return f"{path}{_SEP}{symbol}"


def unit_id_symbol(unit_id: str) -> str:
    """The symbol half of `unit_id`: everything after the FIRST ``::``.

    Splitting on the first separator lets a C++ ``p::Foo::bar`` keep ``Foo::bar`` as
    its symbol. A string with no ``::`` is returned unchanged — the silent
    malformed-id fallback the sole consumer already relied on, where a
    separator-less id reads as all-symbol.
    """
    return unit_id.split(_SEP, 1)[1] if _SEP in unit_id else unit_id


def unit_id_prefix(path: str | os.PathLike[str]) -> str:
    """The ``<path>::`` prefix shared by every unit id under `path`.

    Equal to ``make_unit_id(path, "")``; a `unit_id.startswith(unit_id_prefix(path))`
    test selects the ids that belong to one source file.
    """
    return make_unit_id(path, "")

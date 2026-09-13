"""Pin the `path::symbol` unit-id seam (`forseti.unit_id`).

The seam owns exactly the `::` join and the first-`::` split. These tests are the
interface: byte-identity against the legacy f-string / split expressions the seam
replaces, the C++ `Foo::bar` symbol survival, the `?` sentinel, the silent no-`::`
fallback, and the empty-symbol prefix used by the gate's prune loop. Non-normalization
is pinned too: a `..`-bearing path passes through verbatim, so Core's raw spelling and
the gate's `..`-resolved spelling each serialize to their own unchanged key.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from forseti.unit_id import make_unit_id, unit_id_prefix, unit_id_symbol


def _legacy_symbol(unit_id: str) -> str:
    """The exact expression `proposer.py:87` used before the seam."""
    return unit_id.split("::", 1)[1] if "::" in unit_id else unit_id


@pytest.mark.parametrize(
    "path, symbol",
    [
        ("examples/abs.c", "my_abs"),
        (Path("examples/abs.c"), "my_abs"),
        ("../external/x.c", "f"),  # `..` must survive un-normalized
        ("a/../b.c", "f"),
        ("rel", "?"),  # unknown-unit sentinel (forseti_gate.py:2055)
        ("p", "Foo::bar"),  # a C++ symbol on the right of the first `::`
    ],
)
def test_make_unit_id_byte_identical_to_legacy_fstring(
    path: str | Path, symbol: str
) -> None:
    assert make_unit_id(path, symbol) == f"{path}::{symbol}"


def test_make_unit_id_str_and_path_agree() -> None:
    assert make_unit_id("a/b.c", "f") == make_unit_id(Path("a/b.c"), "f")


def test_make_unit_id_does_not_normalize() -> None:
    assert make_unit_id("a/../b.c", "f") == "a/../b.c::f"
    assert make_unit_id("../external/x.c", "f") == "../external/x.c::f"


def test_symbol_splits_on_first_separator() -> None:
    # A C++ `Foo::bar` symbol survives: only the first `::` is the separator.
    assert unit_id_symbol("p::Foo::bar") == "Foo::bar"
    assert unit_id_symbol(make_unit_id("p", "Foo::bar")) == "Foo::bar"


def test_symbol_sentinel_roundtrips() -> None:
    assert make_unit_id("rel", "?") == "rel::?"
    assert unit_id_symbol("rel::?") == "?"


def test_symbol_malformed_fallback_is_whole_string() -> None:
    # No `::` -> the id reads as all-symbol (the silent fallback proposer.py:87 had).
    assert unit_id_symbol("noseparator") == "noseparator"


@pytest.mark.parametrize(
    "raw",
    [
        "foo",
        "a::b",
        "p::Foo::bar",
        "a::",
        "::",
        ":::",
        "::foo",
        "examples/abs.c::my_abs",
    ],
)
def test_symbol_matches_legacy_expression(raw: str) -> None:
    assert unit_id_symbol(raw) == _legacy_symbol(raw)


def test_symbol_roundtrips_for_separator_free_path() -> None:
    # The pinned round-trip holds whenever the path itself carries no `::`
    # (always true for real filesystem paths).
    for path, symbol in [("a/b.c", "f"), ("rel", "?"), ("p", "Foo::bar")]:
        assert unit_id_symbol(make_unit_id(path, symbol)) == symbol


def test_prefix_is_empty_symbol_join() -> None:
    assert unit_id_prefix("dir/f.c") == "dir/f.c::"
    assert unit_id_prefix(Path("dir/f.c")) == "dir/f.c::"


def test_prefix_selects_units_under_one_path() -> None:
    # The relationship the gate's prune loop (forseti_gate.py:1832) relies on.
    prefix = unit_id_prefix("dir/f.c")
    assert make_unit_id("dir/f.c", "g").startswith(prefix)
    assert not make_unit_id("dir/f2.c", "g").startswith(prefix)

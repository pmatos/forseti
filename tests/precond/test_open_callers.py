"""Tests for `open_caller_checks` — the pure core of the openings seam (no ESBMC).

The five ways a callee's caller set can be *wider* than what `list_units` reports
(a header-defined caller, an address escape, an alias, a load-time attribute, a
GNU inline-asm call) each turn into a `CallerCheck` that withholds the discharge.
This is the dict-in / `CallerCheck`s-out core: one place that owns the taxonomy,
testable directly without a translation unit, a filesystem, or esbmc.
"""

from __future__ import annotations

from pathlib import Path

from forseti.precond.discharge import (
    CallerCheck,
    CallerOutcome,
    open_caller_checks,
)

SOURCE = Path("/proj/frame.c")


def _checks(**kw: tuple[str, ...]) -> tuple[CallerCheck, ...]:
    defaults: dict[str, tuple[str, ...]] = {
        "foreign": (),
        "escaped": (),
        "aliased": (),
        "implicit": (),
        "asm_sites": (),
    }
    defaults.update(kw)
    return open_caller_checks("sum_bytes", SOURCE, **defaults)


def test_no_openings_is_empty() -> None:
    assert _checks() == ()


def test_a_foreign_caller_is_unchecked() -> None:
    (check,) = _checks(foreign=("header_client",))
    assert check.caller == "header_client"
    assert check.outcome is CallerOutcome.UNCHECKED
    assert "defined outside" in check.detail
    assert str(SOURCE) in check.detail


def test_an_address_escape_is_unresolved() -> None:
    (check,) = _checks(escaped=("fp",))
    assert check.caller == "fp"
    assert check.outcome is CallerOutcome.UNRESOLVED
    assert "names sum_bytes() outside a direct call" in check.detail


def test_an_alias_is_unresolved() -> None:
    (check,) = _checks(aliased=("checksum_alias",))
    assert check.caller == "checksum_alias"
    assert check.outcome is CallerOutcome.UNRESOLVED
    assert "another name for sum_bytes()" in check.detail


def test_an_implicit_invocation_names_its_attribute_kind() -> None:
    (check,) = _checks(implicit=("constructor",))
    assert check.caller == "<constructor>"
    assert check.outcome is CallerOutcome.UNRESOLVED
    assert "carries a constructor attribute" in check.detail
    assert "sum_bytes()" in check.detail


def test_an_asm_site_is_unresolved() -> None:
    (check,) = _checks(asm_sites=("via_asm",))
    assert check.caller == "via_asm"
    assert check.outcome is CallerOutcome.UNRESOLVED
    assert "GNU inline assembly" in check.detail


def test_the_concatenation_order_is_stable() -> None:
    # foreign (UNCHECKED) then the four UNRESOLVED kinds, in a fixed order the
    # aggregation and the `result.callers[0]` assertions in test_discharge rely on.
    checks = _checks(
        foreign=("F",),
        escaped=("E",),
        aliased=("A",),
        implicit=("constructor",),
        asm_sites=("S",),
    )
    assert [c.caller for c in checks] == ["F", "E", "A", "<constructor>", "S"]
    assert [c.outcome for c in checks] == [
        CallerOutcome.UNCHECKED,
        CallerOutcome.UNRESOLVED,
        CallerOutcome.UNRESOLVED,
        CallerOutcome.UNRESOLVED,
        CallerOutcome.UNRESOLVED,
    ]


def test_multiple_names_in_one_category_are_preserved_in_order() -> None:
    checks = _checks(foreign=("a", "b", "c"))
    assert [c.caller for c in checks] == ["a", "b", "c"]
    assert all(c.outcome is CallerOutcome.UNCHECKED for c in checks)

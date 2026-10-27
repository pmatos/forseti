"""Tests for `open_caller_checks` — the pure core of the openings seam (no ESBMC).

The five ways a callee's caller set can be *wider* than what `list_units` reports
(a header-defined caller, an address escape, an alias, a load-time attribute, a
GNU inline-asm call) each turn into a `CallerCheck` that withholds the discharge.
This is the `CallerOpenings`-in / `CallerCheck`s-out core: one place that owns
the taxonomy, testable directly without a translation unit, a filesystem, or
esbmc.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from forseti.esbmc import CallerOpenings, ListUnitsError
from forseti.precond.discharge import (
    CallerCheck,
    CallerOutcome,
    find_open_callers,
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
    return open_caller_checks("sum_bytes", SOURCE, CallerOpenings(**defaults))


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


def test_repeated_sites_remain_separate_withholding_reasons() -> None:
    # One site can open the caller set in several ways. Neither overlapping
    # categories nor repeated names may erase evidence from the report.
    checks = _checks(
        foreign=("shared", "shared"),
        escaped=("shared", "later"),
        aliased=("shared",),
        implicit=("constructor", "destructor"),
        asm_sites=("shared",),
    )
    assert [c.caller for c in checks] == [
        "shared",
        "shared",
        "shared",
        "later",
        "shared",
        "<constructor>",
        "<destructor>",
        "shared",
    ]
    assert [c.outcome for c in checks] == [
        CallerOutcome.UNCHECKED,
        CallerOutcome.UNCHECKED,
        *([CallerOutcome.UNRESOLVED] * 6),
    ]
    assert checks[0] == checks[1]
    assert len({c.detail for c in checks if c.caller == "shared"}) == 4
    assert all(c.esbmc_result is None and c.settled_k is None for c in checks)


def test_find_open_callers_projects_the_acquired_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = Path("relative path/other.c")
    openings = CallerOpenings(
        foreign=("header_client",),
        escaped=("fp",),
        aliased=("other_alias",),
        implicit=("constructor",),
        asm_sites=("via_asm",),
    )
    seen: list[tuple[Path, str, str, float]] = []

    def acquire(
        path: Path, symbol: str, *, esbmc_bin: str, timeout_s: float
    ) -> CallerOpenings:
        seen.append((path, symbol, esbmc_bin, timeout_s))
        return openings

    monkeypatch.setattr("forseti.precond.discharge.list_caller_openings", acquire)
    checks = find_open_callers(
        source, "other", esbmc_bin="custom-esbmc", timeout_s=0.25
    )
    assert seen == [(source, "other", "custom-esbmc", 0.25)]
    assert checks == open_caller_checks("other", source, openings)
    assert str(source) in checks[0].detail
    assert all("other()" in c.detail for c in checks[1:])


def test_find_open_callers_propagates_acquisition_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = ListUnitsError("could not acquire caller openings")

    def unavailable(
        path: Path, symbol: str, *, esbmc_bin: str, timeout_s: float
    ) -> CallerOpenings:
        raise error

    monkeypatch.setattr("forseti.precond.discharge.list_caller_openings", unavailable)
    with pytest.raises(ListUnitsError) as excinfo:
        find_open_callers(SOURCE, "sum_bytes")
    assert excinfo.value is error

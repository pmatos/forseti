"""Tests for `forseti.precond.discharge` — the aggregation rule (no ESBMC).

Both esbmc seams are injected, as in `test_precond_verify.py`: canned units and
canned verdicts, dispatched on the generated harness's filename so each phase —
the S2 sidecar, the callee's non-vacuity probe, a caller's obligation run, that
caller's call-site probe — can be answered independently.

The property under test is that the upgrade **fails closed**: it happens only
when every caller was actually checked and every check passed. Every other shape
— an unresolvable caller, an inconclusive ladder, a dead call site, no caller at
all — leaves the honest S2 verdict standing.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from forseti.esbmc import (
    EsbmcResult,
    ListUnitsError,
    RunMeta,
    Unit,
    Unknown,
    UnknownReason,
    Verified,
    Violated,
)
from forseti.esbmc.units import Param
from forseti.precond import ASSESSMENT_EXIT_CODES, Assessment
from forseti.precond.discharge import (
    CallerOutcome,
    DischargeResult,
    discharge_precondition,
    emit_obligations,
)
from forseti.precond.synth import (
    NON_VACUITY_LABEL,
    OBLIGATION_LABEL_PREFIX,
    OBLIGATION_SITE_LABEL_PREFIX,
)
from forseti.precond.verify import PreconditionUnavailable

SOURCE = """\
#include <stddef.h>

unsigned sum_bytes(const unsigned char *buf, size_t len) {
    unsigned acc = 0;
    for (size_t i = 0; i < len; i++) acc += buf[i];
    return acc;
}

unsigned frame_checksum(const unsigned char *frame, size_t len) {
    return sum_bytes(frame, len);
}
"""

CALLEE = Unit(
    "sum_bytes",
    (Param("buf", "const unsigned char *"), Param("len", "unsigned long")),
)
CALLER = Unit(
    "frame_checksum",
    (Param("frame", "const unsigned char *"), Param("len", "unsigned long")),
    ("sum_bytes",),
)


def _meta() -> RunMeta:
    return RunMeta("8.3.0", ("esbmc",), 0, 0.0, "", "")


def _verified() -> Verified:
    return Verified(_meta())


def _violated(text: str) -> Violated:
    return Violated(_meta(), text, None)


def _phase(source: Path) -> str:
    """Which generated harness a canned verdict is being asked about."""
    if "nonvacuity" in source.name:
        return "unit_probe"
    if source.name.endswith("__discharge_site.c"):
        return "caller_probe"
    if source.name.endswith("__discharge.c"):
        return "caller"
    return "unit"


def _raw(caller_verdicts: dict[str, EsbmcResult]) -> Callable[..., EsbmcResult]:
    """S2 always passes; the caller phases answer from `caller_verdicts`."""
    defaults: dict[str, EsbmcResult] = {
        "unit": _verified(),
        "unit_probe": _violated(f"Violated property:\n  {NON_VACUITY_LABEL}"),
        "caller": _verified(),
        "caller_probe": _violated(
            f"Violated property:\n  {OBLIGATION_SITE_LABEL_PREFIX}sum_bytes"
        ),
    }
    defaults.update(caller_verdicts)

    def raw(source: Path, *, unwind: int) -> EsbmcResult:
        return defaults[_phase(source)]

    return raw


def _run(
    tmp: Path,
    *,
    caller_verdicts: dict[str, EsbmcResult] | None = None,
    units: tuple[Unit, ...] = (CALLEE, CALLER),
    function: str = "sum_bytes",
    source_text: str = SOURCE,
    external: Callable[[Path, str], tuple[str, ...]] = lambda _s, _f: (),
    escapes: Callable[[Path, str], tuple[str, ...]] = lambda _s, _f: (),
) -> DischargeResult:
    src = tmp / "frame.c"
    src.write_text(source_text)
    return discharge_precondition(
        src,
        function=function,
        max_len=8,
        ladder_cap=32,
        work_dir=tmp,
        raw_verify=_raw(caller_verdicts or {}),
        list_units_fn=lambda _s: list(units),
        external_callers_fn=external,
        address_escapes_fn=escapes,
    )


def test_every_caller_clean_upgrades_to_discharged(tmp_path: Path) -> None:
    result = _run(tmp_path)
    assert result.assessment is Assessment.DISCHARGED_VERIFIED
    assert [c.outcome for c in result.callers] == [CallerOutcome.DISCHARGED]
    assert "discharged" in result.label
    assert ASSESSMENT_EXIT_CODES[result.assessment] == 0


def test_broken_obligation_is_violated_and_names_the_caller(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        caller_verdicts={
            "caller": _violated(
                f"Violated property:\n  {OBLIGATION_LABEL_PREFIX}sum_bytes:buf"
            )
        },
    )
    assert result.assessment is Assessment.VIOLATED
    assert result.callers[0].outcome is CallerOutcome.OBLIGATION_VIOLATED
    assert "frame_checksum()" in result.label
    assert "call site" in result.label
    assert ASSESSMENT_EXIT_CODES[result.assessment] == 1


def test_caller_with_its_own_bug_does_not_discharge(tmp_path: Path) -> None:
    # A violation that is *not* the obligation means the caller never got there;
    # it is a finding about the caller, and it withholds the upgrade.
    result = _run(
        tmp_path,
        caller_verdicts={
            "caller": _violated("Violated property:\n  dereference failure")
        },
    )
    assert result.assessment is Assessment.ASSUMED_VERIFIED
    assert result.callers[0].outcome is CallerOutcome.CALLER_VIOLATED
    assert "discharge incomplete" in result.label


def test_underdetermined_caller_is_not_blamed_for_the_obligation(
    tmp_path: Path,
) -> None:
    # The mirror-image phantom: `hash_block(const unsigned char *blk)` states no
    # extent, so L0 gives it one byte and it cannot satisfy a 16-byte obligation
    # however correct it is. Flagging it would move RFC-0003's phantom VIOLATED
    # from the callee to the call site.
    bare = Unit("hash_block", (Param("blk", "const unsigned char *"),), ("sum_bytes",))
    result = _run(
        tmp_path,
        units=(CALLEE, bare),
        caller_verdicts={
            "caller": _violated(
                f"Violated property:\n  {OBLIGATION_LABEL_PREFIX}sum_bytes:buf"
            )
        },
    )
    assert result.assessment is Assessment.ASSUMED_VERIFIED
    assert result.callers[0].outcome is CallerOutcome.UNDERDETERMINED
    assert "blk" in result.label
    assert "not attributable" in result.label


def test_a_fixed_extent_caller_is_still_blamed(tmp_path: Path) -> None:
    # The discriminator is *length authority*, not "has a pointer": a written
    # `T p[16]` is something the signature actually states, so an obligation
    # failure there is a genuine caller bug.
    sized = Unit(
        "hash_block",
        (Param("blk", "const unsigned char *", array_extent=16),),
        ("sum_bytes",),
    )
    result = _run(
        tmp_path,
        units=(CALLEE, sized),
        caller_verdicts={
            "caller": _violated(
                f"Violated property:\n  {OBLIGATION_LABEL_PREFIX}sum_bytes:buf"
            )
        },
    )
    assert result.assessment is Assessment.VIOLATED
    assert result.callers[0].outcome is CallerOutcome.OBLIGATION_VIOLATED


def test_unreachable_call_site_does_not_discharge(tmp_path: Path) -> None:
    # The obligation run passes because the call is never made — a vacuous pass
    # that the site probe catches by *also* passing.
    result = _run(tmp_path, caller_verdicts={"caller_probe": _verified()})
    assert result.assessment is Assessment.ASSUMED_VERIFIED
    assert result.callers[0].outcome is CallerOutcome.UNREACHABLE
    assert "discharges nothing" in result.label


def test_inconclusive_caller_ladder_does_not_discharge(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        caller_verdicts={"caller": Unknown(_meta(), UnknownReason.TIMEOUT)},
    )
    assert result.assessment is Assessment.ASSUMED_VERIFIED
    assert result.callers[0].outcome is CallerOutcome.UNCHECKED
    assert "inconclusive" in result.label


def test_inconclusive_site_probe_does_not_discharge(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        caller_verdicts={"caller_probe": Unknown(_meta(), UnknownReason.TIMEOUT)},
    )
    assert result.assessment is Assessment.ASSUMED_VERIFIED
    assert result.callers[0].outcome is CallerOutcome.UNCHECKED
    assert "could not confirm the call is reached" in result.label


def test_caller_l0_cannot_materialise_does_not_discharge(tmp_path: Path) -> None:
    opaque = Unit("frame_checksum", (Param("sink", "void *"),), ("sum_bytes",))
    result = _run(tmp_path, units=(CALLEE, opaque))
    assert result.assessment is Assessment.ASSUMED_VERIFIED
    assert result.callers[0].outcome is CallerOutcome.UNCHECKED
    assert "sink" in result.label


def test_one_broken_caller_outranks_the_clean_ones(tmp_path: Path) -> None:
    # Two callers, and only the aggregate matters: a single obligation failure is
    # a VIOLATED however many siblings discharged.
    second = Unit(
        "payload_checksum",
        (Param("frame", "const unsigned char *"), Param("len", "unsigned long")),
        ("sum_bytes",),
    )

    def raw(source: Path, *, unwind: int) -> EsbmcResult:
        phase = _phase(source)
        if phase == "caller" and source.name.startswith("payload_checksum"):
            return _violated(
                f"Violated property:\n  {OBLIGATION_LABEL_PREFIX}sum_bytes:buf"
            )
        return _raw({})(source, unwind=unwind)

    src = tmp_path / "frame.c"
    src.write_text(SOURCE)
    result = discharge_precondition(
        src,
        function="sum_bytes",
        max_len=8,
        ladder_cap=32,
        work_dir=tmp_path,
        raw_verify=raw,
        list_units_fn=lambda _s: [CALLEE, CALLER, second],
        external_callers_fn=lambda _s, _f: (),
        address_escapes_fn=lambda _s, _f: (),
    )
    assert result.assessment is Assessment.VIOLATED
    assert "payload_checksum()" in result.detail
    assert "frame_checksum()" not in result.detail


def test_a_caller_outside_the_file_withholds_the_upgrade(tmp_path: Path) -> None:
    # A `static inline` in an included header is a caller in this translation
    # unit that `list_units` cannot enumerate by design. Claiming "every caller"
    # while some were never listed would be a claim about a set we never saw.
    result = _run(tmp_path, external=lambda _s, _f: ("header_client",))
    assert result.assessment is Assessment.ASSUMED_VERIFIED
    unchecked = [c for c in result.callers if c.caller == "header_client"]
    assert unchecked and unchecked[0].outcome is CallerOutcome.UNCHECKED
    assert "defined outside" in result.label


def test_an_outside_caller_counts_even_with_no_local_caller(tmp_path: Path) -> None:
    result = _run(tmp_path, units=(CALLEE,), external=lambda _s, _f: ("header_client",))
    assert result.assessment is Assessment.ASSUMED_VERIFIED
    assert [c.caller for c in result.callers] == ["header_client"]
    assert "no caller" not in result.label  # there *is* one; we just cannot check it


def test_an_escaped_address_withholds_the_upgrade(tmp_path: Path) -> None:
    # `static cb_t fp = sum_bytes;` plus an indirect `fp(...)` is a caller no
    # name-based enumeration can reach: the indirect call names the *variable*.
    # A clean sweep of the callers we can see says nothing about that path.
    result = _run(tmp_path, escapes=lambda _s, _f: ("fp",))
    assert result.assessment is Assessment.ASSUMED_VERIFIED
    unresolved = [c for c in result.callers if c.caller == "fp"]
    assert unresolved and unresolved[0].outcome is CallerOutcome.UNRESOLVED
    assert "takes the address of sum_bytes()" in result.label
    # the caller that *was* checked still reports what it established
    assert any(c.outcome is CallerOutcome.DISCHARGED for c in result.callers)


def test_an_escape_counts_even_with_no_local_caller(tmp_path: Path) -> None:
    result = _run(tmp_path, units=(CALLEE,), escapes=lambda _s, _f: ("fp",))
    assert result.assessment is Assessment.ASSUMED_VERIFIED
    assert [c.caller for c in result.callers] == ["fp"]
    assert "no caller" not in result.label  # one exists; it just cannot be named


def test_a_failed_external_listing_is_an_error(tmp_path: Path) -> None:
    def boom(_source: Path, _symbol: str) -> tuple[str, ...]:
        raise ListUnitsError("esbmc --parse-tree-only failed: gone")

    result = _run(tmp_path, external=boom)
    assert result.assessment is Assessment.ERROR
    assert "gone" in result.detail


def test_discharge_states_what_it_is_relative_to(tmp_path: Path) -> None:
    # `frame_checksum` has its own (still assumed) precondition, so proving it
    # calls `sum_bytes` correctly says nothing about a third function calling
    # `frame_checksum` badly. The label must not hide that.
    result = _run(tmp_path)
    assert result.assessment is Assessment.DISCHARGED_VERIFIED
    assert "relative to each caller's own synthesised precondition" in result.label


def test_a_precondition_free_caller_anchors_the_chain(tmp_path: Path) -> None:
    # A caller with no pointer parameters has an empty precondition of its own,
    # so its harness allocates real objects and nothing stays assumed on its
    # side — the chain closes outright and the caveat is dropped.
    entry = Unit("run", (), ("sum_bytes",))
    result = _run(tmp_path, units=(CALLEE, entry))
    assert result.assessment is Assessment.DISCHARGED_VERIFIED
    assert "relative to" not in result.label


def test_no_caller_in_the_translation_unit_stays_assumed(tmp_path: Path) -> None:
    result = _run(tmp_path, units=(CALLEE,))
    assert result.assessment is Assessment.ASSUMED_VERIFIED
    assert result.callers == ()
    assert "exported to its clients" in result.label


def test_scalar_only_unit_has_nothing_to_discharge(tmp_path: Path) -> None:
    scalar = Unit("rotl32", (Param("x", "unsigned int"), Param("c", "int")))
    result = _run(tmp_path, units=(scalar,), function="rotl32")
    assert result.assessment is Assessment.DISCHARGED_VERIFIED
    assert "precondition is empty" in result.label


def test_a_non_assumed_verdict_is_never_upgraded(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        caller_verdicts={
            "unit": _violated("Violated property:\n  array bounds violated")
        },
    )
    assert result.assessment is Assessment.VIOLATED
    assert result.callers == ()
    # the S2 label passes through verbatim: this is the callee's own bug
    assert result.label == result.unit_result.label


def test_unnameable_parameter_is_needs_contract(tmp_path: Path) -> None:
    anonymous = Unit("sum_bytes", (Param("", "const unsigned char *"),))
    result = _run(tmp_path, units=(anonymous, CALLER))
    assert result.assessment is Assessment.NEEDS_CONTRACT
    assert "unnamed" in result.detail


def test_unreadable_source_is_an_error(tmp_path: Path) -> None:
    # esbmc listed the units (injected here), but the text the injection needs
    # cannot be read back — reported, never silently skipped.
    missing = tmp_path / "gone.c"
    result = discharge_precondition(
        missing,
        function="sum_bytes",
        max_len=8,
        ladder_cap=32,
        work_dir=tmp_path,
        raw_verify=_raw({}),
        list_units_fn=lambda _s: [CALLEE, CALLER],
        external_callers_fn=lambda _s, _f: (),
        address_escapes_fn=lambda _s, _f: (),
    )
    assert result.assessment is Assessment.ERROR
    assert "could not read" in result.detail


def test_to_dict_carries_the_per_caller_verdicts(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        caller_verdicts={
            "caller": _violated(
                f"Violated property:\n  {OBLIGATION_LABEL_PREFIX}sum_bytes:buf"
            )
        },
    )
    payload = result.to_dict()
    assert payload["assessment"] == "violated"
    assert payload["assumed"] is False
    assert payload["discharged"] is False
    assert payload["callers"][0]["caller"] == "frame_checksum"
    assert payload["callers"][0]["outcome"] == "obligation_violated"
    assert OBLIGATION_LABEL_PREFIX in payload["callers"][0]["counterexample"]


def test_discharged_to_dict_sets_the_flag(tmp_path: Path) -> None:
    payload = _run(tmp_path).to_dict()
    assert payload["discharged"] is True
    assert payload["assumed"] is False


def test_emit_obligations_returns_the_injected_copy(tmp_path: Path) -> None:
    src = tmp_path / "frame.c"
    src.write_text(SOURCE)
    text = emit_obligations(
        src, function="sum_bytes", list_units_fn=lambda _s: [CALLEE, CALLER]
    )
    assert f"{OBLIGATION_LABEL_PREFIX}sum_bytes:buf" in text
    assert src.read_text() == SOURCE  # the user's file is untouched


def test_emit_obligations_declines_an_unnameable_unit(tmp_path: Path) -> None:
    src = tmp_path / "frame.c"
    src.write_text(SOURCE)
    anonymous = Unit("sum_bytes", (Param("", "const unsigned char *"),))
    with pytest.raises(PreconditionUnavailable) as excinfo:
        emit_obligations(
            src, function="sum_bytes", list_units_fn=lambda _s: [anonymous]
        )
    assert excinfo.value.assessment is Assessment.NEEDS_CONTRACT


def test_emit_obligations_reports_an_unreadable_source(tmp_path: Path) -> None:
    with pytest.raises(PreconditionUnavailable) as excinfo:
        emit_obligations(
            tmp_path / "gone.c",
            function="sum_bytes",
            list_units_fn=lambda _s: [CALLEE],
        )
    assert excinfo.value.assessment is Assessment.ERROR


def test_every_assessment_has_an_exit_code() -> None:
    # mypy cannot catch a missing dict key; a new member without one would be a
    # KeyError at the moment the CLI reports it.
    assert set(ASSESSMENT_EXIT_CODES) == set(Assessment)

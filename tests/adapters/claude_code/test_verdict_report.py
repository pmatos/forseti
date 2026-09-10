"""Unit tests for the shared verdict->report transform (`verdict_report`).

Pins the grammar the two PostToolUse hooks share, driven by a SYNTHETIC
``ReportStyle`` so these tests never depend on either hook's private wording
constant. Each hook's own byte-for-byte output stays pinned in
``test_post_tool_use`` and ``test_out_of_band``; here we exercise the seam
directly and free of ESBMC, stdin, and subprocess.
"""

from __future__ import annotations

import pytest

from forseti.adapters.claude_code import forseti_gate as gate
from forseti.adapters.claude_code import verdict_report

_STYLE = verdict_report.ReportStyle(
    fail_header="HDR: {n} bad.",
    pass_prefix="PFX:",
    trailer="TRAILER.",
)


def _v(
    unit_id: str,
    verdict: str,
    k: int = 8,
    *,
    counterexample: str | None = None,
    detail: str | None = None,
) -> gate.UnitVerdict:
    file, _, fn = unit_id.partition("::")
    return gate.UnitVerdict(
        unit_id, file, fn, verdict, k, counterexample=counterexample, detail=detail
    )


# --- partition ---------------------------------------------------------------


def test_partition_splits_on_the_gate_decision_axes() -> None:
    verified = _v("a::ok", "verified")
    violated = _v("a::bug", "violated")
    unknown = _v("a::slow", "unknown")
    error = _v("a::err", "error")
    needs = _v("a::ptr", gate.NEEDS_CONTRACT)

    part = verdict_report.partition([verified, violated, unknown, error, needs])

    assert part.verified == [verified]
    # not-passed and not NEEDS_CONTRACT — the blocking set (violated/unknown/error)
    assert part.failures == [violated, unknown, error]
    assert part.needs == [needs]


def test_partition_preserves_order() -> None:
    a = _v("a::a", "violated")
    b = _v("a::b", "error")
    assert verdict_report.partition([b, a]).failures == [b, a]


# --- render: pass branch -----------------------------------------------------


def test_render_empty_is_a_silent_pass() -> None:
    report = verdict_report.render([], _STYLE)
    assert report.message == ""
    assert report.exit_code == 0
    assert report.is_failure is False
    assert report.partition.failures == []


def test_render_all_verified_uses_the_style_prefix() -> None:
    report = verdict_report.render(
        [_v("a::f", "verified"), _v("a::g", "verified", k=4)], _STYLE
    )
    assert report.exit_code == 0
    assert report.message == "PFX: VERIFIED up to k — a::f (k=8), a::g (k=4)"


def test_render_needs_only_is_a_loud_pass() -> None:
    needs = _v("a::ptr", gate.NEEDS_CONTRACT)
    report = verdict_report.render([needs], _STYLE)
    assert report.exit_code == 0
    assert report.message == gate.needs_note([needs])


# --- render: failure branch --------------------------------------------------


def test_render_violation_with_counterexample() -> None:
    v = _v("a::f", "violated", counterexample="  x == 0  ")
    report = verdict_report.render([v], _STYLE)
    assert report.exit_code == 2
    assert report.is_failure is True
    assert report.message == "\n".join(
        [
            "HDR: 1 bad.",
            "",
            "✗ a::f — VIOLATED (k=8)",
            "Counterexample:",
            "x == 0",  # counterexample.strip()
            "",
            "TRAILER.",
        ]
    )


def test_render_detail_only_failure() -> None:
    v = _v("a::g", "error", k=4, detail="boom")
    report = verdict_report.render([v], _STYLE)
    assert report.message == "\n".join(
        ["HDR: 1 bad.", "", "✗ a::g — ERROR (k=4)", "  boom", "", "TRAILER."]
    )


def test_render_clips_counterexample_at_cex_clip() -> None:
    v = _v("a::f", "violated", counterexample="Z" * (gate.CEX_CLIP + 500))
    report = verdict_report.render([v], _STYLE)
    assert "Z" * gate.CEX_CLIP in report.message
    assert "Z" * (gate.CEX_CLIP + 1) not in report.message


def test_render_mixed_appends_needs_note_after_trailer() -> None:
    failure = _v("a::bug", "violated", counterexample="c")
    needs = _v("a::ptr", gate.NEEDS_CONTRACT)
    report = verdict_report.render([_v("a::ok", "verified"), failure, needs], _STYLE)
    assert report.exit_code == 2
    assert report.message == "\n".join(
        [
            "HDR: 1 bad.",
            "",
            "✗ a::bug — VIOLATED (k=8)",
            "Counterexample:",
            "c",
            "",
            "TRAILER.",
            "",
            gate.needs_note([needs]),
        ]
    )
    assert report.partition.failures == [failure]
    assert report.partition.needs == [needs]


# --- emit --------------------------------------------------------------------


def test_emit_pass_prints_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    report = verdict_report.render([_v("a::f", "verified")], _STYLE)
    assert verdict_report.emit(report) == 0
    cap = capsys.readouterr()
    assert cap.out == report.message + "\n"
    assert cap.err == ""


def test_emit_failure_prints_to_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    report = verdict_report.render([_v("a::f", "violated", counterexample="c")], _STYLE)
    assert verdict_report.emit(report) == 2
    cap = capsys.readouterr()
    assert cap.err == report.message + "\n"
    assert cap.out == ""


def test_emit_empty_message_prints_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    report = verdict_report.render([], _STYLE)
    assert verdict_report.emit(report) == 0
    cap = capsys.readouterr()
    assert cap.out == ""
    assert cap.err == ""

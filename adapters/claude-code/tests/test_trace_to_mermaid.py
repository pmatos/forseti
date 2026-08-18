"""Tests for the adapter's mermaid renderer (`tools/trace_to_mermaid.py`).

See `tests/adapters/claude_code/test_event_log.py` for the `event_log` tests --
`trace_to_mermaid.py` is a standalone dev tool that stays outside the packaged
adapter (RFC-0004), so it keeps the sys.path-based import via `conftest.py`.

Run from the repo root with the dev venv::

    .venv/bin/python -m pytest adapters/claude-code/tests -q
"""

from __future__ import annotations

import trace_to_mermaid


def _abs_demo_events() -> list[dict[str, object]]:
    """The canonical write -> VIOLATED -> fix -> VERIFIED run for `my_abs`."""
    return [
        {"type": "edit", "tool": "Write", "file": "abs64.c", "functions": ["my_abs"]},
        {
            "type": "verify",
            "unit": "abs64.c::my_abs",
            "verdict": "violated",
            "k": 1,
            "duration_s": 0.14,
        },
        {
            "type": "gate",
            "file": "abs64.c",
            "decision": "block",
            "n_failures": 1,
            "exit_code": 2,
        },
        {"type": "stop", "decision": "block", "n_unverified": 1, "attempt": 1},
        {"type": "edit", "tool": "Edit", "file": "abs64.c", "functions": ["my_abs"]},
        {
            "type": "verify",
            "unit": "abs64.c::my_abs",
            "verdict": "verified",
            "k": 1,
            "duration_s": 0.15,
        },
        {
            "type": "gate",
            "file": "abs64.c",
            "decision": "pass",
            "n_failures": 0,
            "exit_code": 0,
        },
        {"type": "stop", "decision": "allow", "n_unverified": 0, "attempt": 0},
    ]


def test_render_full_loop_to_mermaid() -> None:
    out = trace_to_mermaid.render(_abs_demo_events())

    assert out.startswith("sequenceDiagram")
    for short, name in trace_to_mermaid._PARTICIPANTS:
        assert f"participant {short} as {name}" in out
    assert "C->>G: Write abs64.c (my_abs)" in out
    assert "G->>E: verify abs64.c::my_abs (k=1)" in out
    assert "E-->>G: VIOLATED (0.14s)" in out
    assert "G-->>C: block — 1 cex fed back (exit 2)" in out
    assert "S-->>C: BLOCK (attempt 1)" in out
    assert "E-->>G: VERIFIED (0.15s)" in out
    assert "G-->>C: pass — VERIFIED up to k" in out
    assert "S-->>C: ALLOW (clean)" in out


def test_render_empty_trace_has_placeholder() -> None:
    out = trace_to_mermaid.render([])
    assert "no events recorded yet" in out


def test_render_sanitizes_newlines_and_semicolons() -> None:
    events = [{"type": "edit", "tool": "Write", "file": "a;b\nc.c", "functions": []}]
    out = trace_to_mermaid.render(events)
    # the message line must not contain a raw newline or semicolon from the data
    msg = next(ln for ln in out.splitlines() if ln.strip().startswith("C->>G"))
    assert "\n" not in msg[5:] and ";" not in msg


def test_render_unknown_verdict_falls_back_to_upper() -> None:
    events = [{"type": "verify", "unit": "a.c::f", "verdict": "weird", "k": 2}]
    out = trace_to_mermaid.render(events)
    assert "E-->>G: WEIRD" in out

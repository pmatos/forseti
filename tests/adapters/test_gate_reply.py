"""Tests for the shared harness gate-reply grammar."""

from __future__ import annotations

from forseti.adapters.gate_reply import render_gate_reply


def test_violations_block_and_append_inconclusive_findings_in_order() -> None:
    reply = render_gate_reply(
        [("a.c", "first counterexample"), ("b.c", "second counterexample")],
        [("c.c", "unknown"), ("d.c", "error")],
        violation_header="Forseti found a counterexample:",
        verb="verify",
    )

    assert reply.decision == "block"
    assert (
        reply.message
        == """Forseti found a counterexample:

### VIOLATED: a.c
first counterexample

### VIOLATED: b.c
second counterexample

Also inconclusive (do not ignore): c.c [unknown], d.c [error]"""
    )


def test_inconclusive_findings_are_unresolved() -> None:
    reply = render_gate_reply(
        [],
        [("unit.c::f", "unknown")],
        violation_header="unused",
        verb="check",
    )

    assert reply.decision == "unresolved"
    assert reply.message == (
        "Forseti could not conclusively check: unit.c::f [unknown]. "
        "Not a pass — raise k, add an entry/harness, or report."
    )


def test_empty_findings_are_a_silent_pass() -> None:
    reply = render_gate_reply(
        [],
        [],
        violation_header="unused",
        verb="verify",
    )

    assert reply.decision == "pass"
    assert reply.message == ""

"""Tests for `build_argv`, the pure ESBMC command-line construction seam.

`build_argv` concentrates every decision about *what command line esbmc is
handed* — the `--unwind K --no-unwinding-assertions` discipline, where the
`--function` target and `--timeout` land, and how a float budget is encoded —
into one directly-testable unit. Before this seam, the argv was assembled
inline in `verify()` and only reachable by mocking `subprocess.run`, so its
shape (and a sub-second `--timeout` truncation bug) had no coverage.

Expected timeout tokens here are independent literals derived from the spec
("esbmc timeouts are whole seconds, rounded up, and a non-None budget is never
esbmc's unbounded 0s"), not recomputed via the implementation's own arithmetic.
"""

from __future__ import annotations

from pathlib import Path

from forseti.esbmc import build_argv


def _value_after(argv: tuple[str, ...], flag: str) -> str:
    """The token immediately following `flag` (asserts `flag` is present)."""
    assert flag in argv, f"{flag!r} not in {argv!r}"
    return argv[argv.index(flag) + 1]


SRC = Path("proj/kernel.c")


def test_binary_is_argv0_and_source_is_argv1() -> None:
    argv = build_argv(SRC, unwind=4)
    assert argv[0] == "esbmc"
    assert argv[1] == str(SRC)


def test_custom_binary_is_argv0() -> None:
    argv = build_argv(SRC, unwind=4, esbmc_bin="/opt/esbmc")
    assert argv[0] == "/opt/esbmc"


def test_uses_recommended_unwind_discipline() -> None:
    # ADR/CLAUDE.md: prefer `--unwind N --no-unwinding-assertions`.
    argv = build_argv(SRC, unwind=8)
    assert _value_after(argv, "--unwind") == "8"
    assert "--no-unwinding-assertions" in argv


def test_unwinding_assertions_can_be_turned_on() -> None:
    # The S2 synthesizer needs assertions ON so an under-unwound loop is a
    # distinct unwinding-assertion failure, not a fake proof.
    argv = build_argv(SRC, unwind=8, no_unwinding_assertions=False)
    assert "--no-unwinding-assertions" not in argv
    assert _value_after(argv, "--unwind") == "8"


def test_extra_flags_forwarded_verbatim() -> None:
    argv = build_argv(
        SRC, unwind=1, extra_flags=("--overflow-check", "--memory-leak-check")
    )
    assert "--overflow-check" in argv
    assert "--memory-leak-check" in argv
    # forwarded in the caller's order, adjacent to each other
    i = argv.index("--overflow-check")
    assert argv[i + 1] == "--memory-leak-check"


def test_function_becomes_a_flag_pair_when_given() -> None:
    argv = build_argv(SRC, unwind=1, function="drfrom_bytes")
    assert _value_after(argv, "--function") == "drfrom_bytes"


def test_no_function_flag_when_omitted() -> None:
    argv = build_argv(SRC, unwind=1)
    assert "--function" not in argv


def test_no_timeout_flag_when_none() -> None:
    argv = build_argv(SRC, unwind=1, timeout_s=None)
    assert "--timeout" not in argv


def test_whole_second_timeout_encoded_verbatim() -> None:
    argv = build_argv(SRC, unwind=1, timeout_s=30.0)
    assert _value_after(argv, "--timeout") == "30s"


def test_fractional_timeout_rounds_up() -> None:
    # 2.9s of budget must not silently truncate to 2s.
    argv = build_argv(SRC, unwind=1, timeout_s=2.9)
    assert _value_after(argv, "--timeout") == "3s"


def test_subsecond_timeout_does_not_collapse_to_zero() -> None:
    # The bug this seam fixes: int(0.5) == 0, so the old encoding produced
    # `--timeout 0s`, which esbmc reads as *no* timeout — a semantic flip that
    # turns a 0.5s budget into an unbounded run.
    argv = build_argv(SRC, unwind=1, timeout_s=0.5)
    assert _value_after(argv, "--timeout") == "1s"
    assert "0s" not in argv


def test_positive_budget_is_never_esbmc_unbounded_zero() -> None:
    # For any non-None budget, esbmc must receive a bounded, positive timeout.
    for budget in (0.1, 0.5, 0.999):
        argv = build_argv(SRC, unwind=1, timeout_s=budget)
        assert _value_after(argv, "--timeout") == "1s"


def test_returns_tuple() -> None:
    argv = build_argv(SRC, unwind=1)
    assert isinstance(argv, tuple)

"""Unit tests for the shared verdict presentation — no esbmc, just projection.

`render_result` is the human-text projection of an `EsbmcResult` (the sibling of
`core.result_to_payload`'s JSON projection) and `EXIT_CODES` is the one
verdict->exit-status contract. Both are owned by `forseti.esbmc` and shared by
*both* CLIs (`forseti-esbmc` and `forseti verify`), so they are pinned here once,
at the layer that owns them, rather than re-derived per front-end.
"""

from pathlib import Path

from forseti.esbmc import (
    EXIT_CODES,
    Error,
    RunMeta,
    Unknown,
    UnknownReason,
    Verdict,
    Verified,
    Violated,
    render_result,
)

_ARGV: tuple[str, ...] = ("esbmc", "f.c", "--unwind", "2", "--no-unwinding-assertions")


def _meta(version: str = "8.3.0") -> RunMeta:
    return RunMeta(
        esbmc_version=version,
        argv=_ARGV,
        exit_code=0,
        duration_s=0.5,
        stdout="",
        stderr="",
    )


def test_verified_renders_header_only() -> None:
    text = render_result(Verified(_meta()), Path("f.c"), 2)
    # A VERIFIED is only "verified up to k": header carries source, k, version.
    assert text == "VERIFIED  (f.c, k=2, esbmc 8.3.0)"


def test_violated_appends_raw_counterexample_after_blank_line() -> None:
    text = render_result(Violated(_meta(), "TRACE-TEXT"), Path("f.c"), 1)
    # header, a blank line, then the lossless trace the agent fixes against.
    assert text == "VIOLATED  (f.c, k=1, esbmc 8.3.0)\n\nTRACE-TEXT"


def test_unknown_appends_reason() -> None:
    text = render_result(Unknown(_meta(), UnknownReason.TIMEOUT), Path("f.c"), 1)
    assert text == "UNKNOWN  (f.c, k=1, esbmc 8.3.0)\nreason: timeout"


def test_error_appends_message() -> None:
    text = render_result(Error(_meta(), "bad binary"), Path("f.c"), 1)
    assert text == "ERROR  (f.c, k=1, esbmc 8.3.0)\nerror: bad binary"


def test_missing_version_renders_question_mark() -> None:
    text = render_result(Verified(_meta(version="")), Path("f.c"), 1)
    assert "esbmc ?" in text


def test_exit_codes_are_the_verdict_contract() -> None:
    # UNKNOWN is deliberately non-zero: an inconclusive run is never a silent pass.
    assert EXIT_CODES == {
        Verdict.VERIFIED: 0,
        Verdict.VIOLATED: 1,
        Verdict.UNKNOWN: 2,
        Verdict.ERROR: 3,
    }


def test_core_reexports_the_same_exit_code_table() -> None:
    # core re-exports the single table rather than defining its own copy, so the
    # two CLIs can never drift to different codes for the same verdict.
    from forseti.core import EXIT_CODES as core_exit_codes

    assert core_exit_codes is EXIT_CODES

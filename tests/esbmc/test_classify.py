"""Behavioural tests for the pure verdict classifier.

Each test feeds `classify` a `RunMeta` built from real ESBMC 8.3.0 output and
asserts the verdict. No subprocess, no esbmc binary required.
"""

from forseti.esbmc.result import (
    Error,
    RunMeta,
    Unknown,
    UnknownReason,
    Verdict,
    Verified,
    Violated,
)
from forseti.esbmc.runner import classify


def meta(stdout: str = "", stderr: str = "", exit_code: int = 0) -> RunMeta:
    return RunMeta(
        esbmc_version="8.3.0",
        argv=("esbmc", "f.c", "--unwind", "8", "--no-unwinding-assertions"),
        exit_code=exit_code,
        duration_s=0.0,
        stdout=stdout,
        stderr=stderr,
    )


VERIFIED_OUT = """\
Solving with solver Bitwuzla 0.9.0
Runtime decision procedure: 0.000s
BMC program time: 0.002s

VERIFICATION SUCCESSFUL
ESBMC version 8.3.0 64-bit x86_64 linux
"""


def test_verification_successful_is_verified() -> None:
    result = classify(meta(stdout=VERIFIED_OUT, exit_code=0))
    assert isinstance(result, Verified)
    assert result.verdict is Verdict.VERIFIED


VIOLATED_OUT = """\
Building error trace

[Counterexample]


State 1 file violated_assert.c line 1 column 13 function main thread 0
----------------------------------------------------
  x = -1 (11111111 11111111 11111111 11111111)

State 2 file violated_assert.c line 1 column 20 function main thread 0
----------------------------------------------------
Violated property:
  file violated_assert.c line 1 column 20 function main
  x must be five
  x == 5


VERIFICATION FAILED
ESBMC version 8.3.0 64-bit x86_64 linux
"""


def test_verification_failed_is_violated_with_counterexample() -> None:
    result = classify(meta(stdout=VIOLATED_OUT, exit_code=1))
    assert isinstance(result, Violated)
    assert result.verdict is Verdict.VIOLATED
    assert "[Counterexample]" in result.raw_counterexample
    assert "Violated property:" in result.raw_counterexample
    assert "x must be five" in result.raw_counterexample
    # the span stops before the terminal banner
    assert "VERIFICATION FAILED" not in result.raw_counterexample


def test_violated_carries_parsed_counterexample_model() -> None:
    # classify enriches Violated with the typed model parsed from the raw trace,
    # without disturbing the raw_counterexample fallback.
    result = classify(meta(stdout=VIOLATED_OUT, exit_code=1))
    assert isinstance(result, Violated)
    assert result.counterexample is not None
    assert result.counterexample.violated_property.description == "x must be five"
    assert result.counterexample.violated_property.expression == "x == 5"
    assert {a.lhs: a.value for a in result.counterexample.inputs} == {"x": "-1"}


def test_failure_banner_wins_over_success_banner() -> None:
    # Defensive: never read VERIFIED while any failure marker is present.
    both = "VERIFICATION SUCCESSFUL\n...\nVERIFICATION FAILED\n"
    result = classify(meta(stdout=both, exit_code=1))
    assert isinstance(result, Violated)


def test_timed_out_is_unknown_timeout() -> None:
    # ESBMC timeout exits 1 (same as VIOLATED) but prints no verdict banner.
    out = "Solving with solver Bitwuzla 0.9.0\nERROR: Timed out\n"
    result = classify(meta(stdout=out, exit_code=1))
    assert isinstance(result, Unknown)
    assert result.verdict is Verdict.UNKNOWN
    assert result.reason is UnknownReason.TIMEOUT


def test_out_of_memory_is_unknown_memout() -> None:
    result = classify(meta(stdout="Out of memory\n", exit_code=1))
    assert isinstance(result, Unknown)
    assert result.reason is UnknownReason.MEMOUT


def test_verification_unknown_banner_is_unknown_unclassified() -> None:
    out = "VERIFICATION UNKNOWN\nESBMC version 8.3.0 64-bit x86_64 linux\n"
    result = classify(meta(stdout=out, exit_code=0))
    assert isinstance(result, Unknown)
    assert result.reason is UnknownReason.UNCLASSIFIED


def test_parsing_error_is_error() -> None:
    out = "broken.c:1:9: error: ...\nERROR: PARSING ERROR\n"
    result = classify(meta(stdout=out, exit_code=6))
    assert isinstance(result, Error)
    assert result.verdict is Verdict.ERROR
    assert "parsing" in result.message.lower()


def test_failed_to_open_input_file_is_error() -> None:
    out = "ERROR: failed to open input file /nope.c\n"
    result = classify(meta(stdout=out, exit_code=6))
    assert isinstance(result, Error)
    assert "open" in result.message.lower()


def test_marker_in_stderr_is_classified() -> None:
    # ESBMC's stream discipline for ERROR:/Timed out is unconfirmed; search both.
    result = classify(meta(stdout="", stderr="ERROR: Timed out\n", exit_code=1))
    assert isinstance(result, Unknown)
    assert result.reason is UnknownReason.TIMEOUT


def test_unrecognised_output_is_error_never_a_verdict() -> None:
    # A crash/garbage run must not read as a pass (roadmap Risk 1).
    result = classify(meta(stdout="Segmentation fault\n", stderr="", exit_code=139))
    assert isinstance(result, Error)
    assert "unclassified" in result.message.lower()


def test_echoed_success_banner_in_diagnostic_is_not_verified() -> None:
    # A frontend diagnostic that quotes source text containing the banner must
    # not be read as a pass: SUCCESSFUL is only honoured as a standalone line.
    out = 'broken.c:2:5: error: expected ; before "VERIFICATION SUCCESSFUL"\n'
    result = classify(meta(stdout=out, exit_code=1))
    assert not isinstance(result, Verified)
    assert isinstance(result, Error)


def test_parse_error_with_echoed_success_banner_is_error() -> None:
    # A parse error wins over an inline-echoed success banner, even when the
    # success substring precedes the error markers in the stream.
    out = (
        "note: in expansion 'VERIFICATION SUCCESSFUL'\n"
        "ERROR: PARSING ERROR\n"
    )
    result = classify(meta(stdout=out, exit_code=6))
    assert isinstance(result, Error)
    assert "parsing" in result.message.lower()


def test_parse_error_with_echoed_failed_banner_is_error() -> None:
    # A parse diagnostic may echo an offending source line that is *exactly*
    # the FAILED banner; the invocation error must still win over the verdict.
    out = (
        "broken.c:3:1: error: expected identifier\n"
        "VERIFICATION FAILED\n"
        "ERROR: PARSING ERROR\n"
    )
    result = classify(meta(stdout=out, exit_code=6))
    assert isinstance(result, Error)
    assert "parsing" in result.message.lower()


def test_counterexample_not_truncated_by_echoed_failed_in_message() -> None:
    # The __ESBMC_assert message inside `Violated property:` may itself contain
    # the banner text; the trace must be sliced at the terminal banner, not the
    # earlier echo, so the property details survive in raw_counterexample.
    out = (
        "[Counterexample]\n"
        "State 1 file f.c line 3 function main thread 0\n"
        "  x = 0\n"
        "Violated property:\n"
        "  file f.c line 3 function main\n"
        "  VERIFICATION FAILED\n"  # the assert message, indented
        "  x > 0\n"
        "\n"
        "VERIFICATION FAILED\n"  # the terminal banner
        "ESBMC version 8.3.0 64-bit x86_64 linux\n"
    )
    result = classify(meta(stdout=out, exit_code=1))
    assert isinstance(result, Violated)
    assert "Violated property:" in result.raw_counterexample
    assert "x > 0" in result.raw_counterexample  # not truncated at the echo
    # the terminal banner is excluded from the slice
    assert not result.raw_counterexample.rstrip().endswith("VERIFICATION FAILED")

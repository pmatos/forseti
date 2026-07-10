"""Unit tests for the JSON projection of the sealed `EsbmcResult` union.

`result_to_dict` is the third projection of a verdict — sibling of
`render_result` (human text) and `EXIT_CODES` (process status) — and, like them,
lives in `forseti.esbmc` because it is a property of the result union, not of any
front-end. It serialises only what is intrinsic to a result (verdict, provenance,
variant-specific evidence); a caller adds its own framing (a CLI its
source/unwind, the check phase its settled k).

`structured_cex` selects a VIOLATED's counterexample shape: the machine-facing
grading shape (raw text plus the typed counterexample) or the agent-facing CLI
shape (a single raw-trace field), mirroring `render_result`'s raw trace.
"""

import json

from forseti.esbmc import (
    Counterexample,
    Error,
    RunMeta,
    SourceLoc,
    Unknown,
    UnknownReason,
    Verified,
    Violated,
    ViolatedProperty,
    result_to_dict,
)

_ARGV: tuple[str, ...] = ("esbmc", "f.c", "--unwind", "2", "--no-unwinding-assertions")


def _meta() -> RunMeta:
    return RunMeta(
        esbmc_version="8.3.0",
        argv=_ARGV,
        exit_code=0,
        duration_s=0.5,
        stdout="",
        stderr="",
    )


def _cex() -> Counterexample:
    loc = SourceLoc("f.c", 3, 10, "f")
    return Counterexample(
        steps=(),
        violated_property=ViolatedProperty(loc, "assertion", "result >= 0", ()),
    )


def test_verified_is_intrinsic_provenance_only() -> None:
    payload = result_to_dict(Verified(_meta()))
    assert payload == {
        "verdict": "verified",
        "esbmc_version": "8.3.0",
        "argv": list(_ARGV),
        "duration_s": 0.5,
    }
    # A VERIFIED adds no variant-specific evidence, and no caller framing leaks in.
    assert "counterexample" not in payload
    assert "reason" not in payload
    assert "message" not in payload
    assert "source" not in payload
    assert "k" not in payload
    json.dumps(payload)


def test_argv_is_a_json_list_not_a_tuple() -> None:
    payload = result_to_dict(Verified(_meta()))
    assert isinstance(payload["argv"], list)


def test_violated_raw_only_when_not_structured() -> None:
    payload = result_to_dict(
        Violated(_meta(), "TRACE-TEXT", _cex()), structured_cex=False
    )
    assert payload["verdict"] == "violated"
    # The agent-facing shape: one field carrying the raw trace, mirroring render_result.
    assert payload["counterexample"] == "TRACE-TEXT"
    assert "raw_counterexample" not in payload
    json.dumps(payload)


def test_violated_structured_splits_raw_and_typed_cex() -> None:
    payload = result_to_dict(
        Violated(_meta(), "TRACE-TEXT", _cex()), structured_cex=True
    )
    assert payload["verdict"] == "violated"
    assert payload["raw_counterexample"] == "TRACE-TEXT"
    typed = payload["counterexample"]
    assert typed == _cex().to_dict()
    # concrete spot-check so the assertion can disagree with a broken projection
    assert isinstance(typed, dict)
    assert typed["violated_property"]["description"] == "assertion"
    json.dumps(payload)


def test_violated_structured_typed_is_none_when_parsing_failed() -> None:
    # A parse failure never downgrades the VIOLATED verdict; the raw text survives.
    payload = result_to_dict(Violated(_meta(), "TRACE-TEXT", None), structured_cex=True)
    assert payload["raw_counterexample"] == "TRACE-TEXT"
    assert payload["counterexample"] is None
    json.dumps(payload)


def test_structured_cex_is_the_default() -> None:
    payload = result_to_dict(Violated(_meta(), "TRACE-TEXT", _cex()))
    assert payload["raw_counterexample"] == "TRACE-TEXT"
    assert isinstance(payload["counterexample"], dict)


def test_unknown_carries_reason() -> None:
    payload = result_to_dict(Unknown(_meta(), UnknownReason.TIMEOUT))
    assert payload["verdict"] == "unknown"
    assert payload["reason"] == "timeout"
    assert "counterexample" not in payload
    json.dumps(payload)


def test_error_carries_message() -> None:
    payload = result_to_dict(Error(_meta(), "bad binary"))
    assert payload["verdict"] == "error"
    assert payload["message"] == "bad binary"
    json.dumps(payload)

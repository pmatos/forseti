"""Unit tests for the Core verdict payload — no esbmc, just serialisation.

`result_to_payload` is the wire shape the `forseti verify --json` CLI returns
(and the forthcoming MCP `verify` tool will reuse), so it must render every
verdict variant and stay `json.dumps`-able.
"""

import json
from pathlib import Path

from forseti.core import result_to_payload
from forseti.esbmc import (
    Error,
    RunMeta,
    Unknown,
    UnknownReason,
    Verified,
    Violated,
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


def test_verified_payload_is_minimal_and_serialisable() -> None:
    payload = result_to_payload(Verified(_meta()), Path("f.c"), 2)
    assert payload["verdict"] == "verified"
    assert payload["source"] == "f.c"
    assert payload["unwind"] == 2
    assert payload["esbmc_version"] == "8.3.0"
    assert payload["argv"] == list(_ARGV)
    # A VERIFIED adds no variant-specific evidence.
    assert "counterexample" not in payload
    assert "reason" not in payload
    assert "message" not in payload
    json.dumps(payload)


def test_violated_payload_carries_counterexample() -> None:
    payload = result_to_payload(Violated(_meta(), "TRACE-TEXT"), Path("f.c"), 1)
    assert payload["verdict"] == "violated"
    assert payload["counterexample"] == "TRACE-TEXT"
    json.dumps(payload)


def test_unknown_payload_carries_reason() -> None:
    payload = result_to_payload(Unknown(_meta(), UnknownReason.TIMEOUT), Path("f.c"), 1)
    assert payload["verdict"] == "unknown"
    assert payload["reason"] == "timeout"
    json.dumps(payload)


def test_error_payload_carries_message() -> None:
    payload = result_to_payload(Error(_meta(), "bad binary"), Path("f.c"), 1)
    assert payload["verdict"] == "error"
    assert payload["message"] == "bad binary"
    json.dumps(payload)

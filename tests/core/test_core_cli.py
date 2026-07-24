"""End-to-end tests for the unified ``forseti verify`` CLI.

Runs the real esbmc binary on the worked C examples, so it is skipped when
esbmc is not on PATH (like the esbmc integration suite). Pins Core's verdict
exit-code contract and the ``--json`` payload.
"""

import json
import shutil
from pathlib import Path

import pytest

from forseti.core.cli import main

pytestmark = pytest.mark.skipif(
    shutil.which("esbmc") is None, reason="esbmc binary not on PATH"
)

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
FIXTURES = Path(__file__).resolve().parents[1] / "esbmc" / "fixtures"


def test_verify_verified_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["verify", str(EXAMPLES / "abs_fixed.c"), "-k", "1"])
    assert code == 0
    assert "VERIFIED" in capsys.readouterr().out


def test_verify_violated_exits_one(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["verify", str(EXAMPLES / "abs.c"), "-k", "1"])
    assert code == 1
    assert "VIOLATED" in capsys.readouterr().out


def test_verify_error_exits_three() -> None:
    code = main(["verify", str(FIXTURES / "broken.c"), "-k", "1"])
    assert code == 3


def test_verify_json_payload(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["verify", str(EXAMPLES / "abs.c"), "-k", "1", "--json"])
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "violated"
    assert payload["unwind"] == 1
    assert payload["source"].endswith("abs.c")
    assert payload["counterexample"]  # non-empty trace for the agent to fix


# --- forseti synth (memory-precondition gate, RFC-0003 S2) ------------------


def test_synth_assumed_verified_exits_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(["synth", str(EXAMPLES / "sha1.c"), "--function", "sha1_init"])
    assert code == 0
    out = capsys.readouterr().out
    assert "assuming valid caller pointers" in out


def test_synth_violated_exits_one(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["synth", str(EXAMPLES / "sha1_bug.c"), "--function", "sha1_update"])
    assert code == 1
    assert "VIOLATED" in capsys.readouterr().out


def test_synth_emit_only_prints_sidecar(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(
        ["synth", str(EXAMPLES / "sha1.c"), "--function", "sha1_update", "--emit-only"]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert '#include "' in out and "sha1.c" in out
    assert "malloc((size_t)len)" in out
    assert "sha1_update(ctx, data, len);" in out


def test_synth_json_payload(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(
        ["synth", str(EXAMPLES / "sha1_bug.c"), "--function", "sha1_update", "--json"]
    )
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["assessment"] == "violated"
    assert payload["assumed"] is False
    assert {p["name"] for p in payload["params"]} == {"ctx", "data", "len"}


def test_synth_needs_contract_exits_five(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A void* pointee has no size → L0 cannot synthesise → NEEDS_CONTRACT (5).
    src = tmp_path / "opaque.c"
    src.write_text("void g(void *p) { (void)p; }\n")
    code = main(["synth", str(src), "--function", "g"])
    assert code == 5
    assert "NEEDS_CONTRACT" in capsys.readouterr().out

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

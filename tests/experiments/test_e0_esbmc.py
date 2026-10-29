"""The E0 runner against the real ESBMC, on one kernel; the proposer stays fake.

Skipped when `esbmc` is not on PATH. `examples/utf8_decode.c` settles in a couple
of seconds on both versions, so the test is about the plumbing (real harness, real
verdicts, real rows), not about what a model would propose.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from forseti.experiments.e0 import (
    PROPERTY_EVENT,
    Manifest,
    UnitSpec,
    read_rows,
    run_grid,
    summary_from_run_dir,
)

pytestmark = pytest.mark.skipif(
    shutil.which("esbmc") is None, reason="esbmc binary not on PATH"
)

REPO = Path(__file__).parents[2]


class FakeClient:
    provider = "fake"
    model = "fake-model"

    def complete(self, prompt: str) -> str:
        candidate = {
            "expression": "result >= 0",
            "domain": [],
            "referenced_params": [],
            "rationale": "the decoder returns a count or a negative error",
        }
        return json.dumps({"candidates": [candidate]})


def test_one_kernel_runs_end_to_end_and_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(REPO)
    manifest = Manifest(
        name="smoke",
        units=(
            UnitSpec(
                "examples/utf8_decode.c",
                "utf8_decode",
                twin="examples/utf8_decode_bug.c",
                max_len=4,
            ),
        ),
        recipes=("blind",),
        samples=1,
        esbmc_timeout_s=60,
    )
    run_dir = tmp_path / "run"

    summary = run_grid(manifest, run_dir, client=FakeClient())

    assert summary.complete
    rows = {
        row["target"]: row
        for row in read_rows(run_dir)
        if row["type"] == PROPERTY_EVENT
    }
    assert rows["clean"]["outcome"] == "held"
    assert rows["clean"]["rendered"] is True and rows["clean"]["k"] is not None
    assert rows["twin"]["outcome"] == "violated"
    assert rows["clean"]["esbmc_time_s"] > 0
    assert rows["clean"]["esbmc_version"].startswith("8.")
    recipe = summary_from_run_dir(run_dir).recipes[0]
    assert recipe.targets["clean"].count("held") == 1
    assert recipe.targets["twin"].count("violated") == 1

    before = len(read_rows(run_dir))
    run_grid(manifest, run_dir, client=FakeClient())
    assert len(read_rows(run_dir)) == before

"""End-to-end (real ESBMC): the representation-invariant table, through the CLI (#387).

Reproduces the issue's evidence table on `examples/ring_buffer.c` and its staged-defect
twin `ring_buffer_bug.c` by driving `forseti` itself (`main`), not Core functions:
`submit-property` stores the invariant, `check --type` runs the inductive check
(assume at entry, call, assert at exit, with the non-vacuity probe), and after
`invariant-status accepted` `synth` runs the built-in safety checks (S0) under it.
Every verdict says the invariant is *assumed*. Skipped when esbmc is not on PATH.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from forseti.core.cli import main
from forseti.properties import INVARIANT_ASSUMPTION_NOTE, REP_INVARIANT_LABEL

pytestmark = pytest.mark.skipif(
    shutil.which("esbmc") is None, reason="esbmc binary not on PATH"
)

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
CLEAN = EXAMPLES / "ring_buffer.c"
BUG = EXAMPLES / "ring_buffer_bug.c"
INVARIANT = (
    "self->count <= CAP && self->head < CAP && self->tail < CAP "
    "&& (self->head + self->count) % CAP == self->tail"
)
VACUOUS = "self->count > self->count"


def _run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, Any]]:
    capsys.readouterr()
    code = main([*argv, "--json"])
    return code, json.loads(capsys.readouterr().out)


def _submit(
    capsys: pytest.CaptureFixture[str], source: Path, root: Path, expression: str
) -> str:
    code, payload = _run(
        capsys,
        "submit-property",
        str(source),
        "--kind",
        "rep-invariant",
        "--type",
        "ring_t",
        "--expression",
        expression,
        "--provider",
        "hand",
        "--model",
        "human",
        "--store-root",
        str(root),
    )
    assert code == 0
    (accepted,) = payload["accepted"]
    return str(accepted["property_id"])


def _inductive(
    capsys: pytest.CaptureFixture[str], source: Path, root: Path, function: str
) -> tuple[int, dict[str, Any]]:
    code, payload = _run(
        capsys,
        "check",
        str(source),
        "--function",
        function,
        "--type",
        "ring_t",
        "--store-root",
        str(root),
    )
    (verdict,) = payload["verdicts"]
    assert verdict["assumption"] == INVARIANT_ASSUMPTION_NOTE
    return code, verdict


def _synth(
    capsys: pytest.CaptureFixture[str], source: Path, root: Path, *extra: str
) -> tuple[int, dict[str, Any]]:
    return _run(
        capsys,
        "synth",
        str(source),
        "--function",
        "rb_push",
        "--max-len",
        "4",
        "--store-root",
        str(root),
        *extra,
    )


def _accept(
    capsys: pytest.CaptureFixture[str], source: Path, root: Path, pid: str
) -> None:
    code, payload = _run(
        capsys,
        "invariant-status",
        str(source),
        "--type",
        "ring_t",
        "--property-id",
        pid,
        "accepted",
        "--store-root",
        str(root),
    )
    assert code == 0
    assert payload["status"] == "accepted"


@pytest.fixture(autouse=True)
def _trace_in_tmp_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)


@pytest.mark.parametrize("source", [CLEAN, BUG], ids=["clean", "bug"])
def test_s0_is_violated_without_a_context_and_verified_under_the_accepted_invariant(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], source: Path
) -> None:
    root = tmp_path / ".forseti"
    property_id = _submit(capsys, source, root, INVARIANT)

    # a candidate is not a context: S0 still reports the state no caller can build
    code, payload = _synth(capsys, source, root)
    assert (code, payload["assessment"]) == (1, "violated")
    assert payload["assumed_invariants"] == []

    _accept(capsys, source, root, property_id)

    code, payload = _synth(capsys, source, root)
    assert (code, payload["assessment"]) == (0, "assumed_verified")
    assert payload["assumed"] is True
    (assumed,) = payload["assumed_invariants"]
    assert assumed["type"] == "ring_t"
    assert assumed["expressions"] == [INVARIANT]
    assert assumed["params"] == ["r"]

    code, payload = _synth(capsys, source, root, "--no-invariants")
    assert (code, payload["assessment"]) == (1, "violated")


def test_clean_ring_buffer_holds_the_inductive_check_for_every_function(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / ".forseti"
    _submit(capsys, CLEAN, root, INVARIANT)
    for function in ("rb_push", "rb_pop", "rb_init"):
        code, verdict = _inductive(capsys, CLEAN, root, function)
        assert (code, verdict["outcome"]) == (0, "held"), function
        assert verdict["probe"] == "reached"


def test_the_bug_is_found_only_by_the_inductive_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / ".forseti"
    _submit(capsys, BUG, root, INVARIANT)

    code, verdict = _inductive(capsys, BUG, root, "rb_push")
    assert (code, verdict["outcome"]) == (1, "violated")
    assert REP_INVARIANT_LABEL in verdict["result"]["raw_counterexample"]
    for function in ("rb_pop", "rb_init"):
        code, verdict = _inductive(capsys, BUG, root, function)
        assert (code, verdict["outcome"]) == (0, "held"), function


def test_an_accepted_invariant_is_still_checked_inductively(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / ".forseti"
    _accept(capsys, BUG, root, _submit(capsys, BUG, root, INVARIANT))
    code, verdict = _inductive(capsys, BUG, root, "rb_push")
    assert (code, verdict["outcome"]) == (1, "violated")


def test_a_vacuous_invariant_is_never_held_and_never_a_synth_pass(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / ".forseti"
    property_id = _submit(capsys, CLEAN, root, VACUOUS)

    code, verdict = _inductive(capsys, CLEAN, root, "rb_push")
    assert verdict["outcome"] == "vacuous"
    assert verdict["probe"] == "unreachable"
    assert code != 0

    _accept(capsys, CLEAN, root, property_id)
    code, payload = _synth(capsys, CLEAN, root)
    assert (code, payload["assessment"]) == (4, "vacuous")

"""End-to-end (real ESBMC): discharging the ring_t invariant at its callers (#430).

Drives `forseti invariant-discharge` on `examples/ring_buffer.c` and its staged-defect
twin. The constructing caller is the example's own `main`. Skipped when esbmc is not
on PATH.

* clean, correct invariant: `main` is checked at every call and passes, but the
  functions are externally visible, so the invariant stays *assumed* (exported);
* the same file with its functions made `static`: discharged;
* the staged defect: reported violated at `main`;
* an invariant the constructor does not establish (an over-strong one, or an
  `rb_init` that forgets a field): reported violated at `main`'s first call.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from forseti.core.cli import main

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
OVER_STRONG = "self->count >= 1"


def _discharge(
    capsys: pytest.CaptureFixture[str], source: Path, expression: str, root: Path
) -> tuple[int, dict[str, Any]]:
    capsys.readouterr()
    code = main(
        [
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
        ]
    )
    assert code == 0
    capsys.readouterr()
    code = main(
        [
            "invariant-discharge",
            str(source),
            "--type",
            "ring_t",
            "--store-root",
            str(root),
            "--json",
        ]
    )
    return code, json.loads(capsys.readouterr().out)


def _by_function(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {entry["function"]: entry for entry in payload["discharges"]}


def _static_copy(tmp_path: Path, replacements: dict[str, str] | None = None) -> Path:
    text = CLEAN.read_text()
    for name in ("void rb_init", "int rb_push", "int rb_pop"):
        text = text.replace(f"\n{name}", f"\nstatic {name}")
    for old, new in (replacements or {}).items():
        assert old in text
        text = text.replace(old, new)
    path = tmp_path / "ring_static.c"
    path.write_text(text)
    return path


def test_clean_ring_buffer_is_checked_at_main_but_stays_assumed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, payload = _discharge(capsys, CLEAN, INVARIANT, tmp_path / ".forseti")
    assert (code, payload["outcome"]) == (0, "incomplete")
    functions = _by_function(payload)
    assert set(functions) == {"rb_push", "rb_pop"}, "rb_init establishes, not assumes"
    for entry in functions.values():
        assert (entry["discharged"], entry["assumed"]) == (False, True)
        assert "externally visible" in entry["detail"]
        (caller,) = entry["callers"]
        assert (caller["caller"], caller["outcome"]) == ("main", "discharged")


def test_static_ring_buffer_is_discharged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _static_copy(tmp_path)
    code, payload = _discharge(capsys, source, INVARIANT, tmp_path / ".forseti")
    assert (code, payload["outcome"]) == (0, "discharged")
    for entry in payload["discharges"]:
        assert (entry["discharged"], entry["assumed"]) == (True, False)
        assert entry["k"] is not None


def test_the_staged_defect_is_violated_at_main(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, payload = _discharge(capsys, BUG, INVARIANT, tmp_path / ".forseti")
    assert (code, payload["outcome"]) == (1, "violated")
    for entry in payload["discharges"]:
        assert entry["status"] == "violated"
        (caller,) = entry["callers"]
        assert (caller["caller"], caller["outcome"]) == ("main", "obligation_violated")
        assert "forseti:rep-invariant-entry" in caller["counterexample"]


def test_an_invariant_the_constructor_does_not_establish_is_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, payload = _discharge(capsys, CLEAN, OVER_STRONG, tmp_path / ".forseti")
    assert (code, payload["outcome"]) == (1, "violated")
    assert {e["status"] for e in payload["discharges"]} == {"violated"}


def test_a_constructor_that_forgets_a_field_is_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _static_copy(
        tmp_path, {"r->head = r->tail = r->count = 0;": "r->head = r->tail = 0;"}
    )
    code, payload = _discharge(capsys, source, INVARIANT, tmp_path / ".forseti")
    assert (code, payload["outcome"]) == (1, "violated")

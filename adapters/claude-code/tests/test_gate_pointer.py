"""Tests for S1 (#124): pointer/array units become NEEDS_CONTRACT, not phantoms.

Function/signature enumeration is done by ``forseti list-units`` — ESBMC's own
clang frontend (#131) — not a regex, so the brittleness-class cases (typedef'd
pointers, K&R and multi-line signatures, function-like macros, a ``*`` inside a
comment) are classified correctly. The end-to-end cases below need ``esbmc`` +
``forseti`` on PATH; the wiring and error handling are covered fast with a fake
CLI so they run everywhere.

Run from the repo root with the dev venv (put its ``forseti`` first on PATH so a
broken launcher elsewhere is shadowed)::

    PATH=.venv/bin:$PATH .venv/bin/python -m pytest adapters/claude-code/tests -q
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import forseti_gate as gate
import pytest

_HAVE_ESBMC = shutil.which("esbmc") is not None and shutil.which("forseti") is not None


# --- fake `forseti list-units` CLI (no esbmc needed) -----------------------


def _fake_forseti_cmd(
    tmp_path: Path, *, stdout: str = "", stderr: str = "", exit_code: int = 0
) -> list[str]:
    """A stand-in for `resolve_forseti_cmd()` that emits canned CLI output.

    Writes a tiny Python script that ignores its argv and prints the given
    streams / exit code, so the gate's subprocess wiring is exercised for real
    (spawn, streams, returncode) without depending on esbmc.
    """
    script = tmp_path / "fake_forseti.py"
    script.write_text(
        "import sys\n"
        f"sys.stdout.write({stdout!r})\n"
        f"sys.stderr.write({stderr!r})\n"
        f"sys.exit({exit_code})\n"
    )
    return [sys.executable, str(script)]


def _units_payload(*units: tuple[str, bool]) -> str:
    """A `forseti list-units --json` payload for the given (name, takes_pointer)."""
    return json.dumps(
        {
            "source": "x.c",
            "units": [
                {"function": name, "takes_pointer": tp, "params": []}
                for name, tp in units
            ],
        }
    )


def test_extract_function_defs_parses_cli_json(tmp_path: Path, monkeypatch) -> None:
    payload = _units_payload(("a", False), ("b", True))
    monkeypatch.setattr(
        gate, "resolve_forseti_cmd", lambda: _fake_forseti_cmd(tmp_path, stdout=payload)
    )
    defs = gate.extract_function_defs(str(tmp_path / "x.c"), project_dir=str(tmp_path))
    assert [(d.name, d.takes_pointer) for d in defs] == [("a", False), ("b", True)]


def test_extract_functions_returns_names(tmp_path: Path, monkeypatch) -> None:
    payload = _units_payload(("a", True), ("b", False))
    monkeypatch.setattr(
        gate, "resolve_forseti_cmd", lambda: _fake_forseti_cmd(tmp_path, stdout=payload)
    )
    assert gate.extract_functions(str(tmp_path / "x.c"), project_dir=str(tmp_path)) == [
        "a",
        "b",
    ]


def test_extract_function_defs_raises_on_nonzero_exit(
    tmp_path: Path, monkeypatch
) -> None:
    # A failed parse (nonzero exit) must raise, never be read as "no units" — the
    # latter would let the gate silently skip a unit.
    monkeypatch.setattr(
        gate,
        "resolve_forseti_cmd",
        lambda: _fake_forseti_cmd(tmp_path, stderr="ERROR: parse failed", exit_code=1),
    )
    with pytest.raises(gate.UnitsUnavailable, match="parse failed"):
        gate.extract_function_defs(str(tmp_path / "x.c"), project_dir=str(tmp_path))


def test_extract_function_defs_raises_on_bad_json(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        gate,
        "resolve_forseti_cmd",
        lambda: _fake_forseti_cmd(tmp_path, stdout="not json at all"),
    )
    with pytest.raises(gate.UnitsUnavailable):
        gate.extract_function_defs(str(tmp_path / "x.c"), project_dir=str(tmp_path))


def test_extract_function_defs_raises_on_malformed_payload(
    tmp_path: Path, monkeypatch
) -> None:
    # Valid JSON, wrong shape (a units entry missing `function`) must degrade to a
    # blocking UnitsUnavailable, never crash the hook with KeyError/TypeError.
    bad = '{"units": [{"takes_pointer": true}]}'
    monkeypatch.setattr(
        gate, "resolve_forseti_cmd", lambda: _fake_forseti_cmd(tmp_path, stdout=bad)
    )
    with pytest.raises(gate.UnitsUnavailable):
        gate.extract_function_defs(str(tmp_path / "x.c"), project_dir=str(tmp_path))


@pytest.mark.parametrize(
    "stdout",
    [
        '{"source": "x.c"}',  # no `units` key (older/incompatible build)
        '{"units": null}',  # present but null
        '{"units": "f"}',  # present but not a list
        "[]",  # a JSON array, not an object
        '"ok"',  # a JSON scalar
    ],
)
def test_extract_function_defs_raises_when_units_absent_or_not_a_list(
    tmp_path: Path, monkeypatch, stdout: str
) -> None:
    # An exit-0 payload without a list-valued `units` (e.g. an older `forseti`
    # build) must NOT default to "no units" — that would let an edited `.c` pass
    # unverified. It has to surface as a blocking UnitsUnavailable.
    monkeypatch.setattr(
        gate, "resolve_forseti_cmd", lambda: _fake_forseti_cmd(tmp_path, stdout=stdout)
    )
    with pytest.raises(gate.UnitsUnavailable):
        gate.extract_function_defs(str(tmp_path / "x.c"), project_dir=str(tmp_path))


def test_extract_function_defs_empty_units_is_a_clean_pass(
    tmp_path: Path, monkeypatch
) -> None:
    # An *empty* list is a legitimate "file defines no functions" pass — only an
    # absent/non-list `units` blocks. Guards against over-rejecting the empty case.
    monkeypatch.setattr(
        gate,
        "resolve_forseti_cmd",
        lambda: _fake_forseti_cmd(tmp_path, stdout='{"source": "x.c", "units": []}'),
    )
    assert (
        gate.extract_function_defs(str(tmp_path / "x.c"), project_dir=str(tmp_path))
        == []
    )


def test_units_absent_payload_records_blocking_error(
    tmp_path: Path, monkeypatch
) -> None:
    # End to end: an exit-0 payload with no `units` key must make verify_and_record
    # persist a blocking `error` verdict, not silently pass the edited file.
    monkeypatch.setattr(
        gate,
        "resolve_forseti_cmd",
        lambda: _fake_forseti_cmd(tmp_path, stdout='{"source": "x.c"}'),
    )
    src = tmp_path / "x.c"
    src.write_text("int f(void) { return 0; }\n")
    verdicts = gate.verify_and_record(str(src), project_dir=str(tmp_path))
    assert len(verdicts) == 1
    assert verdicts[0].verdict == "error"
    state = gate.load_state(str(tmp_path))
    assert gate.blocking_units(state)  # non-empty → the Stop-gate blocks


def test_header_edit_short_circuits_to_clean_pass(tmp_path: Path, monkeypatch) -> None:
    # ESBMC can't parse a .h standalone, so a header is out of gate scope: enumerate
    # nothing (clean pass) WITHOUT shelling out — the fake CLI here would fail if
    # called, proving the .c allowlist short-circuits before the subprocess.
    monkeypatch.setattr(
        gate,
        "resolve_forseti_cmd",
        lambda: _fake_forseti_cmd(tmp_path, stderr="ERROR: must not run", exit_code=1),
    )
    hdr = tmp_path / "api.h"
    hdr.write_text("void foo(int *p);\n")
    assert gate.extract_function_defs(str(hdr), project_dir=str(tmp_path)) == []
    verdicts = gate.verify_and_record(str(hdr), project_dir=str(tmp_path))
    assert verdicts == []  # no units, no block, no error verdict
    state = gate.load_state(str(tmp_path))
    assert gate.blocking_units(state) == []


def test_enumeration_failure_records_blocking_error(
    tmp_path: Path, monkeypatch
) -> None:
    # If units can't be enumerated, verify_and_record must persist a blocking
    # `error` verdict — an edited-but-unparseable file cannot pass silently.
    monkeypatch.setattr(
        gate,
        "resolve_forseti_cmd",
        lambda: _fake_forseti_cmd(tmp_path, stderr="ERROR: boom", exit_code=1),
    )
    src = tmp_path / "x.c"
    src.write_text("int f(void) { return 0; }\n")
    verdicts = gate.verify_and_record(str(src), project_dir=str(tmp_path))
    assert len(verdicts) == 1
    assert verdicts[0].verdict == "error"
    state = gate.load_state(str(tmp_path))
    assert gate.blocking_units(state)  # non-empty → the Stop-gate blocks
    assert gate.needs_contract_units(state) == []


def test_pointer_unit_recorded_needs_contract(tmp_path: Path, monkeypatch) -> None:
    # A pointer-taking unit (from the CLI) is classified NEEDS_CONTRACT without
    # ever shelling out to esbmc verify.
    monkeypatch.setattr(
        gate,
        "resolve_forseti_cmd",
        lambda: _fake_forseti_cmd(tmp_path, stdout=_units_payload(("f", True))),
    )
    src = tmp_path / "buf.c"
    src.write_text("int f(int *p) { return *p; }\n")

    verdicts = gate.verify_and_record(str(src), project_dir=str(tmp_path))

    assert len(verdicts) == 1
    v = verdicts[0]
    assert v.verdict == gate.NEEDS_CONTRACT
    assert v.counterexample is None  # no ESBMC run → no counterexample
    assert v.argv is None  # never shelled out to esbmc verify
    assert not v.passed
    # persisted, and the Stop-gate would treat it as non-blocking
    state = gate.load_state(str(tmp_path))
    assert gate.blocking_units(state) == []
    assert len(gate.needs_contract_units(state)) == 1


# --- authoritative signature detection (needs esbmc) -----------------------


@pytest.mark.skipif(not _HAVE_ESBMC, reason="needs esbmc + forseti on PATH")
@pytest.mark.parametrize(
    "src, name, takes_pointer",
    [
        ("int f(int *p) { return *p; }", "f", True),  # pointer param
        ("int f(int x) { return x; }", "f", False),  # scalar param
        ("char *f(int x) { return 0; }", "f", False),  # pointer RETURN, scalar param
        ("void f(int p[10]) { }", "f", True),  # array param decays to pointer
        ("void f(void) { }", "f", False),  # no params
        ("unsigned f(const char *k, unsigned long n){return n;}", "f", True),
        ("void f(int (*cb)(void)) { }", "f", True),  # function-pointer param
        ("int neg(int x /* input */) { return -x; }", "neg", False),  # #130 comment
        ("int f(int *p /* ptr */) { return *p; }", "f", True),  # real ptr + comment
        ("int g(int a, /* a */ int b) { return a + b; }", "g", False),  # mid-list
        # --- brittleness class (#131): a regex fundamentally cannot do these ---
        ("typedef char* str_t;\nvoid f(str_t s) { (void)s; }", "f", True),  # typedef
        ("int f(\n  int *p\n) {\n  return *p;\n}", "f", True),  # multi-line signature
        ("long\nf(long n) { return n; }", "f", False),  # return type on its own line
        (
            "int f(x, p)\n  int x;\n  int *p;\n{\n  return x + *p;\n}",
            "f",
            True,
        ),  # K&R-style definition
    ],
)
def test_pointer_param_detection(
    tmp_path: Path, src: str, name: str, takes_pointer: bool
) -> None:
    source = tmp_path / "u.c"
    source.write_text(src + "\n")
    defs = gate.extract_function_defs(str(source), project_dir=str(tmp_path))
    assert len(defs) == 1
    assert defs[0].name == name
    assert defs[0].takes_pointer is takes_pointer


@pytest.mark.skipif(not _HAVE_ESBMC, reason="needs esbmc + forseti on PATH")
def test_function_like_macro_not_enumerated(tmp_path: Path) -> None:
    # A function-like macro is not a definition — the authoritative parse ignores
    # it, where the regex could false-match its `NAME(args)` shape.
    src = tmp_path / "m.c"
    src.write_text("#define SQ(a) ((a) * (a))\nint use(int x) { return SQ(x); }\n")
    names = [
        d.name for d in gate.extract_function_defs(str(src), project_dir=str(tmp_path))
    ]
    assert names == ["use"]


# --- blocking vs needs_contract classification -----------------------------


def _state(*verdicts: str) -> dict:
    units = {
        f"u{i}": {"unit_id": f"u{i}", "verdict": v} for i, v in enumerate(verdicts)
    }
    return {"units": units, "stop_attempts": 0}


def test_needs_contract_is_not_blocking() -> None:
    state = _state("verified", gate.NEEDS_CONTRACT, "violated", "unknown", "error")
    blocking = {u["verdict"] for u in gate.blocking_units(state)}
    needs = {u["verdict"] for u in gate.needs_contract_units(state)}
    assert blocking == {"violated", "unknown", "error"}  # neither verified nor needs
    assert needs == {gate.NEEDS_CONTRACT}


def test_verified_and_needs_only_does_not_block() -> None:
    state = _state("verified", gate.NEEDS_CONTRACT, gate.NEEDS_CONTRACT)
    assert gate.blocking_units(state) == []
    assert len(gate.needs_contract_units(state)) == 2


# --- verify_and_record: a mixed file, end to end ---------------------------


@pytest.mark.skipif(not _HAVE_ESBMC, reason="needs esbmc + forseti on PATH")
def test_mixed_file_scalar_gated_pointer_needs_contract(tmp_path: Path) -> None:
    # scalar my_abs is genuinely VIOLATED at INT64_MIN; the pointer unit is skipped.
    src = tmp_path / "mix.c"
    src.write_text(
        "#include <stdint.h>\n"
        "int64_t my_abs(int64_t x) { return (x < 0) ? -x : x; }\n"
        "int deref(int *p) { return *p; }\n"
    )
    verdicts = {
        v.function: v.verdict
        for v in gate.verify_and_record(str(src), project_dir=str(tmp_path))
    }
    assert verdicts["deref"] == gate.NEEDS_CONTRACT
    assert verdicts["my_abs"] == "violated"  # real scalar verdict still produced

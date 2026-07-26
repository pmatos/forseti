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


def test_synth_macro_array_extent_needs_contract_not_violated(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Issue #137, the outcome that matters: this function is *correct* for the
    # extent it declares, but the macro extent needs the preprocessor. Backing it
    # with a single element made the write to `digest[1]` a phantom VIOLATED (1);
    # the honest answer is NEEDS_CONTRACT (5).
    src = tmp_path / "macro_extent.c"
    src.write_text(
        "#define DLEN 20\n"
        "void fill(unsigned char digest[DLEN]) {\n"
        "  for (unsigned i = 0; i < DLEN; i++) digest[i] = (unsigned char)i;\n}\n"
    )
    code = main(["synth", str(src), "--function", "fill"])
    assert code == 5
    assert "NEEDS_CONTRACT" in capsys.readouterr().out


# --- forseti discharge (compositional discharge, RFC-0003 S3) ---------------


def test_discharge_upgrades_a_clean_translation_unit(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(
        ["discharge", str(EXAMPLES / "frame_checksum.c"), "--function", "sum_bytes"]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "discharged" in out
    # each caller is reported by name, not folded into a bare verdict
    assert "frame_checksum(): discharged" in out
    assert "payload_checksum(): discharged" in out


def test_discharge_bad_caller_exits_one_and_names_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(
        ["discharge", str(EXAMPLES / "frame_checksum_bug.c"), "--function", "sum_bytes"]
    )
    assert code == 1
    out = capsys.readouterr().out
    assert "VIOLATED at the call site" in out
    assert "payload_checksum(): obligation_violated" in out


def test_discharge_json_payload(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(
        [
            "discharge",
            str(EXAMPLES / "frame_checksum.c"),
            "--function",
            "sum_bytes",
            "--json",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["assessment"] == "discharged_verified"
    assert payload["discharged"] is True
    assert payload["assumed"] is False
    assert {c["caller"] for c in payload["callers"]} == {
        "frame_checksum",
        "payload_checksum",
    }


def test_discharge_emit_only_prints_the_injected_copy(
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = EXAMPLES / "frame_checksum.c"
    before = source.read_text()
    code = main(["discharge", str(source), "--function", "sum_bytes", "--emit-only"])
    assert code == 0
    out = capsys.readouterr().out
    assert "forseti:obligation:sum_bytes:buf" in out
    assert source.read_text() == before  # the user's file stays pristine


def test_discharge_needs_contract_exits_five(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    src = tmp_path / "opaque.c"
    src.write_text("void g(void *p) { (void)p; }\n")
    code = main(["discharge", str(src), "--function", "g"])
    assert code == 5
    assert "NEEDS_CONTRACT" in capsys.readouterr().out


def test_discharge_emit_only_declines_an_unsynthesisable_unit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    src = tmp_path / "opaque.c"
    src.write_text("void g(void *p) { (void)p; }\n")
    code = main(["discharge", str(src), "--function", "g", "--emit-only"])
    assert code == 5
    assert capsys.readouterr().out == ""


# --- forseti list-units (authoritative unit enumeration, #131) ---------------


def _unit_source(tmp_path: Path) -> Path:
    """A TU that needs `-I` to parse at all and `-D` to define all its units."""
    inc = tmp_path / "inc"
    inc.mkdir()
    (inc / "mytypes.h").write_text("typedef unsigned char byte_t;\n")
    src = tmp_path / "u.c"
    src.write_text(
        '#include "mytypes.h"\n'
        "int scal(byte_t b) { return b; }\n"
        "void ptr(byte_t *p) { (void)p; }\n"
        "#ifdef WIDGET\n"
        "int only_with_define(int x) { return x; }\n"
        "#endif\n"
    )
    return src


def test_list_units_json_payload(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    src = tmp_path / "sig.c"
    src.write_text(
        "typedef void (*cb_t)(void);\n"
        "int scal(int x) { return x; }\n"
        "void reg(cb_t cb) { cb(); }\n"
    )
    assert main(["list-units", str(src), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["source"] == str(src)
    assert {u["function"]: u["takes_pointer"] for u in payload["units"]} == {
        "scal": False,
        "reg": True,  # the typedef'd function pointer, resolved
    }
    reg = next(u for u in payload["units"] if u["function"] == "reg")
    assert reg["params"] == [
        {
            "name": "cb",
            "type": "void (*)(void)",
            "is_pointer": True,
            "array_extent": None,
            "array_extent_unresolved": False,
            "array_static_min": False,
        }
    ]


def test_list_units_json_reports_array_shape(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The `--json` surface carries both halves of a parameter's written array shape:
    # the recovered extent, and (issue #137) whether an unreadable one was declared.
    src = tmp_path / "shapes.c"
    src.write_text(
        "#define DLEN 20\n"
        "void f(unsigned char digest[DLEN], unsigned char tag[16], "
        "unsigned char *raw, unsigned char mac[static DLEN]) {\n"
        "  (void)digest; (void)tag; (void)raw; (void)mac;\n}\n"
    )
    code = main(["list-units", str(src), "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    unit = next(u for u in payload["units"] if u["function"] == "f")
    assert [
        (
            p["name"],
            p["array_extent"],
            p["array_extent_unresolved"],
            p["array_static_min"],
        )
        for p in unit["params"]
    ] == [
        ("digest", None, True, False),
        ("tag", 16, False, False),
        ("raw", None, False, False),
        ("mac", None, True, True),
    ]


def test_list_units_human_output_marks_needs_contract(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    src = tmp_path / "sig.c"
    src.write_text("int scal(int x) { return x; }\nvoid p(char *q) { (void)q; }\n")
    assert main(["list-units", str(src)]) == 0
    lines = capsys.readouterr().out.splitlines()
    # The type is printed as clang's canonical spelling (`char *`) joined to the
    # name, so the pointer star sits away from the identifier.
    assert lines == ["scal(int x)", "p(char * q) [needs-contract]"]


def test_list_units_no_params_renders_void(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    src = tmp_path / "sig.c"
    src.write_text("int nil(void) { return 0; }\n")
    assert main(["list-units", str(src)]) == 0
    assert capsys.readouterr().out.splitlines() == ["nil(void)"]


def test_list_units_forwards_passthrough_build_flags(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The deferred half of #131: without the `--`-separated `-I` this source does
    # not parse (asserted below), and `-D` decides whether `only_with_define` is a
    # unit at all — so the passthrough is load-bearing, not cosmetic.
    src = _unit_source(tmp_path)
    code = main(
        [
            "list-units",
            str(src),
            "--json",
            "--",
            "-I",
            str(tmp_path / "inc"),
            "-DWIDGET",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert [u["function"] for u in payload["units"]] == [
        "scal",
        "ptr",
        "only_with_define",
    ]


def test_list_units_without_build_flags_fails_loudly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # An unresolvable `#include` must exit nonzero, never print an empty unit list
    # — the gate reads "no units" as a clean pass, so a silent [] here would let an
    # edited file through unverified.
    assert main(["list-units", str(_unit_source(tmp_path)), "--json"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "forseti list-units:" in captured.err


def test_list_units_honours_esbmc_bin(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # `--esbmc-bin` must actually select the binary: point it at a missing one and
    # the run fails rather than falling back to `esbmc` on PATH.
    src = tmp_path / "sig.c"
    src.write_text("int scal(int x) { return x; }\n")
    code = main(
        ["list-units", str(src), "--esbmc-bin", str(tmp_path / "no-such-esbmc")]
    )
    assert code == 1
    assert "no-such-esbmc" in capsys.readouterr().err

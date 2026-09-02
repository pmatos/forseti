"""Hermetic tests for the `forseti semantic-loop` CLI (`run_semantic_loop`, #213).

No esbmc, no network: a scripted `FakeVerify` (mirrors `test_core_check.py`'s)
stands in for ESBMC via `run_semantic_loop`'s `verify_port` injection seam,
monkeypatched onto `forseti.core.cli.run_semantic_loop` the same way
`test_core_check.py` does for `check_source`. Argument-parsing and error paths
specific to this subcommand (`--candidates-json`) are pinned here; the
end-to-end real-ESBMC + real-MCP-transport round trip lives in
`test_core_mcp_server.py::test_semantic_loop_stdio_roundtrip_matches_cli_outcome`.
"""

from __future__ import annotations

import json
from functools import partial
from pathlib import Path

import pytest

from forseti.core.cli import main
from forseti.core.loop import run_semantic_loop
from forseti.esbmc import EsbmcResult, RunMeta, Verified

ABS_SLICE = "int64_t my_abs(int64_t x) {\n    return (x < 0) ? -x : x;\n}\n"

CANNED_PROPOSE_REPLY = json.dumps(
    {"candidates": [{"expression": "result >= 0", "domain": ["x > INT64_MIN"]}]}
)


class FakeLLMClient:
    """A stand-in `LLMClient` returning a canned candidate reply (no subprocess)."""

    provider = "fake"
    model = "fake-1"

    def __init__(self, *_a: object, **_kw: object) -> None: ...

    def complete(self, prompt: str) -> str:
        return CANNED_PROPOSE_REPLY


def _meta() -> RunMeta:
    return RunMeta(
        esbmc_version="8.3.0",
        argv=("esbmc", "harness.c", "--unwind", "4"),
        exit_code=0,
        duration_s=0.0,
        stdout="",
        stderr="",
    )


class FakeVerify:
    def __init__(self, results: list[EsbmcResult]) -> None:
        self._results = list(results)

    def __call__(self, source: Path, *, unwind: int) -> EsbmcResult:
        assert self._results, "FakeVerify over-popped: script exhausted"
        return self._results.pop(0)


def _write_unit(tmp_path: Path) -> Path:
    source = tmp_path / "abs_unit.c"
    source.write_text(ABS_SLICE)
    return source


def _patch_verify(monkeypatch: pytest.MonkeyPatch, fake: FakeVerify) -> None:
    monkeypatch.setattr(
        "forseti.core.cli.run_semantic_loop",
        partial(run_semantic_loop, verify_port=fake),
    )


def test_cli_submit_mode_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _write_unit(tmp_path)
    root = tmp_path / ".forseti"
    candidates = tmp_path / "candidates.json"
    candidates.write_text(
        json.dumps([{"expression": "result >= 0", "domain": ["x > INT64_MIN"]}])
    )
    _patch_verify(monkeypatch, FakeVerify([Verified(_meta())]))

    code = main(
        [
            "semantic-loop",
            str(source),
            "--function",
            "my_abs",
            "--mode",
            "submit",
            "--candidates-json",
            str(candidates),
            "--provider",
            "codex",
            "--model",
            "gpt-5.1",
            "--store-root",
            str(root),
            "--json",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "submit"
    assert payload["outcome"] == "held"
    assert payload["ingestion"][0]["accepted"][0]["expression"] == "result >= 0"


def test_cli_check_only_mode_skips_ingestion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _write_unit(tmp_path)
    root = tmp_path / ".forseti"
    candidates = tmp_path / "candidates.json"
    candidates.write_text(json.dumps([{"expression": "result >= 0"}]))
    _patch_verify(monkeypatch, FakeVerify([Verified(_meta()), Verified(_meta())]))

    # Seed the store via "submit" first.
    code = main(
        [
            "semantic-loop",
            str(source),
            "--function",
            "my_abs",
            "--mode",
            "submit",
            "--candidates-json",
            str(candidates),
            "--provider",
            "codex",
            "--model",
            "gpt-5.1",
            "--store-root",
            str(root),
        ]
    )
    assert code == 0
    capsys.readouterr()

    code = main(
        [
            "semantic-loop",
            str(source),
            "--function",
            "my_abs",
            "--mode",
            "check-only",
            "--store-root",
            str(root),
            "--json",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ingestion"] == []
    assert payload["outcome"] == "held"


def test_cli_submit_mode_requires_candidates_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _write_unit(tmp_path)
    code = main(
        [
            "semantic-loop",
            str(source),
            "--function",
            "my_abs",
            "--mode",
            "submit",
            "--provider",
            "codex",
            "--model",
            "gpt-5.1",
        ]
    )
    assert code == 1
    assert "--candidates-json" in capsys.readouterr().err


def test_cli_check_only_rejects_candidates_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _write_unit(tmp_path)
    candidates = tmp_path / "candidates.json"
    candidates.write_text(json.dumps([{"expression": "result >= 0"}]))
    code = main(
        [
            "semantic-loop",
            str(source),
            "--function",
            "my_abs",
            "--mode",
            "check-only",
            "--candidates-json",
            str(candidates),
        ]
    )
    assert code == 1
    err = capsys.readouterr().err
    assert "--candidates-json" in err
    assert "check-only" in err


def test_cli_propose_mode_rejects_candidates_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _write_unit(tmp_path)
    candidates = tmp_path / "candidates.json"
    candidates.write_text(json.dumps([{"expression": "result >= 0"}]))
    code = main(
        [
            "semantic-loop",
            str(source),
            "--function",
            "my_abs",
            "--mode",
            "propose",
            "--candidates-json",
            str(candidates),
        ]
    )
    assert code == 1
    err = capsys.readouterr().err
    assert "--candidates-json" in err
    assert "propose" in err


def test_cli_submit_mode_all_rejected_exits_nonzero_despite_prior_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _write_unit(tmp_path)
    root = tmp_path / ".forseti"
    good = tmp_path / "good.json"
    good.write_text(
        json.dumps([{"expression": "result >= 0", "domain": ["x > INT64_MIN"]}])
    )
    # One verify per submit-mode call: the second call's check phase
    # re-verifies the still-CANDIDATE property the first call persisted.
    _patch_verify(monkeypatch, FakeVerify([Verified(_meta()), Verified(_meta())]))
    code = main(
        [
            "semantic-loop",
            str(source),
            "--function",
            "my_abs",
            "--mode",
            "submit",
            "--candidates-json",
            str(good),
            "--provider",
            "codex",
            "--model",
            "gpt-5.1",
            "--store-root",
            str(root),
        ]
    )
    assert code == 0
    capsys.readouterr()

    # A second submission where every candidate is rejected must not exit 0
    # just because the store already holds an earlier HELD property.
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps([{"expression": "bogus_ident >= 0"}]))
    code = main(
        [
            "semantic-loop",
            str(source),
            "--function",
            "my_abs",
            "--mode",
            "submit",
            "--candidates-json",
            str(bad),
            "--provider",
            "codex",
            "--model",
            "gpt-5.1",
            "--store-root",
            str(root),
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["outcome"] == "held"
    assert code == 1


def test_cli_candidates_json_domain_must_be_a_list(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _write_unit(tmp_path)
    candidates = tmp_path / "candidates.json"
    candidates.write_text(
        json.dumps([{"expression": "result >= 0", "domain": "x > 0"}])
    )
    code = main(
        [
            "semantic-loop",
            str(source),
            "--function",
            "my_abs",
            "--mode",
            "submit",
            "--candidates-json",
            str(candidates),
            "--provider",
            "codex",
            "--model",
            "gpt-5.1",
        ]
    )
    assert code == 1
    assert "list of strings" in capsys.readouterr().err


def test_cli_candidates_json_null_expression(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _write_unit(tmp_path)
    candidates = tmp_path / "candidates.json"
    candidates.write_text(json.dumps([{"expression": None}]))
    code = main(
        [
            "semantic-loop",
            str(source),
            "--function",
            "my_abs",
            "--mode",
            "submit",
            "--candidates-json",
            str(candidates),
            "--provider",
            "codex",
            "--model",
            "gpt-5.1",
        ]
    )
    assert code == 1
    assert "expression" in capsys.readouterr().err


def test_cli_candidates_json_not_a_list(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _write_unit(tmp_path)
    candidates = tmp_path / "candidates.json"
    candidates.write_text(json.dumps({"expression": "result >= 0"}))
    code = main(
        [
            "semantic-loop",
            str(source),
            "--function",
            "my_abs",
            "--mode",
            "submit",
            "--candidates-json",
            str(candidates),
            "--provider",
            "codex",
            "--model",
            "gpt-5.1",
        ]
    )
    assert code == 1
    assert "JSON array" in capsys.readouterr().err


def test_cli_candidates_json_missing_expression(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _write_unit(tmp_path)
    candidates = tmp_path / "candidates.json"
    candidates.write_text(json.dumps([{"domain": ["x > 0"]}]))
    code = main(
        [
            "semantic-loop",
            str(source),
            "--function",
            "my_abs",
            "--mode",
            "submit",
            "--candidates-json",
            str(candidates),
            "--provider",
            "codex",
            "--model",
            "gpt-5.1",
        ]
    )
    assert code == 1
    assert "expression" in capsys.readouterr().err


def test_cli_candidates_json_malformed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _write_unit(tmp_path)
    candidates = tmp_path / "candidates.json"
    candidates.write_text("not json")
    code = main(
        [
            "semantic-loop",
            str(source),
            "--function",
            "my_abs",
            "--mode",
            "submit",
            "--candidates-json",
            str(candidates),
            "--provider",
            "codex",
            "--model",
            "gpt-5.1",
        ]
    )
    assert code == 1
    assert "invalid JSON" in capsys.readouterr().err


def test_cli_violated_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from forseti.esbmc import Violated

    source = _write_unit(tmp_path)
    candidates = tmp_path / "candidates.json"
    candidates.write_text(json.dumps([{"expression": "result < 0"}]))
    _patch_verify(
        monkeypatch, FakeVerify([Violated(_meta(), "[Counterexample]\n", None)])
    )

    code = main(
        [
            "semantic-loop",
            str(source),
            "--function",
            "my_abs",
            "--mode",
            "submit",
            "--candidates-json",
            str(candidates),
            "--provider",
            "codex",
            "--model",
            "gpt-5.1",
            "--store-root",
            str(tmp_path / ".forseti"),
        ]
    )
    assert code == 1
    assert "VIOLATED" in capsys.readouterr().out


def test_cli_propose_mode_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _write_unit(tmp_path)
    root = tmp_path / ".forseti"
    monkeypatch.setattr("forseti.core.propose.ClaudeCliClient", FakeLLMClient)
    _patch_verify(monkeypatch, FakeVerify([Verified(_meta())]))

    code = main(
        [
            "semantic-loop",
            str(source),
            "--function",
            "my_abs",
            "--mode",
            "propose",
            "--store-root",
            str(root),
            "--json",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "propose"
    assert payload["outcome"] == "held"
    assert payload["ingestion"][0]["accepted"][0]["expression"] == "result >= 0"


def test_cli_explicit_unwind_ladder_is_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _write_unit(tmp_path)
    root = tmp_path / ".forseti"
    candidates = tmp_path / "candidates.json"
    candidates.write_text(
        json.dumps([{"expression": "result >= 0", "domain": ["x > INT64_MIN"]}])
    )
    _patch_verify(monkeypatch, FakeVerify([Verified(_meta())]))

    code = main(
        [
            "semantic-loop",
            str(source),
            "--function",
            "my_abs",
            "--mode",
            "submit",
            "--candidates-json",
            str(candidates),
            "--provider",
            "codex",
            "--model",
            "gpt-5.1",
            "--store-root",
            str(root),
            "--unwind-ladder",
            "",  # explicit, not the derived default -- no escalation rungs
        ]
    )
    assert code == 0


def test_cli_store_error_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _write_unit(tmp_path)
    root = tmp_path / ".forseti"
    root.mkdir()
    (root / "forseti.db").write_text("this is not a database")
    candidates = tmp_path / "candidates.json"
    candidates.write_text(json.dumps([{"expression": "result >= 0"}]))

    code = main(
        [
            "semantic-loop",
            str(source),
            "--function",
            "my_abs",
            "--mode",
            "submit",
            "--candidates-json",
            str(candidates),
            "--provider",
            "codex",
            "--model",
            "gpt-5.1",
            "--store-root",
            str(root),
        ]
    )
    assert code == 1
    assert "forseti semantic-loop:" in capsys.readouterr().err

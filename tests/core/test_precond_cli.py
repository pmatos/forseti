"""Hermetic tests for the extracted precondition CLI cluster.

``synth``/``discharge`` — the two RFC-0003 subcommands whose glue lives in
:mod:`forseti.core._precond_cli` — are driven here through ``main`` with the
precond engine monkeypatched, so nothing runs esbmc (unlike the esbmc-gated
end-to-end suite in ``test_core_cli.py``). Two things are pinned:

- the *seam*: the cluster lives in its own module and ``cli`` re-exports the two
  handlers so the dispatch contract (``test_core_cli_dispatch``) still resolves
  ``cli._run_synth``/``cli._run_discharge`` — a "remove unused import" pass that
  dropped the re-export would break dispatch silently, so the identity is a test;
- the *glue*: each ``_run_*`` branch (emit-only, unavailable, verdict, JSON)
  routes its precond call and returns the assessment's exit code.
"""

from __future__ import annotations

import argparse
import json
from types import SimpleNamespace

import pytest

from forseti.core import _precond_cli, cli
from forseti.core.cli import _build_parser, main
from forseti.esbmc import Violated
from forseti.esbmc.result import RunMeta
from forseti.precond import (
    ASSESSMENT_EXIT_CODES,
    Assessment,
    PreconditionResult,
    PreconditionUnavailable,
)


def test_precond_cli_module_exposes_the_synth_and_discharge_cluster() -> None:
    for name in (
        "_add_precondition_arguments",
        "_add_synth_parser",
        "_run_synth",
        "_add_discharge_parser",
        "_run_discharge",
    ):
        assert callable(getattr(_precond_cli, name)), name


def test_cli_reexports_the_moved_handlers_by_identity() -> None:
    # `cli` re-imports these solely so `getattr(cli, "_run_synth")` in the
    # dispatch test resolves and the parser binds the very same object. A future
    # "remove unused import" that dropped the re-export would break dispatch
    # silently — pin the identity so the guard is a failing test, not a comment.
    assert cli._run_synth is _precond_cli._run_synth
    assert cli._run_discharge is _precond_cli._run_discharge
    assert cli._add_synth_parser is _precond_cli._add_synth_parser
    assert cli._add_discharge_parser is _precond_cli._add_discharge_parser


def _subparsers(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    actions = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    assert len(actions) == 1
    return dict(actions[0].choices)


def test_synth_and_discharge_subparsers_bind_the_moved_handlers() -> None:
    choices = _subparsers(_build_parser())
    assert choices["synth"].get_default("func") is _precond_cli._run_synth
    assert choices["discharge"].get_default("func") is _precond_cli._run_discharge


# --- synth glue -------------------------------------------------------------


def test_synth_emit_only_prints_the_sidecar(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(_precond_cli, "synthesize", lambda *a, **k: "SIDECAR_C\n")
    code = main(["synth", "x.c", "--function", "foo", "--emit-only"])
    assert code == 0
    assert capsys.readouterr().out == "SIDECAR_C\n"


def test_synth_emit_only_unavailable_exits_the_assessment_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(*a: object, **k: object) -> str:
        raise PreconditionUnavailable(Assessment.NEEDS_CONTRACT, "no pointer plan")

    monkeypatch.setattr(_precond_cli, "synthesize", boom)
    code = main(["synth", "x.c", "--function", "foo", "--emit-only"])
    assert code == ASSESSMENT_EXIT_CODES[Assessment.NEEDS_CONTRACT]
    assert "forseti synth: no pointer plan" in capsys.readouterr().err


def test_synth_prints_the_label_and_returns_the_assessment_exit_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    result = PreconditionResult(
        function="foo",
        assessment=Assessment.ASSUMED_VERIFIED,
        detail="",
        settled_k=1,
        max_len=8,
    )
    monkeypatch.setattr(_precond_cli, "verify_precondition", lambda *a, **k: result)
    code = main(["synth", "x.c", "--function", "foo"])
    assert code == 0
    out = capsys.readouterr().out
    assert "x.c::foo:" in out
    assert "assuming valid caller pointers" in out


def test_synth_violated_prints_the_counterexample(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    meta = RunMeta(
        esbmc_version="test",
        argv=("esbmc",),
        exit_code=1,
        duration_s=0.0,
        stdout="",
        stderr="",
    )
    result = PreconditionResult(
        function="foo",
        assessment=Assessment.VIOLATED,
        detail="",
        settled_k=1,
        max_len=8,
        esbmc_result=Violated(meta=meta, raw_counterexample="TRACE_TEXT"),
    )
    monkeypatch.setattr(_precond_cli, "verify_precondition", lambda *a, **k: result)
    code = main(["synth", "x.c", "--function", "foo"])
    assert code == ASSESSMENT_EXIT_CODES[Assessment.VIOLATED]
    assert "TRACE_TEXT" in capsys.readouterr().out


def test_synth_json_emits_the_result_payload(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    result = PreconditionResult(
        function="foo",
        assessment=Assessment.ASSUMED_VERIFIED,
        detail="",
        settled_k=1,
        max_len=8,
    )
    monkeypatch.setattr(_precond_cli, "verify_precondition", lambda *a, **k: result)
    code = main(["synth", "x.c", "--function", "foo", "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["assessment"] == "assumed_verified"
    assert payload["assumed"] is True


# --- discharge glue ---------------------------------------------------------


def test_discharge_emit_only_prints_the_injected_copy(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(_precond_cli, "emit_obligations", lambda *a, **k: "INJECTED\n")
    code = main(["discharge", "x.c", "--function", "foo", "--emit-only"])
    assert code == 0
    assert capsys.readouterr().out == "INJECTED\n"


def test_discharge_emit_only_unavailable_exits_the_assessment_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(*a: object, **k: object) -> str:
        raise PreconditionUnavailable(Assessment.ERROR, "no such unit")

    monkeypatch.setattr(_precond_cli, "emit_obligations", boom)
    code = main(["discharge", "x.c", "--function", "foo", "--emit-only"])
    assert code == ASSESSMENT_EXIT_CODES[Assessment.ERROR]
    assert "forseti discharge: no such unit" in capsys.readouterr().err


def test_discharge_prints_the_label_and_each_caller_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    check = SimpleNamespace(
        caller="caller_a",
        outcome=SimpleNamespace(value="discharged"),
        detail="reached and passed",
    )
    result = SimpleNamespace(
        label="DISCHARGED_VERIFIED (every caller checked)",
        assessment=Assessment.DISCHARGED_VERIFIED,
        callers=[check],
    )
    monkeypatch.setattr(_precond_cli, "discharge_precondition", lambda *a, **k: result)
    code = main(["discharge", "x.c", "--function", "foo"])
    assert code == 0
    out = capsys.readouterr().out
    assert "x.c::foo: DISCHARGED_VERIFIED" in out
    assert "caller_a(): discharged — reached and passed" in out


def test_discharge_json_emits_the_result_payload(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    result = SimpleNamespace(
        label="x",
        assessment=Assessment.VIOLATED,
        callers=[],
        to_dict=lambda: {"assessment": "violated"},
    )
    monkeypatch.setattr(_precond_cli, "discharge_precondition", lambda *a, **k: result)
    code = main(["discharge", "x.c", "--function", "foo", "--json"])
    assert code == ASSESSMENT_EXIT_CODES[Assessment.VIOLATED]
    assert json.loads(capsys.readouterr().out) == {"assessment": "violated"}

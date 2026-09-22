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
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from forseti.core import _precond_cli, cli
from forseti.core.cli import _build_parser, main
from forseti.core.events import CLI_COMMAND, events_path
from forseti.esbmc import Violated
from forseti.esbmc.result import RunMeta
from forseti.precond import (
    ASSESSMENT_EXIT_CODES,
    Assessment,
    PreconditionResult,
    PreconditionUnavailable,
)


@pytest.fixture(autouse=True)
def _trace_in_tmp_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests that omit `--store-root` trace under the default relative `.forseti`;
    keep that out of the repo checkout (#301)."""
    monkeypatch.chdir(tmp_path)


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


@pytest.mark.parametrize("command", ["synth", "discharge"])
def test_emit_only_honors_the_requested_parse_timeout(
    command: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def timing_out(
        argv: tuple[str, ...], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", timing_out)
    code = main(
        [
            command,
            "x.c",
            "--function",
            "foo",
            "--emit-only",
            "--timeout",
            "0.125",
            "--esbmc-bin",
            "fake-esbmc",
        ]
    )

    assert code == ASSESSMENT_EXIT_CODES[Assessment.ERROR]
    assert "timed out after 0.125 seconds" in capsys.readouterr().err


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


# --- cli.command trace (#301) ----------------------------------------------

# The published `cli.command` row for `synth`/`discharge`
# (docs/design/0001-harness-portability.md). Pinned as a *set*, not field by
# field: the row is a wire format, so an added or renamed key is a break, and
# the per-field assertions below cannot see one.
PRECOND_EVENT_KEYS = {
    "ts",
    "type",
    "command",
    "source",
    "function",
    "emit_only",
    "assessment",
    "exit_code",
    "duration_s",
}


def _cli_events(store_root: Path) -> list[dict[str, Any]]:
    lines = events_path(store_root).read_text().splitlines()
    return [e for e in map(json.loads, lines) if e["type"] == CLI_COMMAND]


def _stub_engine(monkeypatch: pytest.MonkeyPatch, command: str, path: str) -> None:
    def unavailable(*a: object, **k: object) -> str:
        raise PreconditionUnavailable(Assessment.NEEDS_CONTRACT, "no pointer plan")

    verdict = SimpleNamespace(
        label="x", assessment=Assessment.VIOLATED, callers=[], esbmc_result=None
    )
    engine = {
        ("synth", "emit"): ("synthesize", lambda *a, **k: "SIDECAR_C\n"),
        ("synth", "unavailable"): ("synthesize", unavailable),
        ("synth", "verdict"): ("verify_precondition", lambda *a, **k: verdict),
        ("discharge", "emit"): ("emit_obligations", lambda *a, **k: "INJECTED\n"),
        ("discharge", "unavailable"): ("emit_obligations", unavailable),
        ("discharge", "verdict"): ("discharge_precondition", lambda *a, **k: verdict),
    }
    name, fn = engine[(command, path)]
    monkeypatch.setattr(_precond_cli, name, fn)


@pytest.mark.parametrize("command", ["synth", "discharge"])
@pytest.mark.parametrize(
    ("path", "emit_only", "assessment", "exit_code"),
    [
        ("emit", True, None, 0),
        (
            "unavailable",
            True,
            "needs_contract",
            ASSESSMENT_EXIT_CODES[Assessment.NEEDS_CONTRACT],
        ),
        ("verdict", False, "violated", ASSESSMENT_EXIT_CODES[Assessment.VIOLATED]),
    ],
)
def test_synth_and_discharge_record_one_cli_command_event_per_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    command: str,
    path: str,
    emit_only: bool,
    assessment: str | None,
    exit_code: int,
) -> None:
    _stub_engine(monkeypatch, command, path)
    root = tmp_path / "elsewhere" / ".forseti"
    argv = [command, "x.c", "--function", "foo", "--store-root", str(root)]
    code = main([*argv, "--emit-only"] if emit_only else argv)

    assert code == exit_code
    (event,) = _cli_events(root)
    assert set(event) == PRECOND_EVENT_KEYS
    assert event["command"] == command
    assert event["source"] == "x.c"
    assert event["function"] == "foo"
    assert event["emit_only"] is emit_only
    assert event["assessment"] == assessment
    assert event["exit_code"] == exit_code
    assert event["duration_s"] >= 0
    # Keyed by --store-root, not the cwd (the demo shim's cwd-keyed gap).
    assert not (tmp_path / ".forseti").exists()


def test_synth_trace_defaults_to_the_relative_forseti_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stub_engine(monkeypatch, "synth", "emit")
    assert main(["synth", "x.c", "--function", "foo", "--emit-only"]) == 0
    (event,) = _cli_events(tmp_path / ".forseti")
    assert event["command"] == "synth"


def test_a_failing_trace_write_never_changes_the_exit_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _stub_engine(monkeypatch, "discharge", "verdict")
    blocked = tmp_path / "not_a_dir"
    blocked.write_text("")
    code = main(
        [
            "discharge",
            "x.c",
            "--function",
            "foo",
            "--store-root",
            str(blocked / ".forseti"),
        ]
    )
    assert code == ASSESSMENT_EXIT_CODES[Assessment.VIOLATED]

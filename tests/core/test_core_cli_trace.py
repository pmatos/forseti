"""Hermetic tests for the `cli.command` trace seam (#301).

`forseti.core.events` *states* the policy — one `cli.command` event per
invocation of a traced subcommand, on every exit path, emitted by the CLI
handler and never by the engine. :mod:`forseti.core._cli_trace` is the one place
that policy is *carried out*, for `synth`, `discharge` and `semantic-loop`
alike.

Exercised through the seam, against the real sink: `traced` writes a real
`events.jsonl` under ``tmp_path``, because the published contract row is a JSON
*line* — asserting a Python dict against a fake sink would stop testing
``sort_keys``, the one-line append, and JSON-serialisability, which are the
properties that make the trace a contract. The per-command field sets remain
pinned end-to-end through ``main`` in ``test_precond_cli`` and
``test_semantic_loop_cli``; what is pinned *here* is the shared machinery those
two rows ride on.
"""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
from typing import Any

import pytest

from forseti.core import _cli_trace, _precond_cli, cli
from forseti.core._cli_trace import COMMON_FIELDS, traced
from forseti.core.events import CLI_COMMAND, events_path


def _namespace(store_root: Path, **extra: object) -> argparse.Namespace:
    return argparse.Namespace(
        store_root=store_root, source=Path("x.c"), function="foo", **extra
    )


def _events(store_root: Path) -> list[dict[str, Any]]:
    lines = events_path(store_root).read_text().splitlines()
    return [e for e in map(json.loads, lines) if e["type"] == CLI_COMMAND]


def _no_fields(_args: argparse.Namespace, _result: object) -> dict[str, Any]:
    return {}


def test_traced_records_one_event_and_returns_the_handlers_exit_code(
    tmp_path: Path,
) -> None:
    def run(_args: argparse.Namespace) -> tuple[int, str]:
        return 7, "settled"

    def fields(_args: argparse.Namespace, result: str) -> dict[str, Any]:
        return {"result": result}

    args = _namespace(tmp_path)
    assert traced("probe", run, args, fields=fields) == 7

    (event,) = _events(tmp_path)
    assert event["command"] == "probe"
    assert event["source"] == "x.c"
    assert event["function"] == "foo"
    assert event["exit_code"] == 7
    assert event["result"] == "settled"
    assert event["duration_s"] >= 0


def test_traced_records_an_event_for_a_handler_that_produced_no_result(
    tmp_path: Path,
) -> None:
    # The early-argument-error and successful --emit-only shape: an exit code
    # with nothing to report. The event is recorded all the same -- that is the
    # whole point of "on every exit path".
    def run(_args: argparse.Namespace) -> tuple[int, str | None]:
        return 1, None

    def fields(_args: argparse.Namespace, result: str | None) -> dict[str, Any]:
        return {"result": result}

    assert traced("probe", run, _namespace(tmp_path), fields=fields) == 1

    (event,) = _events(tmp_path)
    assert event["exit_code"] == 1
    assert event["result"] is None


def test_common_fields_is_what_traced_actually_emits(tmp_path: Path) -> None:
    # Derived from a real emission rather than restated, so the constant cannot
    # drift from the keys `traced` fills (minus `record_event`'s own stamps).
    traced("probe", lambda _a: (0, None), _namespace(tmp_path), fields=_no_fields)

    (event,) = _events(tmp_path)
    assert set(event) - {"ts", "type"} == COMMON_FIELDS


def test_the_clock_is_read_before_the_handler_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Discriminating where `duration_s >= 0` is not: a clock captured *after*
    # the handler, or a hard-coded zero, both pass that assertion and fail this.
    ticks = iter([10.0, 12.5])
    monkeypatch.setattr(_cli_trace.time, "monotonic", lambda: next(ticks))

    traced("probe", lambda _a: (0, None), _namespace(tmp_path), fields=_no_fields)

    (event,) = _events(tmp_path)
    assert event["duration_s"] == 2.5


def test_a_handler_that_raises_records_nothing(tmp_path: Path) -> None:
    # Deliberate: there is no try/finally. "Every exit path" means every
    # *return* -- the structural guarantee is the tuple-returning handler. An
    # escaping exception produces no event today, and inventing a payload for a
    # crash would change a published wire format, so pin the absence.
    def run(_args: argparse.Namespace) -> tuple[int, None]:
        raise RuntimeError("engine blew up")

    with pytest.raises(RuntimeError):
        traced("probe", run, _namespace(tmp_path), fields=_no_fields)

    assert not events_path(tmp_path).exists()


def test_a_failing_trace_write_never_changes_the_exit_code(tmp_path: Path) -> None:
    # `record_event` swallows its own I/O errors; assert the seam inherits that
    # rather than re-establishing it, so a trace failure cannot turn a verdict
    # into an error.
    blocked = tmp_path / "not_a_dir"
    blocked.write_text("")

    args = _namespace(blocked / ".forseti")
    assert traced("probe", lambda _a: (3, None), args, fields=_no_fields) == 3


@pytest.mark.parametrize(
    ("builder", "args", "result"),
    [
        (
            _precond_cli._precond_fields,
            argparse.Namespace(emit_only=True),
            None,
        ),
        (
            cli._semantic_loop_fields,
            argparse.Namespace(mode="submit"),
            None,
        ),
    ],
)
def test_no_field_builder_shadows_a_common_field(
    builder: Any, args: argparse.Namespace, result: Any
) -> None:
    # A builder returning a common key is a `TypeError` raised while *building*
    # the record_event call -- loud, but only once that command runs. These two
    # are the whole population, so assert it statically instead.
    assert not COMMON_FIELDS & set(builder(args, result))


def test_cli_trace_is_a_leaf_below_the_two_cli_modules() -> None:
    """`cli` and `_precond_cli` both import the seam; it must import neither.

    An AST walk over the *import statements*, not the source text -- the module
    docstring names both callers in prose. Putting the seam in `cli` would make
    `_precond_cli` import its own importer, and putting it in `_precond_cli`
    would leave a general CLI concern inside precond-only glue; this is the
    source-level check that it ended up below both instead.
    """
    tree = ast.parse(Path(_cli_trace.__file__).read_text())
    imported = [
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    ] + [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    ]
    assert not [name for name in imported if "cli" in name], imported

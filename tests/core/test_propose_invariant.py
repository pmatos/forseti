"""Core's `propose` face for representation invariants: function, CLI and MCP (#420).

Hermetic: a fake `LLMClient`, a real store under `tmp_path`, no ESBMC.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from forseti.core import propose_invariant_source
from forseti.core.cli import main
from forseti.core.events import events_path
from forseti.core.mcp_server import propose_tool, submit_tool
from forseti.properties import (
    INVARIANT_ASSUMPTION_NOTE,
    HarnessError,
    PropertyKind,
    PropertyStatus,
    PropertyStore,
    RecipeError,
)

SOURCE = """\
/* A fixed-capacity ring. */
#define CAP 4
typedef struct { unsigned head, tail, count; int buf[CAP]; } ring_t;
struct node { int v; };

/* Append v; the ring must not be full. */
int rb_push(ring_t *r, int v) {
    r->buf[r->tail] = v;
    r->tail = (r->tail + 1) % CAP;
    r->count++;
    return 1;
}

int node_get(struct node *n) { return n->v; }
int plain(int x) { return x; }
"""
INV = "self->count <= CAP && self->tail < CAP"
REPLY = json.dumps(
    {
        "candidates": [
            {"expression": INV, "rationale": "indices stay in range"},
            {"expression": "0"},
        ]
    }
)


class FakeLLMClient:
    def __init__(
        self, reply: str = REPLY, *, provider: str = "fake", model: str = "fake-1"
    ) -> None:
        self._reply = reply
        self.provider = provider
        self.model = model
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self._reply


def _source(tmp_path: Path) -> Path:
    path = tmp_path / "ring.c"
    path.write_text(SOURCE)
    return path


def _patch_client(
    monkeypatch: pytest.MonkeyPatch, reply: str = REPLY
) -> list[FakeLLMClient]:
    made: list[FakeLLMClient] = []

    def factory(**_kw: object) -> FakeLLMClient:
        made.append(FakeLLMClient(reply))
        return made[-1]

    monkeypatch.setattr("forseti.core.propose.ClaudeCliClient", factory)
    return made


def test_a_proposal_stores_survivors_as_candidates_keyed_by_the_type(
    tmp_path: Path,
) -> None:
    source, root = _source(tmp_path), tmp_path / ".forseti"
    client = FakeLLMClient()

    result = propose_invariant_source(
        source,
        function="rb_push",
        type_name="ring_t",
        store_root=root,
        client=client,
    )

    type_key = f"{source}::ring_t"
    (prop, *rest) = result.accepted
    assert not rest
    assert prop.expression == INV
    assert prop.kind is PropertyKind.REP_INVARIANT
    assert prop.status is PropertyStatus.CANDIDATE
    assert result.unit_id == type_key
    assert "vacuous" in result.rejected[0].reason
    with PropertyStore.open(root) as store:
        assert store.list_for_type(type_key) == (prop,)
        assert store.list_for_unit(f"{source}::rb_push") == ()
    events = [json.loads(line) for line in events_path(root).read_text().splitlines()]
    assert [(e["type"], e["channel"]) for e in events] == [("property.proposed", "llm")]
    assert "const ring_t *" in client.prompts[0]
    assert "rb_push" in client.prompts[0]


def test_a_dry_run_does_not_create_the_store(tmp_path: Path) -> None:
    source, root = _source(tmp_path), tmp_path / ".forseti"
    result = propose_invariant_source(
        source,
        function="rb_push",
        type_name="ring_t",
        persist=False,
        store_root=root,
        client=FakeLLMClient(),
    )
    assert result.accepted
    assert not root.exists()


def test_a_tagged_struct_and_its_spacing_resolve_to_one_key(tmp_path: Path) -> None:
    source = _source(tmp_path)
    reply = json.dumps({"candidates": [{"expression": "self->v >= 0"}]})
    result = propose_invariant_source(
        source,
        function="node_get",
        type_name="struct   node",
        persist=False,
        client=FakeLLMClient(reply),
    )
    assert result.unit_id == f"{source}::struct node"
    assert len(result.accepted) == 1


@pytest.mark.parametrize(
    ("function", "type_name", "fragment"),
    [
        ("plain", "ring_t", "takes no 'ring_t' pointer"),
        ("rb_push", "struct node", "takes no 'struct node' pointer"),
        ("missing", "ring_t", "could not find a definition"),
        ("rb_push", "ring_t *", "not a struct type name"),
    ],
)
def test_a_function_that_does_not_take_the_type_is_refused_before_any_llm_call(
    tmp_path: Path, function: str, type_name: str, fragment: str
) -> None:
    client = FakeLLMClient()
    with pytest.raises(HarnessError, match=fragment):
        propose_invariant_source(
            _source(tmp_path),
            function=function,
            type_name=type_name,
            persist=False,
            client=client,
        )
    assert client.prompts == []


@pytest.mark.parametrize(
    ("recipe", "shown", "hidden"),
    [
        ("sighted", "r->tail = (r->tail + 1) % CAP;", "node_get"),
        ("blind", "int rb_push(ring_t *r, int v);", "r->tail = (r->tail + 1)"),
        ("baseline", "int node_get(struct node *n)", "A fixed-capacity ring."),
    ],
)
def test_a_recipe_chooses_what_the_prompt_shows_and_is_recorded(
    tmp_path: Path, recipe: str, shown: str, hidden: str
) -> None:
    source, root = _source(tmp_path), tmp_path / ".forseti"
    client = FakeLLMClient()

    propose_invariant_source(
        source,
        function="rb_push",
        type_name="ring_t",
        store_root=root,
        client=client,
        recipe=recipe,
    )

    assert shown in client.prompts[0]
    assert hidden not in client.prompts[0]
    assert "typedef struct" in client.prompts[0]
    with PropertyStore.open(root) as store:
        (stored,) = store.list_for_type(f"{source}::ring_t")
    assert stored.provenance.recipe == f"{recipe}/v1"


def test_an_unknown_recipe_is_a_recipe_error(tmp_path: Path) -> None:
    with pytest.raises(RecipeError):
        propose_invariant_source(
            _source(tmp_path),
            function="rb_push",
            type_name="ring_t",
            persist=False,
            client=FakeLLMClient(),
            recipe="nosuch",
        )


def test_cli_proposes_invariants_and_labels_them_assumed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source, root = _source(tmp_path), tmp_path / ".forseti"
    _patch_client(monkeypatch)

    code = main(
        [
            "propose",
            str(source),
            "--function",
            "rb_push",
            "--kind",
            "rep-invariant",
            "--type",
            "ring_t",
            "--store-root",
            str(root),
        ]
    )

    out = capsys.readouterr().out
    assert code == 0
    assert f"{source}::ring_t" in out
    assert INV in out
    assert "vacuous" in out
    assert INVARIANT_ASSUMPTION_NOTE in out
    assert "proof" not in out.lower()
    with PropertyStore.open(root) as store:
        assert len(store.list_for_type(f"{source}::ring_t")) == 1


def test_cli_json_is_the_proposal_payload_with_the_assumption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_client(monkeypatch)
    code = main(
        [
            "propose",
            str(_source(tmp_path)),
            "--function",
            "rb_push",
            "--kind",
            "rep-invariant",
            "--type",
            "ring_t",
            "--no-store",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["accepted"][0]["kind"] == "rep_invariant"
    assert payload["accepted"][0]["status"] == "candidate"
    assert payload["assumption"] == INVARIANT_ASSUMPTION_NOTE


@pytest.mark.parametrize(
    ("extra", "fragment"),
    [
        (("--kind", "rep-invariant"), "requires a type"),
        (("--type", "ring_t"), "rep-invariant only"),
    ],
)
def test_cli_flag_misuse_exits_two(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    extra: tuple[str, ...],
    fragment: str,
) -> None:
    code = main(["propose", str(_source(tmp_path)), "--function", "rb_push", *extra])
    assert code == 2
    assert fragment in capsys.readouterr().err


def test_cli_a_function_without_the_type_exits_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_client(monkeypatch)
    code = main(
        [
            "propose",
            str(_source(tmp_path)),
            "--function",
            "plain",
            "--kind",
            "rep-invariant",
            "--type",
            "ring_t",
            "--no-store",
        ]
    )
    assert code == 1
    assert "forseti propose:" in capsys.readouterr().err


def test_mcp_propose_tool_takes_the_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, root = _source(tmp_path), tmp_path / ".forseti"
    _patch_client(monkeypatch)

    payload = propose_tool(
        str(source),
        "rb_push",
        store_root=str(root),
        kind="rep-invariant",
        type_name="ring_t",
    )

    assert payload["unit_id"] == f"{source}::ring_t"
    assert payload["assumption"] == INVARIANT_ASSUMPTION_NOTE
    with PropertyStore.open(root) as store:
        assert len(store.list_for_type(f"{source}::ring_t")) == 1


def test_mcp_submit_tool_stores_an_invariant_per_type(tmp_path: Path) -> None:
    source, root = _source(tmp_path), tmp_path / ".forseti"

    payload = submit_tool(
        str(source),
        "rb_push",
        INV,
        provider="codex",
        model="gpt-5.1",
        store_root=str(root),
        kind="rep-invariant",
        type_name="ring_t",
    )

    assert payload["unit_id"] == f"{source}::ring_t"
    accepted = payload["accepted"]
    assert isinstance(accepted, list)
    assert accepted[0]["kind"] == "rep_invariant"
    assert accepted[0]["provenance"]["provider"] == "codex"
    assert payload["assumption"] == INVARIANT_ASSUMPTION_NOTE
    with PropertyStore.open(root) as store:
        assert len(store.list_for_type(f"{source}::ring_t")) == 1


def test_mcp_submit_tool_rejects_a_vacuous_invariant_with_a_reason(
    tmp_path: Path,
) -> None:
    root = tmp_path / ".forseti"
    payload = submit_tool(
        str(_source(tmp_path)),
        "rb_push",
        "0",
        provider="codex",
        model="gpt-5.1",
        store_root=str(root),
        kind="rep-invariant",
        type_name="ring_t",
    )
    assert payload["accepted"] == []
    rejected = payload["rejected"]
    assert isinstance(rejected, list)
    assert "vacuous" in rejected[0]["reason"]
    with PropertyStore.open(root) as store:
        assert store.list_for_type(f"{_source(tmp_path)}::ring_t") == ()


def _propose(source: str, **kw: Any) -> object:
    return propose_tool(source, "rb_push", persist=False, **kw)


def _submit(source: str, **kw: Any) -> object:
    return submit_tool(
        source, "rb_push", INV, provider="p", model="m", persist=False, **kw
    )


@pytest.mark.parametrize(
    ("call", "kwargs", "fragment"),
    [
        (_propose, {"kind": "rep-invariant"}, "requires a type"),
        (_propose, {"type_name": "ring_t"}, "rep-invariant only"),
        (_propose, {"kind": "reachability"}, "kind must be one of"),
        (
            _submit,
            {"kind": "rep-invariant", "type_name": "ring_t", "domain": ["x"]},
            "per type",
        ),
        (_submit, {"kind": "rep-invariant"}, "requires a type"),
    ],
)
def test_mcp_kind_misuse_is_a_value_error(
    tmp_path: Path,
    call: Callable[..., object],
    kwargs: dict[str, object],
    fragment: str,
) -> None:
    with pytest.raises(ValueError, match=fragment):
        call(str(_source(tmp_path)), **kwargs)

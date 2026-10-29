"""The E0 runner with the proposer and ESBMC faked: no `claude`, no `esbmc`.

The proposer is a scripted `LLMClient` (so the real prompt rendering, parsing and
static validation still run) and ESBMC a scripted `VerifyPort` that reads the
harness text. The tests pin the runner's own rules: the row shapes, resume, the
pinned manifest, the torn-line repair, and a summary that is a function of the
rows alone.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from forseti._events import events_path
from forseti.esbmc import (
    EsbmcResult,
    RunMeta,
    Unknown,
    UnknownReason,
    Verified,
    Violated,
)
from forseti.experiments import e0
from forseti.experiments.e0 import (
    PROPERTY_EVENT,
    PROPOSAL_EVENT,
    E0Error,
    Manifest,
    UnitSpec,
    load_manifest,
    read_rows,
    run_grid,
    summarise,
    summary_from_run_dir,
)
from forseti.properties import LLMError, LLMUsage, Recipe

REPO = Path(__file__).parents[2]
STEP = 0.5

CLEAN_SRC = """\
/* header comment */
int clamp(int x) { return x < 0 ? 0 : x; }
int main(void) { return clamp(0); }
"""
TWIN_SRC = """\
/* TWIN */
int clamp(int x) { return x; }
int main(void) { return clamp(0); }
"""
REPLY = json.dumps(
    {
        "candidates": [
            {
                "expression": "result >= 0",
                "domain": [],
                "referenced_params": [],
                "rationale": "clamped",
            },
            {
                "expression": "result <= 100",
                "domain": ["x <= 100"],
                "referenced_params": ["x"],
                "rationale": "upper",
            },
            {"expression": "bogus_name > 0", "domain": []},
        ]
    }
)


def meta() -> RunMeta:
    return RunMeta("8.6.0", ("esbmc",), 0, STEP, "", "")


class FakeEsbmc:
    """Reads the harness: the twin violates `result >= 0` and times out on the
    upper bound; the clean unit holds both. A probe always reaches."""

    def __init__(self, interrupt_after: int | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.interrupt_after = interrupt_after

    def __call__(self, source: Path, /) -> Any:
        def port(path: Path, /, *, unwind: int) -> EsbmcResult:
            if self.interrupt_after is not None and (
                len(self.calls) >= self.interrupt_after
            ):
                raise KeyboardInterrupt
            text = path.read_text()
            target = "twin" if "TWIN" in text else "clean"
            self.calls.append((target, path.name))
            if path.name.endswith(".probe.c"):
                return Violated(meta(), "Violated property: forseti:non-vacuity")
            if target == "twin" and "result >= 0" in text:
                return Violated(meta(), "[Counterexample]")
            if target == "twin":
                return Unknown(meta(), UnknownReason.TIMEOUT)
            return Verified(meta())

        return port

    def checks(self) -> list[tuple[str, str]]:
        return [c for c in self.calls if not c[1].endswith(".probe.c")]


class FakeClient:
    provider = "fake"
    model = "fake-model"

    def __init__(self, *replies: str | BaseException) -> None:
        self.replies = list(replies) or [REPLY]
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, BaseException):
            raise reply
        return reply


class UsageClient(FakeClient):
    def complete_with_usage(self, prompt: str) -> tuple[str, LLMUsage | None]:
        return self.complete(prompt), LLMUsage(100, 20, 7, 3, cost_usd=0.5)


@pytest.fixture
def sources(tmp_path: Path) -> tuple[Path, Path]:
    clean = tmp_path / "clamp.c"
    twin = tmp_path / "clamp_bug.c"
    clean.write_text(CLEAN_SRC)
    twin.write_text(TWIN_SRC)
    return clean, twin


def manifest_for(
    sources: tuple[Path, Path], *, twin: bool = True, **fields: Any
) -> Manifest:
    clean, twin_path = sources
    unit = UnitSpec(str(clean), "clamp", str(twin_path) if twin else None)
    defaults: dict[str, Any] = {
        "name": "t",
        "units": (unit,),
        "recipes": ("blind", "sighted"),
        "samples": 2,
    }
    return Manifest(**{**defaults, **fields})


def rows_of(run_dir: Path, kind: str) -> list[dict[str, Any]]:
    return [r for r in read_rows(run_dir) if r["type"] == kind]


def run(
    manifest: Manifest,
    run_dir: Path,
    client: FakeClient | None = None,
    esbmc: FakeEsbmc | None = None,
) -> e0.Summary:
    return run_grid(
        manifest,
        run_dir,
        client=client or FakeClient(),
        verify_for=esbmc or FakeEsbmc(),
    )


def test_the_checked_in_manifest_describes_the_e0_grid() -> None:
    manifest = load_manifest(REPO / "experiments" / "e0" / "manifest.toml")

    assert manifest.recipes == ("baseline", "blind", "sighted")
    assert manifest.samples == 3
    assert manifest.esbmc_timeout_s == 30
    assert len(manifest.cells()) == 54
    assert {u.symbol for u in manifest.units} == {
        "rb_push",
        "msort",
        "utf8_decode",
        "murmur3_32",
        "sha1_update",
        "sum_bytes",
    }
    assert not any("abs" in u.path for u in manifest.units)
    for unit in manifest.units:
        assert unit.twin is not None
        assert (REPO / unit.path).is_file()
        assert (REPO / unit.twin).is_file()


def test_the_checked_in_manifest_builds_every_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(REPO)
    manifest = load_manifest(REPO / "experiments" / "e0" / "manifest.toml")

    prepared = e0._prepare(manifest, esbmc_bin="esbmc", verify_for=FakeEsbmc())

    assert len(prepared) == 6
    for unit in prepared:
        assert set(unit.requests) == {"baseline", "blind", "sighted"}
        assert [t.name for t in unit.targets] == ["clean", "twin"]
        for request in unit.requests.values():
            assert request.context is not None
            assert request.symbol in request.context.text
        baseline = unit.requests["baseline"].context
        assert baseline is not None and "int main" not in baseline.text


def test_manifest_round_trips_through_its_pinned_form() -> None:
    manifest = Manifest(
        name="m",
        units=(UnitSpec("a.c", "f", "b.c", unwind=2, unwind_ladder=(), max_len=1),),
        unwind_ladder=(8,),
    )

    pinned = manifest.to_dict()

    assert pinned["recipe_refs"] == ["baseline/v1", "sighted/v1", "blind/v1"]
    assert Manifest.from_dict(json.loads(json.dumps(pinned))) == manifest
    bounds = manifest.bounds_for(manifest.units[0])
    assert (bounds.unwind, bounds.unwind_ladder, bounds.max_len) == (2, (), 1)
    plain = Manifest(name="m", units=(UnitSpec("a.c", "f"),), unwind_ladder=(8,))
    assert plain.bounds_for(plain.units[0]).unwind_ladder == (8,)


@pytest.mark.parametrize(
    "data",
    [
        {"name": "m"},
        {"name": "m", "units": []},
        {"name": "m", "units": ["x"]},
        {"name": "m", "units": [{"path": "a.c"}]},
        {"name": "m", "units": [{"path": "a.c", "symbol": "f", "bogus": 1}]},
        {"name": "m", "units": [{"path": "a.c", "symbol": "f"}] * 2},
        {"name": "m", "units": [{"path": "a.c", "symbol": "f"}], "recipes": ["nope"]},
        {"name": "m", "units": [{"path": "a.c", "symbol": "f"}], "recipes": []},
        {"name": "m", "units": [{"path": "a.c", "symbol": "f"}], "samples": 0},
        {"name": "m", "units": [{"path": "a.c", "symbol": "f"}], "max_len": -1},
        {"name": "m", "units": [{"path": "a.c", "symbol": "f"}], "bogus": 1},
        {"name": "", "units": [{"path": "a.c", "symbol": "f"}]},
        {"name": "m", "units": [{"path": "a.c", "symbol": "f"}], "samples": "3"},
        {"name": "m", "units": [{"path": "a.c", "symbol": "f"}], "samples": True},
        {"name": "m", "units": [{"path": "a.c", "symbol": "f"}], "recipes": 5},
        {
            "name": "m",
            "units": [{"path": "a.c", "symbol": "f"}],
            "recipes": ["blind", "blind"],
        },
        {
            "name": "m",
            "units": [{"path": "a.c", "symbol": "f"}],
            "esbmc_timeout_s": "30",
        },
        {
            "name": "m",
            "units": [{"path": "a.c", "symbol": "f"}],
            "unwind_ladder": "8,16",
        },
        {
            "name": "m",
            "units": [{"path": "a.c", "symbol": "f", "unwind_ladder": [1.5]}],
        },
        {"name": "m", "units": [{"path": "a.c", "symbol": "f", "max_len": "2"}]},
        {"name": "m", "units": [{"path": "a.c", "symbol": "f", "twin": 3}]},
        {
            "name": "m",
            "units": [{"path": "a.c", "symbol": "f"}],
            "esbmc_timeout_s": 0,
        },
    ],
)
def test_a_bad_manifest_is_an_e0_error(data: dict[str, Any]) -> None:
    with pytest.raises(E0Error):
        Manifest.from_dict(data)


def test_load_manifest_reports_an_unreadable_file(tmp_path: Path) -> None:
    with pytest.raises(E0Error, match="cannot read manifest"):
        load_manifest(tmp_path / "missing.toml")
    broken = tmp_path / "broken.toml"
    broken.write_text("name = ")
    with pytest.raises(E0Error, match="cannot read manifest"):
        load_manifest(broken)


def test_a_run_writes_one_proposal_row_per_cell_and_one_row_per_property_target(
    tmp_path: Path, sources: tuple[Path, Path]
) -> None:
    manifest = manifest_for(sources)
    client, esbmc = FakeClient(), FakeEsbmc()

    summary = run(manifest, tmp_path / "run", client, esbmc)

    run_dir = tmp_path / "run"
    proposals = rows_of(run_dir, PROPOSAL_EVENT)
    assert len(proposals) == 4  # 2 recipes x 2 samples
    assert len(client.prompts) == 4
    first = proposals[0]
    assert first["unit_id"] == f"{sources[0]}::clamp"
    assert first["recipe"] == "blind/v1"
    assert first["prompt"] == "semantic/v1"
    assert (first["provider"], first["model"]) == ("fake", "fake-model")
    assert first["sample"] == 0 and first["attempt"] == 1
    assert first["proposed"] == 3
    assert len(first["accepted"]) == 2
    assert [r["expression"] for r in first["rejected"]] == ["bogus_name > 0"]
    assert first["error"] is None and first["usage"] is None
    assert first["latency_s"] >= 0

    checks = rows_of(run_dir, PROPERTY_EVENT)
    assert len(checks) == 4 * 2 * 2  # cells x properties x targets
    by = {(c["recipe"], c["sample"], c["target"], c["expression"]): c for c in checks}
    clean_ok = by[("blind/v1", 0, "clean", "result >= 0")]
    assert clean_ok["outcome"] == "held" and clean_ok["rendered"] is True
    assert clean_ok["k"] == 4 and clean_ok["probe"] == "reached"
    assert clean_ok["esbmc_time_s"] == 2 * STEP  # the run and its vacuity probe
    assert clean_ok["esbmc_version"] == "8.6.0"
    assert clean_ok["target_path"] == str(sources[0])
    assert (clean_ok["unwind"], clean_ok["ladder"]) == (4, [8, 16])
    assert clean_ok["max_len"] == 8 and clean_ok["esbmc_timeout_s"] == 30.0
    assert by[("blind/v1", 0, "twin", "result >= 0")]["outcome"] == "violated"
    stuck = by[("blind/v1", 0, "twin", "result <= 100")]
    assert stuck["outcome"] == "unknown" and stuck["detail"] == "timeout"
    assert stuck["k"] == 16 and stuck["esbmc_runs"] == 3  # the ladder, rung by rung

    assert summary.complete
    assert json.loads(events_path(run_dir).read_text().splitlines()[0])["ts"] > 0


def test_each_recipe_decides_what_the_prompt_shows(
    tmp_path: Path, sources: tuple[Path, Path]
) -> None:
    client = FakeClient()

    run(
        manifest_for(sources, recipes=("blind", "baseline"), samples=1),
        tmp_path / "r",
        client,
    )

    blind, baseline = client.prompts
    assert "int clamp(int x);" in blind
    assert "return x < 0" not in blind
    assert "return x < 0" in baseline and "int main" not in baseline


def test_a_unit_without_a_twin_is_checked_on_the_clean_source_only(
    tmp_path: Path, sources: tuple[Path, Path]
) -> None:
    esbmc = FakeEsbmc()

    summary = run(
        manifest_for(sources, twin=False, recipes=("blind",), samples=1),
        tmp_path / "r",
        esbmc=esbmc,
    )

    assert {c["target"] for c in rows_of(tmp_path / "r", PROPERTY_EVENT)} == {"clean"}
    assert {target for target, _ in esbmc.calls} == {"clean"}
    assert summary.complete


def test_a_unit_overrides_the_manifest_bounds(
    tmp_path: Path, sources: tuple[Path, Path]
) -> None:
    clean, twin = sources
    unit = UnitSpec(
        str(clean), "clamp", str(twin), unwind=2, unwind_ladder=(), max_len=3
    )

    run(
        Manifest(name="t", units=(unit,), recipes=("blind",), samples=1),
        tmp_path / "r",
    )

    for row in rows_of(tmp_path / "r", PROPERTY_EVENT):
        assert (row["unwind"], row["ladder"], row["max_len"]) == (2, [], 3)
        assert row["esbmc_runs"] <= 2  # one run, plus a probe when it held


def row_signature(run_dir: Path) -> list[tuple[Any, ...]]:
    return [
        (r["type"], r["unit_id"], r["recipe"], r["sample"], r.get("target"),
         r.get("property_id"), r.get("outcome"))
        for r in read_rows(run_dir)
    ]  # fmt: skip


def untimed(summary: e0.Summary) -> list[dict[str, Any]]:
    """The summary without the proposer wall-clock, the one number a rerun moves."""
    return [
        {k: v for k, v in recipe.items() if k != "proposer_latency_s"}
        for recipe in summary.to_dict()["recipes"]
    ]


def test_a_resumed_run_repeats_neither_a_proposal_nor_a_check(
    tmp_path: Path, sources: tuple[Path, Path]
) -> None:
    manifest = manifest_for(sources)
    reference = tmp_path / "reference"
    run(manifest, reference)

    interrupted = tmp_path / "interrupted"
    first_client, first_esbmc = FakeClient(), FakeEsbmc(interrupt_after=9)
    with pytest.raises(KeyboardInterrupt):
        run(manifest, interrupted, first_client, first_esbmc)
    done_checks = len(rows_of(interrupted, PROPERTY_EVENT))
    done_proposals = len(rows_of(interrupted, PROPOSAL_EVENT))
    assert 0 < done_checks < 16

    client, esbmc = FakeClient(), FakeEsbmc()
    summary = run(manifest, interrupted, client, esbmc)

    assert summary.complete
    assert len(client.prompts) == 4 - done_proposals
    assert esbmc.checks()
    assert sorted(map(str, row_signature(interrupted))) == sorted(
        map(str, row_signature(reference))
    )
    assert untimed(summary) == untimed(summary_from_run_dir(reference))


def test_a_finished_run_resumes_to_a_no_op(
    tmp_path: Path, sources: tuple[Path, Path]
) -> None:
    manifest = manifest_for(sources)
    run(manifest, tmp_path / "r")
    before = events_path(tmp_path / "r").read_text()
    client, esbmc = FakeClient(), FakeEsbmc()

    run(manifest, tmp_path / "r", client, esbmc)

    assert not client.prompts and not esbmc.calls
    assert events_path(tmp_path / "r").read_text() == before


def test_a_torn_last_line_is_skipped_and_does_not_corrupt_the_next_row(
    tmp_path: Path, sources: tuple[Path, Path]
) -> None:
    manifest = manifest_for(sources, recipes=("blind",), samples=1)
    run_dir = tmp_path / "r"
    with pytest.raises(KeyboardInterrupt):
        run(manifest, run_dir, esbmc=FakeEsbmc(interrupt_after=3))
    with events_path(run_dir).open("a") as handle:
        handle.write('{"type": "e0.property", "unit_id": "tor')  # killed mid-write
    complete = len(read_rows(run_dir))

    summary = run(manifest, run_dir)

    assert summary.complete
    assert len(read_rows(run_dir)) > complete
    lines = events_path(run_dir).read_text().splitlines()
    assert sum(1 for line in lines if not _is_json(line)) == 1  # only the torn one


def _is_json(line: str) -> bool:
    try:
        json.loads(line)
    except json.JSONDecodeError:
        return False
    return True


def test_a_run_directory_is_pinned_to_its_manifest(
    tmp_path: Path, sources: tuple[Path, Path]
) -> None:
    run(manifest_for(sources, samples=1), tmp_path / "r")
    client = FakeClient()

    with pytest.raises(E0Error, match="different manifest"):
        run(manifest_for(sources, samples=2), tmp_path / "r", client)

    assert not client.prompts
    (tmp_path / "r" / "manifest.json").write_text("{not json")
    with pytest.raises(E0Error, match="cannot read"):
        run(manifest_for(sources, samples=1), tmp_path / "r", client)


def test_a_corrupt_proposal_row_is_an_e0_error(
    tmp_path: Path, sources: tuple[Path, Path]
) -> None:
    manifest = manifest_for(sources, recipes=("blind",), samples=1)
    run_dir = tmp_path / "r"
    run(manifest, run_dir)
    lines = events_path(run_dir).read_text().splitlines()
    rows = [json.loads(line) for line in lines]
    for row in rows:
        if row["type"] == PROPOSAL_EVENT:
            row["accepted"] = [{"no": "expression"}]
        else:
            row["attempt"] = 99
    events_path(run_dir).write_text("".join(json.dumps(r) + "\n" for r in rows))

    with pytest.raises(E0Error, match="corrupt proposal row"):
        run(manifest, run_dir)


def test_a_failed_proposal_is_recorded_and_retried_on_the_next_run(
    tmp_path: Path, sources: tuple[Path, Path]
) -> None:
    manifest = manifest_for(sources, recipes=("blind",), samples=2)
    run_dir = tmp_path / "r"
    flaky = FakeClient(LLMError("claude timed out after 240s"), REPLY)

    first = run(manifest, run_dir, flaky)

    failed = rows_of(run_dir, PROPOSAL_EVENT)[0]
    assert failed["error"] == "LLMError: claude timed out after 240s"
    assert failed["accepted"] == [] and failed["proposed"] == 0
    assert not first.complete
    assert first.recipes[0].proposal_errors == 1
    assert "NO" in first.render()

    client = FakeClient()
    second = run(manifest, run_dir, client)

    assert len(client.prompts) == 1  # only the failed cell is asked again
    assert [r["attempt"] for r in rows_of(run_dir, PROPOSAL_EVENT)] == [1, 1, 2]
    assert second.complete
    recipe = second.recipes[0]
    assert recipe.attempts == 3 and recipe.proposal_errors == 0
    assert recipe.targets["clean"].checked == 4  # 2 cells x 2 properties


def test_an_unparseable_reply_is_a_recorded_proposal_error(
    tmp_path: Path, sources: tuple[Path, Path]
) -> None:
    summary = run(
        manifest_for(sources, recipes=("blind",), samples=1),
        tmp_path / "r",
        FakeClient("not json at all"),
    )

    (row,) = rows_of(tmp_path / "r", PROPOSAL_EVENT)
    assert row["error"].startswith("ProposalParseError")
    assert not summary.complete


def test_a_missing_symbol_fails_before_any_proposer_call(
    tmp_path: Path, sources: tuple[Path, Path]
) -> None:
    clean, twin = sources
    manifest = Manifest(
        name="t", units=(UnitSpec(str(clean), "no_such_fn", str(twin)),)
    )
    client = FakeClient()

    with pytest.raises(E0Error, match="no_such_fn"):
        run(manifest, tmp_path / "r", client)

    assert not client.prompts and not (tmp_path / "r").exists()


def test_a_missing_twin_file_fails_before_any_proposer_call(
    tmp_path: Path, sources: tuple[Path, Path]
) -> None:
    manifest = Manifest(
        name="t",
        units=(UnitSpec(str(sources[0]), "clamp", str(tmp_path / "gone.c")),),
    )
    with pytest.raises(E0Error, match=r"gone\.c"):
        run(manifest, tmp_path / "r")


def test_a_bad_ladder_fails_before_any_proposer_call(
    tmp_path: Path, sources: tuple[Path, Path]
) -> None:
    manifest = manifest_for(sources, unwind_ladder=(2,))
    with pytest.raises(E0Error, match="ladder"):
        run(manifest, tmp_path / "r")


def test_a_missing_esbmc_binary_fails_before_any_proposer_call(
    tmp_path: Path, sources: tuple[Path, Path]
) -> None:
    client = FakeClient()
    with pytest.raises(E0Error, match="esbmc not found"):
        run_grid(
            manifest_for(sources),
            tmp_path / "r",
            client=client,
            esbmc_bin=str(tmp_path / "no-esbmc"),
        )
    assert not client.prompts


def test_usage_is_summed_where_the_provider_reports_it(
    tmp_path: Path, sources: tuple[Path, Path]
) -> None:
    summary = run(
        manifest_for(sources, recipes=("blind",), samples=2),
        tmp_path / "r",
        UsageClient(),
    )

    (recipe,) = summary.recipes
    assert (recipe.input_tokens, recipe.output_tokens) == (200, 40)
    assert recipe.cost_usd == 1.0 and recipe.calls_with_usage == 2
    assert (recipe.cache_read_tokens, recipe.cache_creation_tokens) == (14, 6)
    assert "200/40, cache 14/6 (2/2 calls)" in summary.render()
    assert rows_of(tmp_path / "r", PROPOSAL_EVENT)[0]["usage"]["input_tokens"] == 100


def test_usage_the_provider_does_not_report_reads_as_not_reported(
    tmp_path: Path, sources: tuple[Path, Path]
) -> None:
    summary = run(manifest_for(sources, recipes=("blind",), samples=1), tmp_path / "r")
    assert "not reported" in summary.render()


def test_every_summary_number_is_recomputed_from_the_rows(
    tmp_path: Path, sources: tuple[Path, Path]
) -> None:
    manifest = manifest_for(sources)
    run_dir = tmp_path / "r"
    live = run(manifest, run_dir)

    rebuilt = summary_from_run_dir(run_dir)

    assert rebuilt.to_dict() == live.to_dict()
    blind = next(r for r in rebuilt.recipes if r.recipe == "blind/v1")
    assert (blind.cells, blind.proposed_cells, blind.complete_cells) == (2, 2, 2)
    assert (blind.proposed, blind.accepted, blind.rejected) == (6, 4, 2)
    clean, twin = blind.targets["clean"], blind.targets["twin"]
    assert (clean.checked, clean.rendered, clean.count("held")) == (4, 4, 4)
    assert twin.count("violated") == 2 and twin.count("unknown") == 2
    assert clean.esbmc_time_s == 4 * 2 * STEP
    assert twin.esbmc_time_s == 2 * STEP + 2 * 3 * STEP  # a violated run; 3 rungs
    text = rebuilt.render()
    assert "| blind/v1 | 4 | 4 | 4 | 0 | 0 | 0 | 0 | 4.0 |" in text
    assert "Run complete: yes" in text and "Verdicts are bounded" in text


def test_a_property_check_that_is_missing_from_the_rows_is_reported(
    tmp_path: Path, sources: tuple[Path, Path]
) -> None:
    manifest = manifest_for(sources, recipes=("blind",), samples=1)
    run_dir = tmp_path / "r"
    run(manifest, run_dir)
    lines = events_path(run_dir).read_text().splitlines()
    events_path(run_dir).write_text("\n".join(lines[:-1]) + "\n")  # drop one check

    summary = summary_from_run_dir(run_dir)

    assert not summary.complete
    assert summary.recipes[0].missing_checks == 1
    assert "1 property checks have no row yet or ended in an ESBMC error" in (
        summary.render()
    )
    resumed = run(manifest, run_dir)  # a dropped row is simply redone
    assert resumed.complete


def test_a_cell_never_proposed_counts_against_completeness(
    tmp_path: Path, sources: tuple[Path, Path]
) -> None:
    summary = summarise(manifest_for(sources), [])

    assert not summary.complete
    assert all(r.cells == 2 and r.complete_cells == 0 for r in summary.recipes)


def test_outcomes_outside_the_columns_and_vacuous_checks_are_not_dropped() -> None:
    manifest = Manifest(
        name="m", units=(UnitSpec("a.c", "f"),), recipes=("blind",), samples=1
    )
    key = {"unit_id": "a.c::f", "recipe": "blind/v1", "sample": 0, "attempt": 1}
    accepted = [{"property_id": f"p{i}"} for i in range(3)]
    rows = [
        {"type": PROPOSAL_EVENT, **key, "accepted": accepted, "proposed": 3},
        {"type": PROPERTY_EVENT, **key, "property_id": "p0", "target": "clean",
         "outcome": "vacuous", "rendered": True, "esbmc_time_s": 1.0},
        {"type": PROPERTY_EVENT, **key, "property_id": "p1", "target": "clean",
         "outcome": "error", "rendered": False, "esbmc_time_s": 0.0},
        {"type": PROPERTY_EVENT, **key, "property_id": "p2", "target": "clean",
         "outcome": "skipped", "rendered": False},
    ]  # fmt: skip

    summary = summarise(manifest, rows)

    clean = summary.recipes[0].targets["clean"]
    assert (clean.checked, clean.rendered) == (3, 1)
    assert (clean.count("vacuous"), clean.count("error"), clean.other) == (1, 1, 1)
    assert "1 checks have an outcome outside the columns" in summary.render()


def write_manifest(tmp_path: Path, sources: tuple[Path, Path]) -> Path:
    clean, twin = sources
    path = tmp_path / "manifest.toml"
    path.write_text(
        'name = "cli"\nrecipes = ["blind"]\nsamples = 1\n'
        f'[[units]]\npath = "{clean}"\nsymbol = "clamp"\ntwin = "{twin}"\n'
    )
    return path


@pytest.fixture
def faked_tools(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"client": FakeClient(), "esbmc": FakeEsbmc()}
    real = e0.run_grid

    def fake_client(**_kwargs: Any) -> FakeClient:
        return state["client"]

    def fake_grid(*args: Any, **kwargs: Any) -> e0.Summary:
        return real(*args, **{**kwargs, "verify_for": state["esbmc"]})

    monkeypatch.setattr(e0, "ClaudeCliClient", fake_client)
    monkeypatch.setattr(e0, "run_grid", fake_grid)
    return state


def test_the_run_command_writes_the_summary_and_exits_zero(
    tmp_path: Path,
    sources: tuple[Path, Path],
    faked_tools: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest = write_manifest(tmp_path, sources)
    run_dir = tmp_path / "run"

    code = e0.main(["run", str(manifest), "--run-dir", str(run_dir)])

    assert code == 0
    captured = capsys.readouterr()
    assert "Run complete: yes" in captured.out
    assert "proposing" in captured.err
    assert (run_dir / "summary.md").read_text() == captured.out.rstrip("\n") + "\n"
    assert json.loads((run_dir / "summary.json").read_text())["complete"] is True

    assert e0.main(["summary", str(run_dir)]) == 0
    assert "Run complete: yes" in capsys.readouterr().out
    assert e0.main(["summary", str(run_dir), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["name"] == "cli"


def test_the_run_command_exits_two_while_work_remains(
    tmp_path: Path, sources: tuple[Path, Path], faked_tools: dict[str, Any]
) -> None:
    faked_tools["client"] = FakeClient(LLMError("down"))
    manifest = write_manifest(tmp_path, sources)

    assert e0.main(["run", str(manifest), "--run-dir", str(tmp_path / "r")]) == 2


def test_the_run_command_says_how_to_resume_after_an_interrupt(
    tmp_path: Path,
    sources: tuple[Path, Path],
    faked_tools: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    faked_tools["esbmc"] = FakeEsbmc(interrupt_after=1)
    manifest = write_manifest(tmp_path, sources)

    code = e0.main(["run", str(manifest), "--run-dir", str(tmp_path / "r")])

    assert code == 130
    assert "re-run the same command to resume" in capsys.readouterr().err


def test_the_commands_report_a_bad_manifest_or_run_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = e0.main(["run", str(tmp_path / "no.toml"), "--run-dir", str(tmp_path)])
    assert code == 1 and "e0: cannot read manifest" in capsys.readouterr().err

    assert e0.main(["summary", str(tmp_path)]) == 1
    assert "not an E0 run directory" in capsys.readouterr().err


def test_an_esbmc_error_is_reported_with_its_message() -> None:
    assert e0._result_detail(e0.Error(meta(), "x" * 300)) == "x" * 200
    assert e0._result_detail(Verified(meta())) is None
    assert e0._result_detail(None) is None


def test_an_esbmc_error_is_redone_on_resume_and_keeps_the_run_incomplete(
    tmp_path: Path, sources: tuple[Path, Path]
) -> None:
    manifest = manifest_for(sources, recipes=("blind",), samples=1, twin=False)
    run_dir = tmp_path / "r"

    class Crashing(FakeEsbmc):
        def __call__(self, source: Path, /) -> Any:
            real = super().__call__(source)

            def port(path: Path, /, *, unwind: int) -> EsbmcResult:
                if "result <= 100" in path.read_text():
                    return e0.Error(meta(), "esbmc killed")
                return real(path, unwind=unwind)

            return port

    first = run(manifest, run_dir, esbmc=Crashing())

    assert not first.complete
    assert first.recipes[0].targets["clean"].count("error") == 1
    assert "ended in an ESBMC error" in first.render()
    esbmc = FakeEsbmc()
    second = run(manifest, run_dir, esbmc=esbmc)
    assert second.complete
    assert len(esbmc.checks()) == 1  # only the errored check was redone
    clean = second.recipes[0].targets["clean"]
    assert (clean.count("held"), clean.count("error")) == (2, 0)


def test_a_render_failure_is_final_and_not_redone(
    tmp_path: Path, sources: tuple[Path, Path]
) -> None:
    key = {"unit_id": "a.c::f", "recipe": "blind/v1", "sample": 0, "attempt": 1}
    failed = {"type": PROPERTY_EVENT, **key, "property_id": "p", "target": "clean"}

    assert not e0._needs_retry({**failed, "outcome": "error", "rendered": False})
    assert e0._needs_retry({**failed, "outcome": "error", "rendered": True})


def test_an_unexpected_proposer_failure_is_recorded_not_fatal(
    tmp_path: Path, sources: tuple[Path, Path]
) -> None:
    summary = run(
        manifest_for(sources, recipes=("blind",), samples=1),
        tmp_path / "r",
        FakeClient(ValueError("provider blank")),
    )

    (row,) = rows_of(tmp_path / "r", PROPOSAL_EVENT)
    assert row["error"] == "ValueError: provider blank"
    assert not summary.complete


def test_a_run_directory_is_pinned_to_the_source_files_it_read(
    tmp_path: Path, sources: tuple[Path, Path]
) -> None:
    manifest = manifest_for(sources, recipes=("blind",), samples=1)
    run(manifest, tmp_path / "r")
    sources[1].write_text(TWIN_SRC.replace("return x;", "return x + 1;"))
    client = FakeClient()

    with pytest.raises(E0Error, match="different source files"):
        run(manifest, tmp_path / "r", client)

    assert not client.prompts


def test_a_second_runner_on_the_same_run_directory_is_refused(
    tmp_path: Path, sources: tuple[Path, Path]
) -> None:
    manifest = manifest_for(sources, recipes=("blind",), samples=1)
    run_dir = tmp_path / "r"
    with e0._exclusive(run_dir), pytest.raises(E0Error, match="in use"):
        run(manifest, run_dir)

    assert run(manifest, run_dir).complete  # released with the first holder


def test_the_summary_uses_the_recipe_refs_the_run_was_pinned_to(
    tmp_path: Path, sources: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    run(manifest_for(sources, recipes=("blind",), samples=1), tmp_path / "r")
    before = summary_from_run_dir(tmp_path / "r")
    bumped = dict(e0.RECIPES)
    bumped["blind"] = Recipe("blind", "2", bumped["blind"].build)
    monkeypatch.setattr(e0, "RECIPES", bumped)

    after = summary_from_run_dir(tmp_path / "r")

    assert after.recipes[0].recipe == "blind/v1"
    assert untimed(after) == untimed(before)

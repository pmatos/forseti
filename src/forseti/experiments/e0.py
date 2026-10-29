"""RFC-0007 E0: the recipe x sample proposer grid on Tier 0 (#389).

E0 measures the *machinery*, M1 (does a proposed property hold on the fixed
version) and M5 (cost). It cannot measure M2: Tier 0 has no usable bug pair.

For every cell ``(unit, recipe, sample)`` the runner asks the proposer for
properties (the recipe decides what the prompt shows), then checks each accepted
property on the clean source and, where the manifest names one, on its twin. The
run directory holds the whole experiment::

    <run-dir>/manifest.json   the manifest the run is pinned to
    <run-dir>/events.jsonl    the rows (the same trace file Core writes)
    <run-dir>/work/           the harnesses ESBMC read

Two row types go to ``events.jsonl`` through ``record_event``:

- ``e0.proposal``: one per proposer call, written *before* any check, carrying the
  accepted properties in full, so a proposal is paid for once;
- ``e0.property``: one per property x target, with the verdict, k and ESBMC time.

Every number in the summary is computed from those rows (``summarise``); the
``summary`` command rebuilds the table from a run directory alone.

Resume: re-running the same command on the same run directory skips every
proposal and every check that already has a row, so an interrupted run loses at
most the one ESBMC check that was in flight. A proposal that failed (an LLM or
parse error) is retried; its newer row supersedes the failed one. A run directory
is pinned to its manifest: a different manifest is refused. ``record_event`` is
best-effort, so a row that failed to write is simply redone on the next run and
the summary reports the missing work.

Paths in the manifest are read relative to the working directory (run from the
repository root), and the unit ids are spelt as the manifest spells them.

Vocabulary: a verdict is "no violation found up to k (and len <= max_len)", never
a proof. ``unknown`` includes ``vacuous`` (the post-call point was unreachable, so
the property was never evaluated); neither is ever read as held.

The grid (about 54 proposer calls and hours of ESBMC)::

    python -m forseti.experiments.e0 run experiments/e0/manifest.toml \\
        --run-dir runs/e0
    python -m forseti.experiments.e0 summary runs/e0
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import shutil
import sys
import time
import tomllib
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from forseti._events import events_path, record_event
from forseti.core._proposal_request import prepare_proposal_request
from forseti.esbmc import Error, EsbmcResult, Unknown
from forseti.grading.discriminate import PropertySetError, properties_from_data
from forseti.orchestrator import (
    PropertyVerdict,
    Unit,
    VerifyPort,
    check_properties,
)
from forseti.orchestrator.semantic_check import (
    DEFAULT_UNWIND,
    SemanticCheckPlan,
)
from forseti.precond import DEFAULT_MAX_LEN
from forseti.properties import (
    MAX_CANDIDATES_DEFAULT,
    RECIPES,
    ClaudeCliClient,
    LLMClient,
    LLMUsage,
    Property,
    ProposalRequest,
    RecipeError,
    propose_properties,
)
from forseti.unit_id import make_unit_id

PROPOSAL_EVENT = "e0.proposal"
PROPERTY_EVENT = "e0.property"
CLEAN = "clean"
TWIN = "twin"

DEFAULT_ESBMC_TIMEOUT_S = 30.0
DEFAULT_PROPOSER_TIMEOUT_S = 240.0
DEFAULT_MODEL = "sonnet"

_MANIFEST_FILE = "manifest.json"
_WORK_DIR = "work"
_LOCK_FILE = ".lock"
_SUMMARY_MD = "summary.md"
_SUMMARY_JSON = "summary.json"

CellKey = tuple[str, str, int]  # (unit id, recipe ref, sample)
CheckKey = tuple[CellKey, int, str, str]  # (cell, attempt, property id, target)


class E0Error(Exception):
    """A manifest, run directory or unit that the runner cannot work with."""


@dataclass(frozen=True)
class UnitSpec:
    """One manifest unit: where its function lives, its twin, and bound overrides."""

    path: str
    symbol: str
    twin: str | None = None
    unwind: int | None = None
    unwind_ladder: tuple[int, ...] | None = None
    max_len: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "symbol": self.symbol,
            "twin": self.twin,
            "unwind": self.unwind,
            "unwind_ladder": (
                None if self.unwind_ladder is None else list(self.unwind_ladder)
            ),
            "max_len": self.max_len,
        }


_FIELD_TYPES: dict[str, type | tuple[type, ...]] = {
    "name": str,
    "model": str,
    "samples": int,
    "max_candidates": int,
    "unwind": int,
    "max_len": int,
    "esbmc_timeout_s": (int, float),
    "proposer_timeout_s": (int, float),
}


def _require_ladder(value: object, where: str) -> None:
    if value is not None and (
        not isinstance(value, list)
        or not all(
            isinstance(rung, int) and not isinstance(rung, bool) for rung in value
        )
    ):
        raise E0Error(f"{where} must be a list of integers")


@dataclass(frozen=True)
class Bounds:
    unwind: int
    unwind_ladder: tuple[int, ...] | None
    max_len: int


@dataclass(frozen=True)
class Manifest:
    """The grid: units x recipes x samples, the proposer, and the ESBMC bounds.

    `unwind`/`unwind_ladder`/`max_len` are the defaults a unit may override;
    `unwind_ladder=None` derives the rungs above `unwind` (and past `max_len`).
    `esbmc_timeout_s` bounds each ESBMC invocation, not a whole property.
    """

    name: str
    units: tuple[UnitSpec, ...]
    recipes: tuple[str, ...] = tuple(RECIPES)
    samples: int = 3
    model: str = DEFAULT_MODEL
    max_candidates: int = MAX_CANDIDATES_DEFAULT
    unwind: int = DEFAULT_UNWIND
    unwind_ladder: tuple[int, ...] | None = None
    max_len: int = DEFAULT_MAX_LEN
    esbmc_timeout_s: float = DEFAULT_ESBMC_TIMEOUT_S
    proposer_timeout_s: float = DEFAULT_PROPOSER_TIMEOUT_S

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Manifest:
        """Build from decoded TOML/JSON; every problem is an `E0Error`."""
        raw = dict(data)
        raw.pop("recipe_refs", None)
        raw.pop("sources", None)
        units_raw = raw.pop("units", None)
        if not isinstance(units_raw, list) or not units_raw:
            raise E0Error("manifest needs a non-empty `units` list")
        units = tuple(_unit_from(item, index) for index, item in enumerate(units_raw))
        recipes_raw = raw.pop("recipes", list(RECIPES))
        if (
            not isinstance(recipes_raw, list)
            or not recipes_raw
            or any(name not in RECIPES for name in recipes_raw)
            or len(set(recipes_raw)) != len(recipes_raw)
        ):
            raise E0Error(
                "manifest `recipes` must be distinct registered recipes "
                f"({', '.join(RECIPES)}); got {recipes_raw!r}"
            )
        recipes = tuple(recipes_raw)
        ladder = raw.pop("unwind_ladder", None)
        _require_ladder(ladder, "manifest `unwind_ladder`")
        try:
            manifest = cls(
                units=units,
                recipes=recipes,
                unwind_ladder=None if ladder is None else tuple(ladder),
                **raw,
            )
        except TypeError as exc:
            raise E0Error(f"bad manifest field: {exc}") from exc
        manifest._validate()
        return manifest

    def _validate(self) -> None:
        for field_name, kind in _FIELD_TYPES.items():
            value = getattr(self, field_name)
            if not isinstance(value, kind) or isinstance(value, bool):
                raise E0Error(f"manifest `{field_name}` has the wrong type: {value!r}")
        if not self.name:
            raise E0Error("manifest needs a `name`")
        if self.samples < 1 or self.max_candidates < 1:
            raise E0Error("`samples` and `max_candidates` must be >= 1")
        if self.esbmc_timeout_s <= 0 or self.proposer_timeout_s <= 0:
            raise E0Error("timeouts must be > 0")
        if self.max_len < 0:
            raise E0Error("`max_len` must be >= 0")
        ids = [(u.path, u.symbol) for u in self.units]
        if len(set(ids)) != len(ids):
            raise E0Error("manifest lists the same unit twice")

    def to_dict(self) -> dict[str, Any]:
        """The canonical form pinned in the run directory."""
        return {
            "name": self.name,
            "model": self.model,
            "recipes": list(self.recipes),
            "recipe_refs": list(self.recipe_refs.values()),
            "samples": self.samples,
            "max_candidates": self.max_candidates,
            "unwind": self.unwind,
            "unwind_ladder": (
                None if self.unwind_ladder is None else list(self.unwind_ladder)
            ),
            "max_len": self.max_len,
            "esbmc_timeout_s": self.esbmc_timeout_s,
            "proposer_timeout_s": self.proposer_timeout_s,
            "units": [unit.to_dict() for unit in self.units],
        }

    @property
    def recipe_refs(self) -> dict[str, str]:
        return {name: RECIPES[name].ref for name in self.recipes}

    def bounds_for(self, unit: UnitSpec) -> Bounds:
        """The ESBMC bounds for `unit`: its overrides over the manifest's."""
        return Bounds(
            unwind=unit.unwind if unit.unwind is not None else self.unwind,
            unwind_ladder=(
                unit.unwind_ladder
                if unit.unwind_ladder is not None
                else self.unwind_ladder
            ),
            max_len=unit.max_len if unit.max_len is not None else self.max_len,
        )

    def cells(self) -> list[tuple[UnitSpec, str, int]]:
        return [
            (unit, recipe, sample)
            for unit in self.units
            for recipe in self.recipes
            for sample in range(self.samples)
        ]


def _unit_from(item: object, index: int) -> UnitSpec:
    where = f"units[{index}]"
    if not isinstance(item, Mapping):
        raise E0Error(f"{where} is not a table")
    raw = dict(item)
    path, symbol = raw.pop("path", None), raw.pop("symbol", None)
    if not isinstance(path, str) or not isinstance(symbol, str):
        raise E0Error(f"{where} needs string `path` and `symbol`")
    ladder = raw.pop("unwind_ladder", None)
    _require_ladder(ladder, f"{where}.unwind_ladder")
    try:
        unit = UnitSpec(
            path=path,
            symbol=symbol,
            unwind_ladder=None if ladder is None else tuple(ladder),
            **raw,
        )
    except TypeError as exc:
        raise E0Error(f"{where}: {exc}") from exc
    for name in ("unwind", "max_len"):
        value = getattr(unit, name)
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool)
        ):
            raise E0Error(f"{where}.{name} must be an integer")
    if unit.twin is not None and not isinstance(unit.twin, str):
        raise E0Error(f"{where}.twin must be a path")
    return unit


def load_manifest(path: Path) -> Manifest:
    """Read a TOML manifest (see `experiments/e0/manifest.toml`)."""
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise E0Error(f"cannot read manifest {path}: {exc}") from exc
    return Manifest.from_dict(data)


def read_rows(run_dir: Path) -> list[dict[str, Any]]:
    """Every E0 row of `run_dir`, in file order.

    A line that is not a JSON object (the torn last line a kill mid-append can
    leave) is skipped, never fatal.
    """
    path = events_path(run_dir)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("type") in (
            PROPOSAL_EVENT,
            PROPERTY_EVENT,
        ):
            rows.append(row)
    return rows


def _seal_trace(run_dir: Path) -> None:
    """End a torn last line, so the next appended row starts on its own line."""
    path = events_path(run_dir)
    if path.exists() and path.stat().st_size:
        with path.open("rb+") as handle:
            handle.seek(-1, 2)
            if handle.read(1) != b"\n":
                handle.write(b"\n")


def _cell_key(row: Mapping[str, Any]) -> CellKey:
    return (str(row.get("unit_id")), str(row.get("recipe")), int(row.get("sample", -1)))


def _check_key(row: Mapping[str, Any]) -> CheckKey:
    return (
        _cell_key(row),
        int(row.get("attempt", 0)),
        str(row.get("property_id")),
        str(row.get("target")),
    )


def _needs_retry(row: Mapping[str, Any]) -> bool:
    """An ESBMC invocation error on a rendered harness: possibly transient."""
    return row.get("outcome") == "error" and bool(row.get("rendered"))


@dataclass
class _State:
    """What the rows already say: the latest proposal per cell, and the checks done."""

    proposals: dict[CellKey, dict[str, Any]] = field(default_factory=dict)
    checks: set[CheckKey] = field(default_factory=set)

    @classmethod
    def from_rows(cls, rows: Sequence[dict[str, Any]]) -> _State:
        state = cls()
        for row in rows:
            state.add(row)
        return state

    def add(self, row: dict[str, Any]) -> None:
        if row["type"] == PROPOSAL_EVENT:
            self.proposals[_cell_key(row)] = row
        elif _needs_retry(row):
            self.checks.discard(_check_key(row))
        else:
            self.checks.add(_check_key(row))


class _MeteredClient:
    """An `LLMClient` that remembers the usage its last completion reported."""

    def __init__(self, inner: LLMClient) -> None:
        self._inner = inner
        self.usage: LLMUsage | None = None

    @property
    def provider(self) -> str:
        return self._inner.provider

    @property
    def model(self) -> str:
        return self._inner.model

    def complete(self, prompt: str) -> str:
        with_usage = getattr(self._inner, "complete_with_usage", None)
        if with_usage is None:
            return self._inner.complete(prompt)
        text, self.usage = with_usage(prompt)
        return text


class _TimedVerify:
    """A `VerifyPort` summing the ESBMC time of every run it forwards."""

    def __init__(self, port: VerifyPort) -> None:
        self._port = port
        self.seconds = 0.0
        self.runs = 0

    def __call__(self, source: Path, /, *, unwind: int) -> EsbmcResult:
        result = self._port(source, unwind=unwind)
        self.seconds += result.meta.duration_s
        self.runs += 1
        return result


class _OneProperty:
    """A `PropertyStorePort` over one property, whatever the unit id (a twin's)."""

    def __init__(self, prop: Property) -> None:
        self._prop = prop

    def list_for_unit(
        self, unit_id: str, statuses: object = None
    ) -> Sequence[Property]:
        return (self._prop,)


@dataclass(frozen=True)
class _Target:
    name: str
    unit: Unit
    bounds: Bounds
    plan: SemanticCheckPlan


@dataclass(frozen=True)
class _Prepared:
    """A manifest unit with its files read once and its requests built."""

    spec: UnitSpec
    unit_id: str
    targets: tuple[_Target, ...]
    requests: dict[str, ProposalRequest]  # by recipe name


def _prepare(
    manifest: Manifest,
    *,
    esbmc_bin: str,
    verify_for: Callable[[Path], VerifyPort] | None,
) -> list[_Prepared]:
    """Read every file and build every request before the first (paid) call."""
    prepared = []
    for spec in manifest.units:
        bounds = manifest.bounds_for(spec)
        try:
            plan = SemanticCheckPlan(
                unwind=bounds.unwind,
                unwind_ladder=bounds.unwind_ladder,
                max_len=bounds.max_len,
                timeout_s=manifest.esbmc_timeout_s,
                esbmc_bin=esbmc_bin,
                verify_for=verify_for,
            )
            plan.validate_ladder()
            clean = Unit.from_path(Path(spec.path), spec.symbol)
            targets = [_Target(CLEAN, clean, bounds, plan)]
            if spec.twin is not None:
                twin = Unit.from_path(Path(spec.twin), spec.symbol)
                targets.append(_Target(TWIN, twin, bounds, plan))
            requests = {
                recipe: prepare_proposal_request(
                    Path(spec.path), function=spec.symbol, recipe=recipe
                )
                for recipe in manifest.recipes
            }
        except (OSError, RecipeError, ValueError) as exc:
            raise E0Error(f"unit {spec.path}::{spec.symbol}: {exc}") from exc
        prepared.append(_Prepared(spec, clean.unit_id, tuple(targets), requests))
    return prepared


def _propose(
    manifest: Manifest,
    unit: _Prepared,
    recipe: str,
    sample: int,
    *,
    attempt: int,
    client: LLMClient,
    run_dir: Path,
) -> dict[str, Any]:
    request = unit.requests[recipe]
    metered = _MeteredClient(client)
    error: str | None = None
    accepted: tuple[Property, ...] = ()
    rejected: list[dict[str, object]] = []
    started = time.monotonic()
    try:
        result = propose_properties(
            request,
            client=metered,
            store=None,
            max_candidates=manifest.max_candidates,
        )
        accepted = result.accepted
        rejected = [rej.to_dict() for rej in result.rejected]
    except Exception as exc:  # the paid reply is lost either way: record, retry
        error = f"{type(exc).__name__}: {exc}"
    latency = time.monotonic() - started
    row: dict[str, Any] = {
        "run": manifest.name,
        "unit_id": unit.unit_id,
        "symbol": unit.spec.symbol,
        "recipe": manifest.recipe_refs[recipe],
        "sample": sample,
        "attempt": attempt,
        "prompt": request.prompt.ref,
        "provider": metered.provider,
        "model": metered.model,
        "latency_s": latency,
        "usage": None if metered.usage is None else metered.usage.to_dict(),
        "proposed": len(accepted) + len(rejected),
        "accepted": [prop.to_dict() for prop in accepted],
        "rejected": rejected,
        "error": error,
    }
    record_event(run_dir, PROPOSAL_EVENT, **row)
    return {"type": PROPOSAL_EVENT, **row}


def _result_detail(result: EsbmcResult | None) -> str | None:
    if isinstance(result, Unknown):
        return result.reason.value
    if isinstance(result, Error):
        return result.message[:200]
    return None


def _check(
    manifest: Manifest,
    unit: _Prepared,
    target: _Target,
    prop: Property,
    proposal: Mapping[str, Any],
    *,
    work_dir: Path,
    run_dir: Path,
) -> dict[str, Any]:
    timed = _TimedVerify(target.plan.verify_for(Path(target.unit.path)))
    run = check_properties(
        target.unit,
        store=_OneProperty(prop),
        render=target.plan.writer,
        verify=timed,
        work_dir=work_dir / target.name,
        unwind=target.bounds.unwind,
        unwind_ladder=target.plan.ladder,
    )
    (verdict,) = run.verdicts
    row = _property_row(manifest, unit, target, prop, proposal, verdict, timed)
    record_event(run_dir, PROPERTY_EVENT, **row)
    return {"type": PROPERTY_EVENT, **row}


def _property_row(
    manifest: Manifest,
    unit: _Prepared,
    target: _Target,
    prop: Property,
    proposal: Mapping[str, Any],
    verdict: PropertyVerdict,
    timed: _TimedVerify,
) -> dict[str, Any]:
    result = verdict.result
    return {
        "run": manifest.name,
        "unit_id": unit.unit_id,
        "recipe": proposal["recipe"],
        "sample": proposal["sample"],
        "attempt": proposal["attempt"],
        "prompt": proposal["prompt"],
        "model": proposal["model"],
        "target": target.name,
        "target_path": str(target.unit.path),
        "property_id": prop.property_id,
        "kind": prop.kind.value,
        "expression": prop.expression,
        "domain": list(prop.domain),
        "rendered": verdict.harness_source is not None,
        "outcome": verdict.outcome.value,
        "k": verdict.k,
        "esbmc_time_s": timed.seconds,
        "esbmc_runs": timed.runs,
        "detail": _result_detail(result) or verdict.skip_reason,
        "probe": verdict.probe.value if verdict.probe is not None else None,
        "length_bounds": dict(verdict.length_bounds),
        "esbmc_version": result.meta.esbmc_version if result is not None else None,
        "unwind": target.bounds.unwind,
        "ladder": list(target.plan.ladder),
        "max_len": target.bounds.max_len,
        "esbmc_timeout_s": manifest.esbmc_timeout_s,
    }


def _source_hashes(prepared: Sequence[_Prepared]) -> dict[str, str]:
    return {
        str(target.unit.path): hashlib.sha256(
            target.unit.source_text.encode()
        ).hexdigest()
        for unit in prepared
        for target in unit.targets
    }


@contextmanager
def _exclusive(run_dir: Path) -> Generator[None]:
    """Hold the run directory's lock; a second runner on it is refused."""
    run_dir.mkdir(parents=True, exist_ok=True)
    with (run_dir / _LOCK_FILE).open("w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise E0Error(f"{run_dir} is in use by another run") from None
        yield


def _pin_manifest(
    run_dir: Path, manifest: Manifest, sources: Mapping[str, str]
) -> None:
    """Write the manifest and the source hashes on first use; refuse a run
    directory pinned to a different manifest or to different source files."""
    path = run_dir / _MANIFEST_FILE
    wanted = {**manifest.to_dict(), "sources": dict(sources)}
    if path.exists():
        try:
            pinned = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise E0Error(f"cannot read {path}: {exc}") from exc
        if pinned != wanted:
            raise E0Error(
                f"{run_dir} was started with a different manifest or different "
                f"source files ({path}); resume unchanged, or use a new --run-dir"
            )
        return
    path.write_text(json.dumps(wanted, indent=2) + "\n", encoding="utf-8")


def _accepted(proposal: Mapping[str, Any], unit_id: str) -> tuple[Property, ...]:
    try:
        return properties_from_data(
            {"accepted": proposal.get("accepted", [])}, unit_id=unit_id
        )
    except PropertySetError as exc:
        raise E0Error(f"corrupt proposal row for {unit_id}: {exc}") from exc


def run_grid(
    manifest: Manifest,
    run_dir: Path,
    *,
    client: LLMClient,
    esbmc_bin: str = "esbmc",
    verify_for: Callable[[Path], VerifyPort] | None = None,
    progress: Callable[[str], None] | None = None,
) -> Summary:
    """Run (or resume) the grid in `run_dir`; return the summary read from its rows.

    Everything that can be checked without a paid call is checked first: the
    ESBMC binary (unless `verify_for` replaces it), every file, every symbol,
    every recipe. `client` and `verify_for` are the proposer and verifier seams.
    """
    say = progress or (lambda _message: None)
    if verify_for is None and shutil.which(esbmc_bin) is None:
        raise E0Error(f"esbmc not found: {esbmc_bin}")
    prepared = _prepare(manifest, esbmc_bin=esbmc_bin, verify_for=verify_for)
    with _exclusive(run_dir):
        _pin_manifest(run_dir, manifest, _source_hashes(prepared))
        _run_cells(manifest, prepared, run_dir, client, say)
    return summarise(manifest, read_rows(run_dir))


def _run_cells(
    manifest: Manifest,
    prepared: Sequence[_Prepared],
    run_dir: Path,
    client: LLMClient,
    say: Callable[[str], None],
) -> None:
    _seal_trace(run_dir)
    state = _State.from_rows(read_rows(run_dir))
    total = len(manifest.cells())
    done = 0
    for unit in prepared:
        for recipe in manifest.recipes:
            ref = manifest.recipe_refs[recipe]
            for sample in range(manifest.samples):
                done += 1
                key = (unit.unit_id, ref, sample)
                label = f"[{done}/{total}] {unit.unit_id} {ref} sample {sample}"
                proposal = state.proposals.get(key)
                if proposal is None or proposal.get("error"):
                    attempt = 1 if proposal is None else int(proposal["attempt"]) + 1
                    say(f"{label}: proposing")
                    proposal = _propose(
                        manifest,
                        unit,
                        recipe,
                        sample,
                        attempt=attempt,
                        client=client,
                        run_dir=run_dir,
                    )
                    state.add(proposal)
                    if proposal["error"]:
                        say(f"{label}: proposal failed: {proposal['error']}")
                for prop in _accepted(proposal, unit.unit_id):
                    for target in unit.targets:
                        check = (
                            key,
                            proposal["attempt"],
                            prop.property_id,
                            target.name,
                        )
                        if check in state.checks:
                            continue
                        say(f"{label}: {target.name} {prop.expression}")
                        state.add(
                            _check(
                                manifest,
                                unit,
                                target,
                                prop,
                                proposal,
                                work_dir=run_dir / _WORK_DIR,
                                run_dir=run_dir,
                            )
                        )


_OUTCOMES = ("held", "violated", "unknown", "vacuous", "error")


@dataclass
class TargetTally:
    """The checks of one target (clean or twin), by outcome."""

    checked: int = 0
    rendered: int = 0
    outcomes: dict[str, int] = field(default_factory=dict)
    esbmc_time_s: float = 0.0

    def add(self, row: Mapping[str, Any]) -> None:
        self.checked += 1
        self.rendered += bool(row.get("rendered"))
        outcome = str(row.get("outcome"))
        self.outcomes[outcome] = self.outcomes.get(outcome, 0) + 1
        self.esbmc_time_s += float(row.get("esbmc_time_s") or 0.0)

    def count(self, outcome: str) -> int:
        return self.outcomes.get(outcome, 0)

    @property
    def other(self) -> int:
        return self.checked - sum(self.count(name) for name in _OUTCOMES)

    def to_dict(self) -> dict[str, Any]:
        return {
            "checked": self.checked,
            "rendered": self.rendered,
            **{name: self.count(name) for name in _OUTCOMES},
            "other": self.other,
            "esbmc_time_s": self.esbmc_time_s,
        }


@dataclass
class RecipeSummary:
    """One recipe's cells, proposals and checks.

    Proposer cost (`attempts`, latency, usage) sums every call, failed ones
    included; proposed/accepted/rejected and the checks read each cell's latest
    successful proposal only.
    """

    recipe: str
    cells: int
    proposed_cells: int = 0
    complete_cells: int = 0
    proposal_errors: int = 0
    attempts: int = 0
    proposed: int = 0
    accepted: int = 0
    rejected: int = 0
    missing_checks: int = 0
    proposer_latency_s: float = 0.0
    calls_with_usage: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_usd: float = 0.0
    targets: dict[str, TargetTally] = field(
        default_factory=lambda: {CLEAN: TargetTally(), TWIN: TargetTally()}
    )

    def add_usage(self, usage: Mapping[str, Any] | None) -> None:
        if not usage:
            return
        self.calls_with_usage += 1
        self.input_tokens += int(usage.get("input_tokens") or 0)
        self.output_tokens += int(usage.get("output_tokens") or 0)
        self.cache_read_tokens += int(usage.get("cache_read_tokens") or 0)
        self.cache_creation_tokens += int(usage.get("cache_creation_tokens") or 0)
        self.cost_usd += float(usage.get("cost_usd") or 0.0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "recipe": self.recipe,
            "cells": self.cells,
            "proposed_cells": self.proposed_cells,
            "complete_cells": self.complete_cells,
            "proposal_errors": self.proposal_errors,
            "attempts": self.attempts,
            "proposed": self.proposed,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "missing_checks": self.missing_checks,
            "proposer_latency_s": self.proposer_latency_s,
            "calls_with_usage": self.calls_with_usage,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
            "cost_usd": self.cost_usd,
            "targets": {name: tally.to_dict() for name, tally in self.targets.items()},
        }


@dataclass(frozen=True)
class Summary:
    name: str
    recipes: tuple[RecipeSummary, ...]

    @property
    def complete(self) -> bool:
        """Every cell proposed without error and every check done."""
        return all(r.complete_cells == r.cells for r in self.recipes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "complete": self.complete,
            "recipes": [recipe.to_dict() for recipe in self.recipes],
        }

    def render(self) -> str:
        """Markdown tables: the clean version (M1, M5), the twin, the proposer."""

        def table(header: Sequence[str], body: Sequence[Sequence[object]]) -> list[str]:
            lines = ["| " + " | ".join(header) + " |"]
            lines.append("|" + "|".join("---" for _ in header) + "|")
            lines += ["| " + " | ".join(str(c) for c in row) + " |" for row in body]
            return lines

        def checks(target: str) -> list[list[object]]:
            return [
                [
                    r.recipe,
                    r.targets[target].checked,
                    r.targets[target].rendered,
                    *(r.targets[target].count(name) for name in _OUTCOMES),
                    f"{r.targets[target].esbmc_time_s:.1f}",
                ]
                for r in self.recipes
            ]

        check_header = ["recipe", "checked", "rendered", *_OUTCOMES, "ESBMC s"]
        proposer_rows = [
            [
                r.recipe,
                f"{r.complete_cells}/{r.cells}",
                r.proposal_errors,
                r.attempts,
                r.proposed,
                r.accepted,
                r.rejected,
                f"{r.proposer_latency_s:.1f}",
                (
                    f"{r.input_tokens}/{r.output_tokens}, cache "
                    f"{r.cache_read_tokens}/{r.cache_creation_tokens} "
                    f"({r.calls_with_usage}/{r.attempts} calls)"
                    if r.calls_with_usage
                    else "not reported"
                ),
                f"{r.cost_usd:.2f}" if r.calls_with_usage else "-",
            ]
            for r in self.recipes
        ]
        lines = [
            f"# E0 summary: {self.name}",
            "",
            f"Run complete: {'yes' if self.complete else 'NO (resume the run)'}",
            "",
            "## Properties checked on the clean version (M1)",
            "",
            *table(check_header, checks(CLEAN)),
            "",
            "## Properties checked on the twin",
            "",
            *table(check_header, checks(TWIN)),
            "",
            "## Proposer (M5)",
            "",
            *table(
                [
                    "recipe",
                    "cells done",
                    "failed",
                    "calls",
                    "proposed",
                    "accepted",
                    "rejected",
                    "latency s",
                    "tokens in/out, cache read/write",
                    "cost $",
                ],
                proposer_rows,
            ),
            "",
            "Verdicts are bounded: no violation found up to k (and len <= max_len). "
            "`unknown` and `vacuous` are never held. `rendered` counts checks whose "
            "harness was built.",
        ]
        missing = sum(r.missing_checks for r in self.recipes)
        if missing:
            lines.append(
                f"{missing} property checks have no row yet or ended in an ESBMC "
                "error (a resume redoes them)."
            )
        other = sum(t.other for r in self.recipes for t in r.targets.values())
        if other:
            lines.append(f"{other} checks have an outcome outside the columns above.")
        return "\n".join(lines) + "\n"


def summarise(
    manifest: Manifest,
    rows: Sequence[Mapping[str, Any]],
    refs: Mapping[str, str] | None = None,
) -> Summary:
    """The summary of `rows` for `manifest`; the only source of every number.

    `refs` maps recipe names to the refs the rows carry (a run directory pins
    them); it defaults to the registry's. A check that ended in an ESBMC error is
    tallied but still counts as work remaining, since a resume redoes it.
    """
    refs = refs or manifest.recipe_refs
    proposals: dict[CellKey, list[Mapping[str, Any]]] = {}
    checks: dict[CheckKey, Mapping[str, Any]] = {}
    for row in rows:
        if row["type"] == PROPOSAL_EVENT:
            proposals.setdefault(_cell_key(row), []).append(row)
        else:
            checks[_check_key(row)] = row

    summaries = []
    for recipe in manifest.recipes:
        ref = refs[recipe]
        summary = RecipeSummary(ref, cells=len(manifest.units) * manifest.samples)
        for spec in manifest.units:
            unit_id = make_unit_id(Path(spec.path), spec.symbol)
            names = (CLEAN, TWIN) if spec.twin is not None else (CLEAN,)
            for sample in range(manifest.samples):
                key = (unit_id, ref, sample)
                calls = proposals.get(key, [])
                for call in calls:
                    summary.attempts += 1
                    summary.proposer_latency_s += float(call.get("latency_s") or 0.0)
                    summary.add_usage(call.get("usage"))
                if not calls:
                    continue
                latest = calls[-1]
                if latest.get("error"):
                    summary.proposal_errors += 1
                    continue
                summary.proposed_cells += 1
                accepted = latest.get("accepted") or []
                summary.proposed += int(latest.get("proposed") or 0)
                summary.accepted += len(accepted)
                summary.rejected += len(latest.get("rejected") or [])
                missing = 0
                for prop in accepted:
                    for name in names:
                        row = checks.get(
                            (
                                key,
                                int(latest["attempt"]),
                                str(prop["property_id"]),
                                name,
                            )
                        )
                        if row is None or _needs_retry(row):
                            missing += 1
                        if row is not None:
                            summary.targets[name].add(row)
                summary.missing_checks += missing
                summary.complete_cells += missing == 0
        summaries.append(summary)
    return Summary(manifest.name, tuple(summaries))


def summary_from_run_dir(run_dir: Path) -> Summary:
    """Rebuild the summary of an existing run directory from its pinned manifest
    and its rows alone."""
    path = run_dir / _MANIFEST_FILE
    try:
        pinned = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise E0Error(f"{run_dir} is not an E0 run directory: {exc}") from exc
    manifest = Manifest.from_dict(pinned)
    refs = dict(zip(manifest.recipes, pinned.get("recipe_refs", ()), strict=False))
    return summarise(manifest, read_rows(run_dir), refs or None)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m forseti.experiments.e0",
        description="RFC-0007 E0: the recipe x sample proposer grid on Tier 0.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser(
        "run",
        help="run the grid, or resume it",
        description=(
            "Run the manifest's grid into --run-dir. Re-running on the same "
            "directory skips every proposal and check that already has a row. "
            "Exit 0 when every cell is complete, 2 when work remains (resume), "
            "1 on a failed run."
        ),
    )
    run.add_argument("manifest", type=Path)
    run.add_argument("--run-dir", type=Path, required=True, metavar="DIR")
    run.add_argument("--claude-bin", default="claude")
    run.add_argument("--esbmc-bin", default="esbmc")
    summary = sub.add_parser(
        "summary", help="print the summary table of a run directory, from its rows"
    )
    summary.add_argument("run_dir", type=Path)
    summary.add_argument("--json", action="store_true")
    return parser


def _progress(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _run(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.manifest)
    client = ClaudeCliClient(
        model=manifest.model,
        claude_bin=args.claude_bin,
        timeout_s=manifest.proposer_timeout_s,
    )
    summary = run_grid(
        manifest,
        args.run_dir,
        client=client,
        esbmc_bin=args.esbmc_bin,
        progress=_progress,
    )
    (args.run_dir / _SUMMARY_MD).write_text(summary.render(), encoding="utf-8")
    (args.run_dir / _SUMMARY_JSON).write_text(
        json.dumps(summary.to_dict(), indent=2) + "\n", encoding="utf-8"
    )
    print(summary.render())
    return 0 if summary.complete else 2


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "run":
            return _run(args)
        summary = summary_from_run_dir(args.run_dir)
    except E0Error as exc:
        print(f"e0: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("e0: interrupted; re-run the same command to resume", file=sys.stderr)
        return 130
    print(json.dumps(summary.to_dict(), indent=2) if args.json else summary.render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

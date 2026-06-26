"""The report-to-human payload for a stopped loop (roadmap Risk 2).

When the loop stops — converged, halted, or gave up — a human needs enough
context to act: the last counterexample, the per-iteration history (the verdict,
the bound `k` it ran at, and the source verified that pass), and *why* it gave
up. `Report` is that payload and `report_for` is the pure projection that builds
it from a `LoopRun`. ESBMC emits no proof object, so this carries a *verdict*
trail, never a "proof".

`report_for` is total over every terminal state (DONE / UNKNOWN / GIVE_UP), so a
human gets context even for an inconclusive UNKNOWN halt. How the payload is
*presented* in a harness is #14; the streaming trace is #6 — this is the single
end-of-run snapshot.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from forseti.esbmc import Counterexample, Unknown, Violated

from .loop import LoopRun
from .state import GiveUpReason, LoopState


@dataclass(frozen=True)
class IterationReport:
    """One serializable row of the per-iteration history.

    `source` is the path verified that pass (the path-level fix handle; a
    structured diff ref arrives with #28). `k` is the unwind bound the pass
    actually ran at, read back from the esbmc argv. `unknown_reason` is the
    `UnknownReason` (e.g. `"timeout"`) when the pass was `Unknown`, else `None` —
    so a human can tell *why* the loop escalated.
    """

    index: int
    verdict: str
    k: int | None
    source: str
    unknown_reason: str | None


@dataclass(frozen=True)
class Report:
    """The report-to-human payload for a stopped loop.

    `last_counterexample` is the typed cex of the last `Violated` pass (`None`
    if the run never violated or parsing had failed); `last_counterexample_raw`
    is that pass's raw esbmc text — a lossless fallback so a human still gets the
    trace even when typed parsing returned `None`.
    """

    final_state: LoopState
    give_up_reason: GiveUpReason | None
    iterations: tuple[IterationReport, ...]
    last_counterexample: Counterexample | None
    last_counterexample_raw: str | None

    def to_dict(self) -> dict[str, Any]:
        """A JSON-serializable dict (for a harness / the result cache).

        Built by hand rather than via `asdict(self)`: the enum fields and the
        nested `Counterexample` would otherwise stay as objects `json.dumps`
        rejects. `IterationReport` is all `str | int | None`, so `asdict` is
        safe for the rows.
        """
        return {
            "final_state": self.final_state.value,
            "give_up_reason": (
                self.give_up_reason.value if self.give_up_reason is not None else None
            ),
            "iterations": [asdict(it) for it in self.iterations],
            "last_counterexample": (
                self.last_counterexample.to_dict()
                if self.last_counterexample is not None
                else None
            ),
            "last_counterexample_raw": self.last_counterexample_raw,
        }


def _unwind_from_argv(argv: tuple[str, ...]) -> int | None:
    """The unwind bound `k` the run actually used, read from the esbmc argv.

    Bounds- and parse-safe — never raises: returns `None` when `--unwind` is
    absent, is the last token (no value follows), or is followed by a non-int.
    """
    if "--unwind" not in argv:
        return None
    i = argv.index("--unwind")
    if i + 1 >= len(argv):
        return None
    try:
        return int(argv[i + 1])
    except ValueError:
        return None


def report_for(run: LoopRun) -> Report:
    """Project a `LoopRun` into the report-to-human payload (pure, total)."""
    iterations = tuple(
        IterationReport(
            index=it.index,
            verdict=it.result.verdict.value,
            k=_unwind_from_argv(it.result.meta.argv),
            source=str(it.source),
            unknown_reason=(
                it.result.reason.value if isinstance(it.result, Unknown) else None
            ),
        )
        for it in run.iterations
    )
    last_cex: Counterexample | None = None
    last_cex_raw: str | None = None
    for it in reversed(run.iterations):
        if isinstance(it.result, Violated):
            last_cex = it.result.counterexample
            last_cex_raw = it.result.raw_counterexample
            break
    return Report(
        final_state=run.final_state,
        give_up_reason=run.give_up_reason,
        iterations=iterations,
        last_counterexample=last_cex,
        last_counterexample_raw=last_cex_raw,
    )

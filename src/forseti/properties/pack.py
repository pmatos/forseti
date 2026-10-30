"""Context-pack builder: a ranked, token-budgeted view of the project around one unit.

The proposer-input half of RFC-0007 T1. `build_context_pack` is a deterministic
function from ``(project index, unit id, config)`` to a `ContextPack`: the unit's
target text and the slices that explain how it is used, each ranked and cut to fit a
token budget. Given the same tree it returns the same bytes every run, and because the
index reads one commit (`GitTree`) nothing written after it can leak in. The one input
that is not from the tree is the `contracts` seam: whatever it returns is shown, so the
caller owns its temporal validity (`store_contracts` reads the store as it is now, and
keys by the tree-relative path, which may differ from the spelling in stored unit ids).

Slices, in the order the budget is spent:

- ``target``: the function and its doc comment, or (``signature``) without the body,
  for a blind pack. When selected it is never dropped for the budget. A blind pack
  takes its types from the signature alone; callees are read from the body either way.
- ``types``: struct/union/enum/typedef/macro definitions the target uses, to
  `PackConfig.type_depth` levels of nesting.
- ``callees``: signature and doc comment of each function the target calls, plus any
  accepted contract the `contracts` seam knows for it. Never bodies.
- ``callers``: the lines around each call site, within the calling function. A
  ``static`` function in a ``.c`` file is file-local, so only its own file is searched,
  and another file's own function of the same name is not a caller.
- ``tests``: functions in test files that call the target.
- ``siblings``: signature and doc comment of other functions in the unit's file.

``main`` is skipped as a caller by default (`PackConfig.skip_callers`): in this
repository's ``examples/`` it is the harness whose asserts are the target properties,
the leak RFC-0007 ground rule 1 forbids.

Budget accounting is a deterministic estimate (`estimate_tokens`, four characters a
token), not a tokenizer, so a pack does not depend on a model. An entry that does not
fit is skipped and later, smaller ones may still be taken. The caller, test and
sibling slices are also capped in count, so one slice cannot starve the rest.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from forseti.unit_id import make_unit_id, unit_id_path, unit_id_symbol

from .index import (
    FunctionRef,
    ProjectIndex,
    ProjectIndexError,
    Reach,
    is_test_path,
    line_of,
)
from .model import PropertyStatus
from .recipes import ProposalContext

if TYPE_CHECKING:
    from .store import PropertyStore

Contracts = Callable[[str], Sequence[str]]


class SliceKind(StrEnum):
    """A pack slice. Declaration order is the order the budget is spent in."""

    TARGET = "target"
    SIGNATURE = "signature"
    TYPES = "types"
    CALLEES = "callees"
    CALLERS = "callers"
    TESTS = "tests"
    SIBLINGS = "siblings"


ALL_SLICES: frozenset[SliceKind] = frozenset(SliceKind) - {SliceKind.SIGNATURE}


@dataclass(frozen=True)
class PackConfig:
    """The knobs of one pack. The defaults are unmeasured starting points.

    `reach` and the caps are the open parameters of RFC-0007 question 1; a recipe
    fixes them, and its version changes when they do.
    """

    slices: Collection[SliceKind] = ALL_SLICES
    reach: Reach = Reach.PROJECT
    max_tokens: int = 4000
    type_depth: int = 3
    caller_context_lines: int = 3
    test_max_lines: int = 40
    max_callees: int = 8
    max_callers: int = 5
    max_tests: int = 3
    max_siblings: int = 5
    skip_callers: Collection[str] = ("main",)


@dataclass(frozen=True)
class PackEntry:
    """One item of a slice: a ``//`` label line and the text it labels."""

    kind: SliceKind
    label: str
    text: str

    def render(self) -> str:
        return f"// {self.label}\n{self.text.strip(chr(10))}\n"


_HEADERS = {
    SliceKind.TARGET: "target",
    SliceKind.SIGNATURE: "target (signature only)",
    SliceKind.TYPES: "types and macros it uses",
    SliceKind.CALLEES: "functions it calls (signatures, not bodies)",
    SliceKind.CALLERS: "how it is called",
    SliceKind.TESTS: "tests that call it",
    SliceKind.SIBLINGS: "other functions in the same file",
}


def estimate_tokens(text: str) -> int:
    """A model-free token estimate: four characters a token, rounded up."""
    return -(-len(text) // 4)


def _section(kind: SliceKind, entries: Sequence[PackEntry]) -> str:
    return f"// === {_HEADERS[kind]} ===\n" + "".join(e.render() for e in entries)


@dataclass(frozen=True)
class ContextPack:
    """The selected entries, in render order, and how many the budget dropped."""

    unit_id: str
    entries: tuple[PackEntry, ...]
    omitted: int = 0

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.text)

    @property
    def text(self) -> str:
        sections = [
            _section(kind, [e for e in self.entries if e.kind is kind])
            for kind in SliceKind
            if any(e.kind is kind for e in self.entries)
        ]
        return "\n".join(sections)

    def to_context(self, recipe: str) -> ProposalContext:
        """The pack as the text a recipe hands the proposer, labelled `recipe`."""
        return ProposalContext(recipe=recipe, text=self.text)


def store_contracts(store: PropertyStore) -> Contracts:
    """Accepted properties of a unit, as one-line contract strings, for callees."""

    def contracts(unit_id: str) -> Sequence[str]:
        accepted = store.list_for_unit(unit_id, {PropertyStatus.ACCEPTED})
        return [
            f"{p.expression} (given {'; '.join(p.domain)})"
            if p.domain
            else p.expression
            for p in accepted
        ]

    return contracts


def _one_line(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _normalise(path: str) -> str:
    return posixpath.normpath(path).removeprefix("./")


def _signature(index: ProjectIndex, ref: FunctionRef) -> str:
    """Doc comment and signature of `ref`, with a prototype in place of a body."""
    scan = index.scan(ref.path)
    source = index.source(ref.path)
    start = scan.doc_start(ref.start)
    if ref.brace is None:
        return source[start : ref.end]
    return source[start : ref.brace].rstrip() + ";"


def _target_entries(
    index: ProjectIndex, unit_id: str, ref: FunctionRef, kind: SliceKind
) -> list[PackEntry]:
    scan = index.scan(ref.path)
    source = index.source(ref.path)
    if kind is SliceKind.SIGNATURE:
        text = _signature(index, ref)
    else:
        text = source[scan.doc_start(ref.start) : ref.end]
    return [PackEntry(kind, unit_id, text)]


def _type_entries(
    index: ProjectIndex, ref: FunctionRef, config: PackConfig, with_body: bool
) -> list[PackEntry]:
    code = index.scan(ref.path).code
    end = ref.end if with_body else ref.brace or ref.end
    found = index.definitions_used(
        code[ref.start : end], ref.path, config.reach, config.type_depth
    )
    return [PackEntry(SliceKind.TYPES, path, d.text) for path, d in found]


def _callee_entries(
    index: ProjectIndex,
    ref: FunctionRef,
    config: PackConfig,
    contracts: Contracts | None,
) -> list[PackEntry]:
    entries: list[PackEntry] = []
    for name in index.callees(ref):
        if len(entries) == config.max_callees:
            break
        callee = index.find(name, ref.path, config.reach)
        if callee is None:
            continue
        unit = make_unit_id(callee.path, name)
        known = contracts(unit) if contracts is not None else ()
        lines = [_signature(index, callee)]
        lines += [f"// accepted: {_one_line(c)}" for c in known]
        entries.append(PackEntry(SliceKind.CALLEES, unit, "\n".join(lines)))
    return entries


def _windows(lines: list[int], radius: int, first: int, last: int) -> list[range]:
    merged: list[list[int]] = []
    for line in sorted(set(lines)):
        lo, hi = max(line - radius, first), min(line + radius, last)
        if merged and lo <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    return [range(lo, hi + 1) for lo, hi in merged]


def _caller_text(
    index: ProjectIndex, caller: FunctionRef, call_lines: list[int], radius: int
) -> str:
    source = index.source(caller.path)
    rows = source.split("\n")
    first, last = line_of(source, caller.start), line_of(source, caller.end - 1)
    windows = _windows(call_lines, radius, first, last)
    return "\n// ...\n".join("\n".join(rows[n - 1] for n in w) for w in windows)


def _call_entries(
    index: ProjectIndex,
    target: FunctionRef,
    config: PackConfig,
    kind: SliceKind,
) -> list[PackEntry]:
    """Callers outside test files (`CALLERS`) or functions inside them (`TESTS`)."""
    want_tests = kind is SliceKind.TESTS
    skipped = set(config.skip_callers)
    found: dict[tuple[str, str], list[int]] = {}
    reach = config.reach
    if not want_tests and index.is_file_local(target):
        reach = Reach.FILE
    for path in index.files_in_reach(target.path, reach):
        if is_test_path(path) is not want_tests or (
            path != target.path and index.has_local(path, target.name)
        ):
            continue
        for site in index.call_sites(path, target.name):
            own = (path, site.caller) == (target.path, target.name)
            if not own and (want_tests or site.caller not in skipped):
                found.setdefault((path, site.caller), []).append(site.line)
    limit = config.max_tests if want_tests else config.max_callers
    entries = []
    for (path, name), lines in sorted(
        found.items(), key=lambda kv: (kv[0][0] != target.path, kv[0], kv[1][0])
    )[:limit]:
        caller = index.definition(path, name)
        assert caller is not None
        if want_tests:
            text = _test_text(index, caller, config.test_max_lines)
        else:
            text = _caller_text(index, caller, lines, config.caller_context_lines)
        entries.append(PackEntry(kind, make_unit_id(path, name), text))
    return entries


def _test_text(index: ProjectIndex, test: FunctionRef, max_lines: int) -> str:
    start = index.scan(test.path).doc_start(test.start)
    lines = index.source(test.path)[start : test.end].split("\n")
    if len(lines) > max_lines:
        lines = [*lines[:max_lines], "// ... (truncated)"]
    return "\n".join(lines)


def _sibling_entries(
    index: ProjectIndex,
    target: FunctionRef,
    config: PackConfig,
    shown: set[str],
) -> list[PackEntry]:
    siblings = [
        r
        for r in index.functions(target.path)
        if r.is_definition
        and r.name != target.name
        and r.name not in config.skip_callers
        and make_unit_id(r.path, r.name) not in shown
    ]
    siblings.sort(key=lambda r: (abs(r.start - target.start), r.start))
    return [
        PackEntry(
            SliceKind.SIBLINGS,
            make_unit_id(r.path, r.name),
            _signature(index, r),
        )
        for r in siblings[: config.max_siblings]
    ]


def build_context_pack(
    index: ProjectIndex,
    unit_id: str,
    config: PackConfig | None = None,
    *,
    contracts: Contracts | None = None,
) -> ContextPack:
    """Build the pack for `unit_id` (``path::symbol``, a path of the index's tree).

    `ProjectIndexError` if the unit's file or function is not in the tree.
    `contracts` maps a callee's unit id to its accepted contracts (see
    `store_contracts`); without it callees show signatures and docs only.
    """
    config = config or PackConfig()
    path, symbol = _normalise(unit_id_path(unit_id)), unit_id_symbol(unit_id)
    target = index.definition(path, symbol)
    if target is None:
        raise ProjectIndexError(f"no definition of {symbol!r} in {path!r}")

    kinds = set(config.slices)
    if SliceKind.TARGET in kinds and SliceKind.SIGNATURE in kinds:
        raise ProjectIndexError("a pack has the target or its signature, not both")

    ranked: list[PackEntry] = []
    for kind in SliceKind:
        if kind not in kinds:
            continue
        if kind in (SliceKind.TARGET, SliceKind.SIGNATURE):
            ranked += _target_entries(index, unit_id, target, kind)
        elif kind is SliceKind.TYPES:
            ranked += _type_entries(
                index, target, config, SliceKind.SIGNATURE not in kinds
            )
        elif kind is SliceKind.CALLEES:
            ranked += _callee_entries(index, target, config, contracts)
        elif kind is SliceKind.SIBLINGS:
            ranked += _sibling_entries(index, target, config, {e.label for e in ranked})
        else:
            ranked += _call_entries(index, target, config, kind)

    return _select(unit_id, ranked, config.max_tokens)


def _select(unit_id: str, ranked: list[PackEntry], max_tokens: int) -> ContextPack:
    """Take entries in rank order while the rendered pack fits `max_tokens`.

    Target entries are taken unconditionally.
    """
    chosen: list[PackEntry] = []
    for entry in ranked:
        if entry.kind in (SliceKind.TARGET, SliceKind.SIGNATURE) or (
            estimate_tokens(ContextPack(unit_id, (*chosen, entry)).text) <= max_tokens
        ):
            chosen.append(entry)
    return ContextPack(unit_id, tuple(chosen), omitted=len(ranked) - len(chosen))

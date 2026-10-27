"""Equivalence harness (RFC-0006 S1) and the equivalent-mutant filter (#391).

``check_equivalence`` asks whether two versions of one C function agree on every
input up to a bound. ``render_equivalence_harness`` puts the reference (every
function it defines renamed with a ``__ref`` suffix) and the candidate in one
translation unit; ``main`` draws nondet inputs once, gives each version its own
copy of every buffer, calls both, and asserts the *relation* over the return
value and every buffer (a ``const`` one too: a cast can still write through it).

The relation is bitwise equality. Floats compare as "both NaN, or bit-identical",
so NaN payloads do not matter but -0.0 and +0.0 differ.

Vocabulary: ESBMC emits no proof object. ``EquivalenceVerified`` means *no
divergence found up to bound k and length bound ``max_len``*, never "equivalent
for all inputs". Every verdict carries both bounds. An inconclusive run is
``EquivalenceUnknown`` and a unit the harness cannot model faithfully is
``EquivalenceRefused`` with a reason; neither is ever read as VERIFIED.

``filter_equivalent`` uses the check to drop mutants equal to the original. A
mutant that diverges only *beyond* the bound looks equivalent too and is skipped
with the rest, which shrinks the denominator of a kill-rate and inflates it. That
bias is inherent to a bounded check; it is why the skipped count is always
reported alongside the filter's result.

Scope of the shared translation unit (v1, fail closed): sources that define a
file-scope object, a file-scope aggregate, ``main``, or function-local ``static``
state are refused, as are non-arithmetic parameters and mismatched signatures.
Conditional directives and ``#pragma`` are refused. State hidden behind an
``#include``, and calls to functions defined in neither source (each draws an
independent nondet), are not visible to this text-level analysis.
"""

from __future__ import annotations

import re
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from functools import partial
from pathlib import Path
from typing import Protocol

from forseti.esbmc import (
    Counterexample,
    Error,
    EsbmcResult,
    RunMeta,
    Verified,
    Violated,
    mask_comments,
    rename_all_declarations_and_definitions,
    verify,
)
from forseti.esbmc import Unknown as EsbmcUnknown
from forseti.esbmc.preprocessor import mask_string_literals
from forseti.orchestrator import VerifyPort
from forseti.precond import DEFAULT_MAX_LEN
from forseti.precond.run import escalating_port
from forseti.properties import (
    BufferParam,
    HarnessError,
    LengthBounds,
    ScalarParam,
    SemanticSpec,
    UnitSignature,
    extract_signature,
    plan_length_bounds,
    renderability_reason,
)

DEFAULT_TIMEOUT_S = 30.0
REFERENCE_SUFFIX = "__ref"
_PRIVATE = "__forseti_"
_LABEL = "forseti:equivalence"
_MAX_LITERAL_LEN = 64
_QUALIFIERS = frozenset({"const", "volatile"})
_INT_WORD_RE = re.compile(
    r"unsigned|signed|int|short|long|char|_Bool|bool|s?size_t|ptrdiff_t"
    r"|u?int(?:_least|_fast)?\d+_t|u?intptr_t|u?intmax_t"
)
_FLOAT_WORD_RE = re.compile(r"float|double")
_DIRECTIVE_RE = re.compile(r"^[ \t]*#(?:[^\n]|(?<=\\)\n)*", re.MULTILINE)
_CONDITIONAL_RE = re.compile(
    r"^[ \t]*#[ \t]*(?:if|ifdef|ifndef|elif|else|endif|pragma)\b", re.MULTILINE
)
_INCLUDES = ("stdint.h", "stddef.h", "limits.h", "stdlib.h", "stdbool.h")


class EquivalenceRefusal(HarnessError):
    """The pair cannot be modelled faithfully by the equivalence harness."""


class EquivalenceVerdict(Enum):
    VERIFIED = "verified"
    VIOLATED = "violated"
    UNKNOWN = "unknown"
    REFUSED = "refused"


class UnknownReason(Enum):
    TIMEOUT = "timeout"
    MEMOUT = "memout"
    UNCLASSIFIED = "unclassified"
    UNDER_UNWOUND = "under_unwound"
    FAULT = "fault"
    ERROR = "error"


@dataclass(frozen=True)
class WitnessValue:
    """One observed value: ESBMC's printed text and, when shown, its bit string."""

    value: str
    bits: str | None = None


@dataclass(frozen=True)
class Witness:
    """A behaviour-diff witness: one input on which the versions disagree.

    `inputs`, `reference_output` and `candidate_output` list ``(label, value)``
    in declaration order. A label is a parameter name, ``name[i]`` for a buffer
    element, or ``return``. An entry is omitted when the counterexample did not
    bind it. `diverging` names the output whose assertion failed.
    """

    diverging: str
    inputs: tuple[tuple[str, WitnessValue], ...]
    reference_output: tuple[tuple[str, WitnessValue], ...]
    candidate_output: tuple[tuple[str, WitnessValue], ...]


@dataclass(frozen=True)
class EquivalenceVerified:
    """No divergence found up to `k` and `length_bounds` (not for all inputs)."""

    k: int
    length_bounds: LengthBounds
    meta: RunMeta

    @property
    def verdict(self) -> EquivalenceVerdict:
        return EquivalenceVerdict.VERIFIED


@dataclass(frozen=True)
class EquivalenceViolated:
    """The versions disagree on `witness`."""

    k: int
    length_bounds: LengthBounds
    witness: Witness
    raw_counterexample: str = field(repr=False)
    meta: RunMeta = field(repr=False)

    @property
    def verdict(self) -> EquivalenceVerdict:
        return EquivalenceVerdict.VIOLATED


@dataclass(frozen=True)
class EquivalenceUnknown:
    """Inconclusive at `k`: raise the bound or report it; never read as VERIFIED."""

    k: int
    length_bounds: LengthBounds
    reason: UnknownReason
    detail: str
    meta: RunMeta

    @property
    def verdict(self) -> EquivalenceVerdict:
        return EquivalenceVerdict.UNKNOWN


@dataclass(frozen=True)
class EquivalenceRefused:
    """The harness declined to model this pair; `reason` says why."""

    reason: str

    @property
    def verdict(self) -> EquivalenceVerdict:
        return EquivalenceVerdict.REFUSED


EquivalenceResult = (
    EquivalenceVerified | EquivalenceViolated | EquivalenceUnknown | EquivalenceRefused
)


@dataclass(frozen=True)
class _Output:
    label: str
    ref_var: str
    cand_var: str


@dataclass(frozen=True)
class EquivalenceHarness:
    """A rendered harness, the length bounds it applied, and its witness slots."""

    source_text: str
    length_bounds: LengthBounds
    input_vars: tuple[tuple[str, str], ...]
    outputs: tuple[_Output, ...]


def render_equivalence_harness(
    *,
    reference_source: str,
    candidate_source: str,
    symbol: str,
    max_len: int = DEFAULT_MAX_LEN,
) -> EquivalenceHarness:
    """Return one C translation unit comparing `symbol` across two sources.

    Pure string synthesis. Raises `EquivalenceRefusal` (a `HarnessError`) with a
    reason for anything the harness cannot model: see the module docstring.
    Each buffer length is capped at `max_len`; buffers and their fills are
    unrolled, so only loops inside the units need ``--unwind``.
    """
    if max_len < 0:
        raise EquivalenceRefusal(f"max_len must be >= 0, got {max_len}")
    signature, ref_text = _prepare(reference_source, candidate_source, symbol)
    scalars = [p for p in signature.params if isinstance(p, ScalarParam)]
    buffers = [p for p in signature.params if isinstance(p, BufferParam)]
    bounds = plan_length_bounds(signature, SemanticSpec("1"), max_len)
    returns_void = signature.return_ctype.strip() == "void"
    ret_ctype = _unqual(signature.return_ctype)

    body: list[str] = []
    inputs: list[tuple[str, str]] = []
    outputs: list[_Output] = []
    asserts: list[str] = []
    nondet: list[str] = []
    float_kinds: set[str] = set()

    def need_nondet(ctype: str) -> None:
        if ctype not in nondet:
            nondet.append(ctype)

    def eq(ctype: str, a: str, b: str) -> str:
        kind = _arith_kind(ctype)
        if kind in ("float", "double"):
            float_kinds.add(kind)
            return f"{_PRIVATE}same_{kind}({a}, {b})"
        return f"({a} == {b})"

    for p in scalars:
        ctype = _unqual(p.ctype)
        need_nondet(ctype)
        body.append(f"    {ctype} {p.name} = {_nondet(ctype)}();")
        inputs.append((p.name, p.name))
    for name, bound in bounds:
        body.append(f"    __ESBMC_assume(({name} >= 0 && {name} <= {bound}));")

    ref_args: dict[str, str] = {p.name: p.name for p in scalars}
    cand_args: dict[str, str] = dict(ref_args)
    for b in buffers:
        elem = _unqual(b.elem_ctype)
        need_nondet(elem)
        ref_name = f"{_PRIVATE}ref_{b.name}"
        if b.out and b.length.strip() == "1":
            seed = f"{_PRIVATE}in_{b.name}"
            body += [
                f"    {elem} {seed} = {_nondet(elem)}();",
                f"    {elem} {b.name} = {seed};",
                f"    {elem} {ref_name} = {seed};",
            ]
            inputs.append((b.name, seed))
            ref_args[b.name] = f"&{ref_name}"
            cand_args[b.name] = f"&{b.name}"
            ro, co = f"{_PRIVATE}out_ref_{b.name}", f"{_PRIVATE}out_{b.name}"
            outputs.append(_Output(b.name, ro, co))
            asserts += [
                f"    {elem} {ro} = {ref_name};",
                f"    {elem} {co} = {b.name};",
                f'    __ESBMC_assert({eq(elem, ro, co)}, "{_LABEL}:{b.name}");',
            ]
            continue
        count = int(b.length) if b.length.strip().isdecimal() else max_len
        body += [
            f"    {elem} *{b.name} = malloc(({b.length}) * sizeof({elem}));",
            f"    {elem} *{ref_name} = malloc(({b.length}) * sizeof({elem}));",
            f"    __ESBMC_assume({b.name} != NULL);",
            f"    __ESBMC_assume({ref_name} != NULL);",
        ]
        for j in range(count):
            seed = f"{_PRIVATE}in_{b.name}_{j}"
            body += [
                f"    if (({b.length}) > {j}) {{",
                f"        {elem} {seed} = {_nondet(elem)}();",
                f"        {b.name}[{j}] = {seed};",
                f"        {ref_name}[{j}] = {seed};",
                "    }",
            ]
            inputs.append((f"{b.name}[{j}]", seed))
            label = f"{b.name}[{j}]"
            ro = f"{_PRIVATE}out_ref_{b.name}_{j}"
            co = f"{_PRIVATE}out_{b.name}_{j}"
            outputs.append(_Output(label, ro, co))
            asserts += [
                f"    if (({b.length}) > {j}) {{",
                f"        {elem} {ro} = {ref_name}[{j}];",
                f"        {elem} {co} = {b.name}[{j}];",
                f'        __ESBMC_assert({eq(elem, ro, co)}, "{_LABEL}:{label}");',
                "    }",
            ]
        ref_args[b.name] = ref_name
        cand_args[b.name] = b.name

    order = [p.name for p in signature.params]
    ref_call = f"{symbol}{REFERENCE_SUFFIX}({', '.join(ref_args[n] for n in order)})"
    cand_call = f"{symbol}({', '.join(cand_args[n] for n in order)})"
    if returns_void:
        body += [f"    {ref_call};", f"    {cand_call};"]
    else:
        rr, rc = f"{_PRIVATE}ret_ref", f"{_PRIVATE}ret"
        body += [
            f"    {ret_ctype} {rr} = {ref_call};",
            f"    {ret_ctype} {rc} = {cand_call};",
            f'    __ESBMC_assert({eq(ret_ctype, rr, rc)}, "{_LABEL}:return");',
        ]
        outputs.insert(0, _Output("return", rr, rc))
    body += asserts
    for b in buffers:
        if not (b.out and b.length.strip() == "1"):
            body += [f"    free({b.name});", f"    free({_PRIVATE}ref_{b.name});"]

    lines = [f"#include <{inc}>" for inc in _INCLUDES]
    lines += ["", ref_text.strip("\n"), "", candidate_source.strip("\n"), ""]
    for kind, bits in (("float", "uint32_t"), ("double", "uint64_t")):
        if kind in float_kinds:
            lines += [
                f"static int {_PRIVATE}same_{kind}({kind} p, {kind} q) {{",
                f"    union {{ {kind} v; {bits} u; }} {_PRIVATE}x, {_PRIVATE}y;",
                f"    {_PRIVATE}x.v = p;",
                f"    {_PRIVATE}y.v = q;",
                f"    return (p != p && q != q) || {_PRIVATE}x.u == {_PRIVATE}y.u;",
                "}",
                "",
            ]
    lines += [f"{c} {_nondet(c)}(void);" for c in nondet]
    lines += ["", "int main(void) {", *body, "    return 0;", "}"]
    return EquivalenceHarness(
        source_text="\n".join(lines) + "\n",
        length_bounds=bounds,
        input_vars=tuple(inputs),
        outputs=tuple(outputs),
    )


def check_equivalence(
    *,
    reference: str,
    candidate: str,
    symbol: str,
    k: int,
    max_len: int = DEFAULT_MAX_LEN,
    timeout_s: float | None = DEFAULT_TIMEOUT_S,
    extra_flags: Sequence[str] = (),
    esbmc_bin: str = "esbmc",
    work_dir: Path | None = None,
    verify_port: VerifyPort | None = None,
) -> EquivalenceResult:
    """Check ``reference::symbol`` against ``candidate::symbol`` up to bound `k`.

    Unwinding assertions are ON (an under-unwound loop is UNKNOWN, never a
    vacuous VERIFIED). A safety violation in the reference is a refusal (the
    pair's shape is not modelled faithfully); one elsewhere is UNKNOWN. `verify_port`
    replaces the real ESBMC port, as in ``core.check.check_source``. The harness
    file goes under `work_dir`, or a temporary directory removed afterwards.
    """
    try:
        harness = render_equivalence_harness(
            reference_source=reference,
            candidate_source=candidate,
            symbol=symbol,
            max_len=max_len,
        )
    except EquivalenceRefusal as exc:
        return EquivalenceRefused(str(exc))
    except HarnessError as exc:
        return EquivalenceRefused(f"signature not modelled: {exc}")
    port = verify_port or escalating_port(
        partial(
            verify,
            timeout_s=timeout_s,
            esbmc_bin=esbmc_bin,
            extra_flags=(*extra_flags, "--no-slice"),
            no_unwinding_assertions=False,
        )
    )

    def run(directory: Path) -> EsbmcResult:
        path = directory / f"equivalence_{symbol}.c"
        path.write_text(harness.source_text)
        return port(path, unwind=k)

    if work_dir is not None:
        work_dir.mkdir(parents=True, exist_ok=True)
        result = run(work_dir)
    else:
        with tempfile.TemporaryDirectory(prefix="forseti-equivalence-") as tmp:
            result = run(Path(tmp))
    return _classify(result, harness, k)


def _classify(
    result: EsbmcResult, harness: EquivalenceHarness, k: int
) -> EquivalenceResult:
    bounds = harness.length_bounds
    if isinstance(result, Verified):
        return EquivalenceVerified(k, bounds, result.meta)
    if isinstance(result, EsbmcUnknown):
        reason = UnknownReason.__members__.get(
            result.reason.name, UnknownReason.UNCLASSIFIED
        )
        return EquivalenceUnknown(k, bounds, reason, reason.value, result.meta)
    if isinstance(result, Error):
        return EquivalenceUnknown(
            k, bounds, UnknownReason.ERROR, result.message, result.meta
        )
    assert isinstance(result, Violated)
    cex = result.counterexample
    description = cex.violated_property.description if cex else ""
    if _LABEL in description or (cex is None and _LABEL in result.raw_counterexample):
        return EquivalenceViolated(
            k,
            bounds,
            _witness(cex, harness, description),
            result.raw_counterexample,
            result.meta,
        )
    function = cex.violated_property.loc.function if cex else None
    where = function or "unknown location"
    detail = f"{description or 'safety property violated'} in {where}"
    if function is not None and function.endswith(REFERENCE_SUFFIX):
        return EquivalenceRefused(
            "the reference itself violates a property under the harness's assumed "
            f"input shape ({detail}); equivalence is not decided"
        )
    return EquivalenceUnknown(k, bounds, UnknownReason.FAULT, detail, result.meta)


def _witness(
    cex: Counterexample | None, harness: EquivalenceHarness, description: str
) -> Witness:
    values: dict[str, WitnessValue] = {}
    if cex is not None:
        for step in cex.steps:
            for a in step.assignments:
                values[a.lhs] = WitnessValue(a.value, a.binary)

    def pick(pairs: Sequence[tuple[str, str]]) -> tuple[tuple[str, WitnessValue], ...]:
        return tuple((label, values[v]) for label, v in pairs if v in values)

    _, _, diverging = description.partition(f"{_LABEL}:")
    return Witness(
        diverging=diverging.strip(),
        inputs=pick(harness.input_vars),
        reference_output=pick([(o.label, o.ref_var) for o in harness.outputs]),
        candidate_output=pick([(o.label, o.cand_var) for o in harness.outputs]),
    )


class MutantLike(Protocol):
    """What the filter needs of a mutant: an id and the full mutated source."""

    @property
    def id(self) -> str: ...

    @property
    def source(self) -> str: ...


EquivalenceChecker = Callable[..., EquivalenceResult]


@dataclass(frozen=True)
class EquivalenceFilter[M: MutantLike]:
    """The outcome of `filter_equivalent`.

    `skipped_equivalent` holds only mutants found equivalent *up to `k` and
    `max_len`*; a mutant diverging only beyond those bounds is in it too, which
    inflates a kill-rate computed over `kept`. `skipped` is always reported.
    Every other mutant, including UNKNOWN and refused ones, is in `kept`.
    `results` pairs each mutant id with its result, in input order.
    `original_refusal` is set when the original itself could not be modelled; then
    nothing is skipped.
    """

    kept: tuple[M, ...]
    skipped_equivalent: tuple[M, ...]
    results: tuple[tuple[str, EquivalenceResult], ...]
    k: int
    max_len: int
    original_refusal: str | None = None

    @property
    def skipped(self) -> int:
        return len(self.skipped_equivalent)

    @property
    def undecided(self) -> tuple[str, ...]:
        """Ids kept because the check was UNKNOWN or refused."""
        return tuple(
            mutant_id
            for mutant_id, result in self.results
            if isinstance(result, EquivalenceUnknown | EquivalenceRefused)
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "k": self.k,
            "max_len": self.max_len,
            "skipped": self.skipped,
            "skipped_equivalent": [m.id for m in self.skipped_equivalent],
            "kept": [m.id for m in self.kept],
            "undecided": list(self.undecided),
            "original_refusal": self.original_refusal,
            "verdicts": {i: r.verdict.value for i, r in self.results},
        }


def filter_equivalent[M: MutantLike](
    original: str,
    mutants: Sequence[M],
    k: int,
    *,
    symbol: str,
    max_len: int = DEFAULT_MAX_LEN,
    checker: EquivalenceChecker = check_equivalence,
    **check_kwargs: object,
) -> EquivalenceFilter[M]:
    """Split `mutants` into kept and skipped-as-equivalent against `original`.

    Only a VERIFIED check skips a mutant (see `EquivalenceFilter` for the bias that
    implies). VIOLATED, UNKNOWN and refused mutants are kept. `check_kwargs` are
    forwarded to `checker` (timeout, extra flags, ESBMC binary, ...). The original
    is first checked against itself once; if that is refused (e.g. the original
    itself faults under the harness) nothing is skipped and no mutant is run.
    Raises `ValueError` on duplicate mutant ids.
    """
    ids = [m.id for m in mutants]
    if len(set(ids)) != len(ids):
        raise ValueError("mutant ids must be unique")
    original_refusal: str | None = None
    try:
        _prepare(original, original, symbol)
    except HarnessError as exc:
        original_refusal = str(exc)
    if original_refusal is None and mutants:
        baseline = checker(
            reference=original,
            candidate=original,
            symbol=symbol,
            k=k,
            max_len=max_len,
            **check_kwargs,
        )
        if isinstance(baseline, EquivalenceRefused):
            original_refusal = baseline.reason
    kept: list[M] = []
    skipped: list[M] = []
    results: list[tuple[str, EquivalenceResult]] = []
    for mutant in mutants:
        result: EquivalenceResult
        if original_refusal is not None:
            result = EquivalenceRefused(f"original not modelled: {original_refusal}")
        else:
            result = checker(
                reference=original,
                candidate=mutant.source,
                symbol=symbol,
                k=k,
                max_len=max_len,
                **check_kwargs,
            )
        results.append((mutant.id, result))
        (skipped if isinstance(result, EquivalenceVerified) else kept).append(mutant)
    return EquivalenceFilter(
        tuple(kept), tuple(skipped), tuple(results), k, max_len, original_refusal
    )


@dataclass(frozen=True)
class _Scan:
    functions: dict[str, str]
    state: str | None


def _scan_view(source: str) -> str:
    view = mask_string_literals(mask_comments(source))
    return _DIRECTIVE_RE.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), view)


def _function_name(head: str) -> str | None:
    depth = 0
    for i in range(len(head) - 1, -1, -1):
        if head[i] == ")":
            depth += 1
        elif head[i] == "(":
            depth -= 1
            if depth == 0:
                match = re.search(r"([A-Za-z_]\w*)\s*$", head[:i])
                if match is None or match.group(1) == "__attribute__":
                    return None
                return match.group(1)
    return None


def _statement_state(statement: str) -> str | None:
    text = " ".join(statement.split())
    if not text or re.match(r"typedef\b", text):
        return None
    is_prototype = (
        text.endswith(")")
        and "=" not in text
        and "__attribute__" not in text
        and not re.search(r"\(\s*\*", text)
    )
    if is_prototype:
        return None
    return f"declares file-scope object `{text[:60]}`"


def _scan(view: str) -> _Scan:
    functions: dict[str, str] = {}
    state: str | None = None
    depth = 0
    head: list[str] = []
    pending: str | None = None
    body_start = 0
    for i, ch in enumerate(view):
        if depth == 0:
            if ch == "{":
                text = "".join(head).rstrip()
                pending = _function_name(text) if text.endswith(")") else None
                if pending is None and state is None:
                    state = "defines a file-scope type or initialised object"
                body_start = i + 1
                depth = 1
            elif ch == ";":
                state = state or _statement_state("".join(head))
                head = []
            else:
                head.append(ch)
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and pending is not None:
                functions[pending] = view[body_start:i]
                if state is None and re.search(
                    r"\b(?:static|extern|_Thread_local|__thread)\b", functions[pending]
                ):
                    state = f"function `{pending}` declares static or extern state"
                head = []
                pending = None
    return _Scan(functions, state)


def _unqual(ctype: str) -> str:
    return " ".join(w for w in ctype.split() if w not in _QUALIFIERS)


def _arith_kind(ctype: str) -> str | None:
    words = _unqual(ctype).split()
    if not words:
        return None
    if len(words) == 1 and _FLOAT_WORD_RE.fullmatch(words[0]):
        return words[0]
    if all(_INT_WORD_RE.fullmatch(w) for w in words):
        return "int"
    return None


def _nondet(ctype: str) -> str:
    return "nondet_" + re.sub(r"[^A-Za-z0-9_]", "_", ctype.strip())


def _shape(signature: UnitSignature) -> tuple[object, ...]:
    index = {p.name: i for i, p in enumerate(signature.params)}
    parts: list[object] = [" ".join(signature.return_ctype.split())]
    for p in signature.params:
        if isinstance(p, ScalarParam):
            parts.append(("scalar", " ".join(p.ctype.split())))
        else:
            length = p.length.strip()
            parts.append(
                (
                    "buffer",
                    " ".join(p.elem_ctype.split()),
                    p.const,
                    p.out,
                    f"#{index[length]}" if length in index else length,
                )
            )
    return tuple(parts)


def _signature_refusal(signature: UnitSignature) -> str | None:
    ret = signature.return_ctype.strip()
    if ret != "void" and _arith_kind(ret) is None:
        return f"return type `{ret}` is not void or a plain arithmetic type"
    by_name = {p.name: p for p in signature.params}
    for p in signature.params:
        if p.name.startswith(_PRIVATE):
            return f"parameter `{p.name}` uses the reserved prefix `{_PRIVATE}`"
        if isinstance(p, ScalarParam):
            if _arith_kind(p.ctype) is None:
                return f"parameter `{p.name}` has non-arithmetic type `{p.ctype}`"
            continue
        if _arith_kind(p.elem_ctype) is None:
            return (
                f"parameter `{p.name}` points to non-arithmetic type "
                f"`{p.elem_ctype}` (struct and typedef pointers are not modelled)"
            )
        length = p.length.strip()
        if p.out and length == "1":
            continue
        if length.isdecimal():
            if int(length) > _MAX_LITERAL_LEN:
                return (
                    f"buffer `{p.name}` has literal length {length} "
                    f"> {_MAX_LITERAL_LEN}"
                )
            continue
        owner = by_name.get(length)
        if not isinstance(owner, ScalarParam) or _arith_kind(owner.ctype) != "int":
            return (
                f"buffer `{p.name}` length `{length}` is not an integer "
                "parameter or decimal literal"
            )
    return None


def _fragment_refusal(view: str, symbol: str) -> str | None:
    match = re.search(rf"(?<![\w.]){re.escape(symbol)}\s*\(([^)]*)\)\s*\{{", view)
    if match is None:
        return f"could not locate the parameter list of `{symbol}`"
    for frag in match.group(1).split(","):
        if frag.count("*") > 1 or frag.count("[") > 1:
            return f"parameter `{frag.strip()}` is multi-level or multi-dimensional"
        if re.search(r"\b(?:struct|union|enum)\b|\.\.\.", frag):
            return f"parameter `{frag.strip()}` is not a plain arithmetic type"
    return None


def _out_param_refusal(signature: UnitSignature, body: str, label: str) -> str | None:
    """A single-element output must only be dereferenced.

    `extract_signature` types a trailing non-const pointer as a one-element output;
    a pointer forwarded, indexed or advanced is really a buffer of unknown extent
    (e.g. ``msort``'s ``tmp``), so the harness would model it wrongly.
    """
    for p in signature.params:
        if not (isinstance(p, BufferParam) and p.out and p.length.strip() == "1"):
            continue
        n = re.escape(p.name)
        for use in re.finditer(rf"(?<![\w.]){n}\b", body):
            before = body[: use.start()].rstrip()
            after = body[use.end() :]
            allowed = (
                before.endswith("*")
                or before.endswith("!")
                or re.match(r"\s*\[\s*0\s*\]", after)
                or re.match(r"\s*[!=]=\s*(?:NULL|0)\b", after)
                or re.search(r"\b(?:NULL|0)\s*[!=]=$", before)
            )
            if not allowed:
                return (
                    f"{label}: output parameter `{p.name}` is used other than by "
                    "dereference (forwarded, indexed or advanced); its extent cannot "
                    "be inferred from the signature"
                )
    return None


def _prepare(reference: str, candidate: str, symbol: str) -> tuple[UnitSignature, str]:
    """Validate the pair and return the shared signature and the renamed reference."""
    views: dict[str, str] = {}
    scans: dict[str, _Scan] = {}
    sigs: dict[str, UnitSignature] = {}
    for label, source in (("reference", reference), ("candidate", candidate)):
        view = _scan_view(source)
        views[label] = view
        if _CONDITIONAL_RE.search(mask_comments(source)):
            raise EquivalenceRefusal(
                f"{label} source uses a conditional directive or #pragma, which "
                "would change meaning inside the shared translation unit"
            )
        if re.search(r"(?<![\w.])main\s*\(", view):
            raise EquivalenceRefusal(f"{label} source mentions main()")
        scan = _scan(view)
        if scan.state is not None:
            raise EquivalenceRefusal(f"{label} source {scan.state} (v1 refuses state)")
        if symbol not in scan.functions:
            raise EquivalenceRefusal(f"{label} source does not define `{symbol}`")
        scans[label] = scan
        try:
            sigs[label] = extract_signature(mask_comments(source), symbol)
        except HarnessError as exc:
            raise EquivalenceRefusal(f"{label} signature not modelled: {exc}") from exc
        reason = _fragment_refusal(view, symbol) or _signature_refusal(sigs[label])
        if reason is not None:
            raise EquivalenceRefusal(f"{label}: {reason}")
        reason = _out_param_refusal(sigs[label], scan.functions[symbol], label)
        if reason is not None:
            raise EquivalenceRefusal(reason)
    if _shape(sigs["reference"]) != _shape(sigs["candidate"]):
        raise EquivalenceRefusal(
            "reference and candidate signatures differ (return type, parameter "
            "types, buffer roles or lengths)"
        )
    signature = sigs["candidate"]
    reason = renderability_reason(
        signature, SemanticSpec("1", result_var=f"{_PRIVATE}result")
    )
    if reason is not None:
        raise EquivalenceRefusal(reason)
    for label in ("reference", "candidate"):
        if re.search(rf"\w{re.escape(REFERENCE_SUFFIX)}\b", views[label]):
            raise EquivalenceRefusal(
                f"{label} source already uses a `{REFERENCE_SUFFIX}` name"
            )
    renamed = reference
    names = sorted(scans["reference"].functions)
    for name in names:
        renamed = rename_all_declarations_and_definitions(
            renamed, name, name + REFERENCE_SUFFIX
        )
    leftover = _scan_view(renamed)
    for name in names:
        if re.search(rf"(?<!\.)(?<!->)\b{re.escape(name)}\b", leftover):
            raise EquivalenceRefusal(
                f"reference refers to `{name}` other than by a direct call "
                "(function pointer or shadowing name); it cannot be renamed safely"
            )
    return signature, renamed

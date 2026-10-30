"""Render a semantic Property into a compilable ESBMC harness (C text).

Given a verification unit (its C kernel *slice* + the target symbol's signature)
and a semantic Property (domain preconditions + a postcondition over the
parameters and the call's result), synthesize one self-contained C translation
unit ESBMC can check: the inlined unit, a nondet generator per parameter type,
``__ESBMC_assume(...)`` preconditions that constrain the domain (so a pass is
non-vacuous), the call, and the postcondition as ``__ESBMC_assert(...)``.

Pure: returns source *text* only -- no disk, no esbmc -- mirroring
``orchestrator.fix``'s return-text seam so the loop and tests stay effect-free.
The runner compiles a *single* ``source: Path`` (`esbmc/runner.py`), so the unit
is **inlined** into the harness rather than referenced; `unit_source` must be a
main-free kernel slice (the ``examples/*.c`` files ship their own ``main`` -- that
is the example harness, not the unit). C only for W2 (ADR-0003); reachability
emission is deferred (ADR-0009 D2).
"""

from __future__ import annotations

import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass

from forseti.esbmc import find_definition_brace

from ._harness_parameters import prepare_parameters
from .cexpr import (
    conjuncts,
    derefs_or_subscripts,
    identifiers,
    references,
    unwrap_parens,
)
from .model import Property, PropertyKind
from .signature import (
    BufferParam,
    HarnessError,
    ScalarParam,
    StructParam,
    UnitSignature,
    extract_signature,
)
from .signature import (
    Param as Param,
)

# Standard headers the semantic harness emits by default. Every macro a pure
# property may name without a declaration is guaranteed by one of these, so the
# allowlist below stays in lockstep with what the emitted C actually declares.
DEFAULT_INCLUDES: tuple[str, ...] = ("stdint.h", "stddef.h", "limits.h")

# Standard limit/pointer macros grouped by the header that defines them. This is
# the single source of truth: `HARNESS_MACROS` is derived by unioning the groups
# whose header appears in `DEFAULT_INCLUDES`, so dropping a header from the
# emitted includes automatically drops its macros from the allowlist -- the
# lockstep is a property of this shape, not a hand-maintained comment (#81).
# POSIX-only names (e.g. SSIZE_MAX) are deliberately absent: no standard header
# here guarantees them, so an accepted candidate that used one would not compile.
_MACROS_BY_HEADER: dict[str, frozenset[str]] = {
    "limits.h": frozenset(
        {
            "CHAR_MIN",
            "CHAR_MAX",
            "SCHAR_MIN",
            "SCHAR_MAX",
            "UCHAR_MAX",
            "SHRT_MIN",
            "SHRT_MAX",
            "USHRT_MAX",
            "INT_MIN",
            "INT_MAX",
            "UINT_MAX",
            "LONG_MIN",
            "LONG_MAX",
            "ULONG_MAX",
            "LLONG_MIN",
            "LLONG_MAX",
            "ULLONG_MAX",
        }
    ),
    "stdint.h": frozenset(
        {
            "INT8_MIN",
            "INT8_MAX",
            "UINT8_MAX",
            "INT16_MIN",
            "INT16_MAX",
            "UINT16_MAX",
            "INT32_MIN",
            "INT32_MAX",
            "UINT32_MAX",
            "INT64_MIN",
            "INT64_MAX",
            "UINT64_MAX",
            "INTMAX_MIN",
            "INTMAX_MAX",
            "UINTMAX_MAX",
            "INTPTR_MIN",
            "INTPTR_MAX",
            "UINTPTR_MAX",
            "SIZE_MAX",
            "PTRDIFF_MIN",
            "PTRDIFF_MAX",
        }
    ),
    "stddef.h": frozenset({"NULL"}),
}

# Macros a pure property may reference without declaring them. Derived from the
# headers actually emitted (`DEFAULT_INCLUDES`) so the allowlist cannot drift out
# of lockstep with the harness's includes. The proposer's static gate imports
# this rather than restating the set.
HARNESS_MACROS: frozenset[str] = frozenset(
    macro
    for header, macros in _MACROS_BY_HEADER.items()
    if header in DEFAULT_INCLUDES
    for macro in macros
)


LengthBounds = tuple[tuple[str, int], ...]

# The label the post-call reachability probe (``__ESBMC_assert(0, ...)``) carries,
# so a reachable site is a recognisable FAILED rather than an anonymous one. The
# precond stack's non-vacuity probe uses the same label and the same reading
# (`precond.reachability.classify_site_probe`); `precond.synth` imports it from here.
NON_VACUITY_LABEL = "forseti:non-vacuity"


def non_vacuity_probe(label: str = NON_VACUITY_LABEL) -> str:
    """The C statement that marks a point reachable: a labelled ``assert(0)``."""
    return f'__ESBMC_assert(0, "{label}");'


@dataclass(frozen=True)
class PropertyHarness:
    """A rendered harness plus the default length bounds it applied.

    `length_bounds` is ``((param, max_len), ...)``: every length parameter the
    renderer capped with ``__ESBMC_assume(param <= max_len)`` because the
    property's own domain did not constrain it. Empty when nothing was capped, so
    a caller can always tell whether a verdict is scoped to ``len <= N``.
    """

    source_text: str
    length_bounds: LengthBounds = ()
    probe_source_text: str | None = None
    assumption: str | None = None


@dataclass(frozen=True)
class SemanticSpec:
    """The checkable content of a semantic Property, as harness-ready C exprs.

    `preconditions` are boolean C expressions over the parameter names -> each
    becomes ``__ESBMC_assume((expr));`` before the call. `postcondition` is a
    boolean C expression over the parameter names, output-buffer names, and the
    reserved identifier `result_var` (bound to the call's return value) -> it
    becomes the ``__ESBMC_assert(...)``. `result_var` is unused when the unit
    returns void.
    """

    postcondition: str
    preconditions: tuple[str, ...] = ()
    result_var: str = "result"


def render_semantic_harness(
    *,
    unit_source: str,
    signature: UnitSignature,
    spec: SemanticSpec,
    includes: Sequence[str] = DEFAULT_INCLUDES,
    max_len: int | None = None,
    probe: bool = False,
) -> str:
    """Return a compilable ESBMC harness (C text) for one semantic property.

    Deterministic, pure string synthesis. Emits, in order: the includes, the
    inlined `unit_source`, one nondet prototype per distinct nondet type, then a
    ``main`` that declares the scalar params, assumes the length/scalar
    preconditions, sets up the buffers, assumes any buffer-content preconditions
    (which reference an identifier only in scope after the buffer is declared and
    filled), calls the unit, and asserts the postcondition. Raises
    `HarnessError` on an un-renderable input (empty postcondition, `result_var`
    clashing a param name, a void return referenced by `result_var`, a
    `unit_source` that defines its own ``main``, an unknown Param subtype, or a
    negative `max_len`).

    `max_len` caps every buffer-length parameter the property's domain leaves
    unconstrained (`plan_length_bounds`) with
    ``__ESBMC_assume(len >= 0 && len <= max_len)`` before the allocation (the
    ``>= 0`` is a tautology for an unsigned length; for a signed one it keeps a
    negative length out of the fill loop, which the allocation's overflow guard
    only prunes when ``sizeof(elem) > 1``). ``None`` (the default) leaves such a
    length genuinely unconstrained -- sound, but the nondet-fill loop then
    iterates an unbounded number of times, so no finite unwind settles it and the
    verdict is always UNKNOWN (#299); the check path passes a small bound instead.

    `probe=True` renders the *vacuity probe* twin (W3.4): byte-identical up to and
    including the call, with the real assertion replaced by a labelled
    ``__ESBMC_assert(0, NON_VACUITY_LABEL)``. Verified at the same k, a probe that
    fires (VIOLATED carrying the label) shows some path reaches the post-call
    point; a VERIFIED probe shows the postcondition was never evaluated.

    A `StructParam` is one nondet ``T`` object (``T p__obj = nondet_T();``) with the
    parameter bound to its address (``T *p = &p__obj;``), declared before the
    domain so a clause may read ``p->field``. Nothing constrains it except the
    domain: it can be a state no caller builds (`nondet_struct_note`).
    """
    # The source-level guard needs `unit_source`, so it stays here; every
    # `(signature, spec)` guard -- empty postcondition, `result_var` clash, void
    # return, the two emission rules, and a precondition mixing a buffer with its
    # length -- is delegated to the single static authority so the renderer and the
    # proposer's gate consult one predicate.
    if _defines_main(unit_source):
        raise HarnessError(
            "unit_source defines main; pass the kernel slice, not the example harness"
        )

    reason = renderability_reason(signature, spec)
    if reason is not None:
        raise HarnessError(reason)

    postcondition = spec.postcondition.strip()
    returns_void = signature.return_ctype.strip() == "void"

    prepared = prepare_parameters(signature.params, includes=includes)
    buffers = _plain_buffers(signature.params)

    lines = [*prepared.headers, "", unit_source.strip("\n"), ""]
    lines += prepared.prototypes
    lines += ["", "int main(void) {"]
    lines += prepared.scalar_declarations
    for _, declaration in prepared.struct_declarations:
        lines += declaration
    pre_buffer, post_buffer = _split_assumptions(spec.preconditions, buffers)
    for pre in pre_buffer:
        lines.append(f"    __ESBMC_assume(({pre}));")
    for name, bound in plan_length_bounds(signature, spec, max_len):
        lines.append(f"    __ESBMC_assume(({name} >= 0 && {name} <= {bound}));")
    lines += prepared.buffer_setup
    for pre in post_buffer:
        lines.append(f"    __ESBMC_assume(({pre}));")
    call = f"{signature.symbol}({prepared.arguments})"
    if returns_void:
        lines.append(f"    {call};")
    else:
        lines.append(f"    {signature.return_ctype} {spec.result_var} = {call};")
    if probe:
        lines.append(f"    {non_vacuity_probe()}")
    else:
        lines.append(f'    __ESBMC_assert(({postcondition}), "forseti:semantic");')
    lines += prepared.cleanup
    lines.append("    return 0;")
    lines.append("}")
    return "\n".join(lines) + "\n"


def render_property_harness(
    *, unit_source: str, symbol: str, prop: Property, max_len: int | None = None
) -> str:
    """Render a stored semantic Property into a compilable ESBMC harness (C text).

    The "I only have the source slice + symbol" entry point over the writer:
    parse the unit signature from the slice (`extract_signature`), project the
    Property onto a `SemanticSpec` (`spec_from_property`), and render a
    self-contained harness (`render_semantic_harness`). This is the recipe the
    #66 check phase runs per property; it lives *here*, beside the three
    functions it composes, so a driver need only ask for "the harness for this
    property" rather than carry the knowledge that building one means
    parse -> project -> render (`render_semantic_harness` alone wants a
    pre-parsed `UnitSignature`; this is the variant that starts from source).

    Fail-loud: a `HarnessError` from any leg -- an unparseable signature, a
    non-semantic (reachability) property, or an un-renderable postcondition --
    propagates unchanged, so a bad unit/property is never turned into a silent
    mis-harness. `max_len` is `render_semantic_harness`'s.
    """
    return build_property_harness(
        unit_source=unit_source, symbol=symbol, prop=prop, max_len=max_len
    ).source_text


def build_property_harness(
    *, unit_source: str, symbol: str, prop: Property, max_len: int | None = None
) -> PropertyHarness:
    """`render_property_harness`, plus the length bounds the harness applied.

    The check phase reports those bounds on each verdict so a result scoped to
    ``len <= N`` is never mistaken for one over every length. `probe_source_text`
    is the vacuity-probe twin of `source_text` (`render_semantic_harness`
    ``probe=True``), checked at the same k after a HELD.
    """
    signature = extract_signature(unit_source, symbol, infer_structs=True)
    spec = spec_from_property(prop)
    return PropertyHarness(
        source_text=render_semantic_harness(
            unit_source=unit_source, signature=signature, spec=spec, max_len=max_len
        ),
        length_bounds=plan_length_bounds(signature, spec, max_len),
        probe_source_text=render_semantic_harness(
            unit_source=unit_source,
            signature=signature,
            spec=spec,
            max_len=max_len,
            probe=True,
        ),
        assumption=nondet_struct_note(signature),
    )


def nondet_struct_note(signature: UnitSignature) -> str | None:
    """What a verdict over a nondet struct parameter assumes, or None without one.

    The struct starts as an arbitrary ``T``, including states no caller can build,
    and the harness never discharges that at a caller that does build one, so a
    result is conditional on the domain and is *assumed*, not unconditional.
    """
    structs = signature.struct_params
    if not structs:
        return None
    subjects = ", ".join(f"{p.name} ({p.elem_ctype})" for p in structs)
    return (
        f"{subjects} start as nondet struct values: any field contents (pointer "
        "members included), including states no caller can build; a result holds "
        "only under the domain and is not discharged at the callers that construct "
        "the type"
    )


def spec_from_property(prop: Property) -> SemanticSpec:
    """Project a semantic Property (#62) onto `SemanticSpec`.

    Maps the Property's `expression` -> `postcondition` and `domain` ->
    `preconditions`. Requires ``prop.kind == PropertyKind.SEMANTIC``; raises
    `HarnessError` on a reachability property (deferred, ADR-0009 D2) or a
    missing postcondition. Lives here so #62's Property model stays free of
    harness concerns.
    """
    if prop.kind is not PropertyKind.SEMANTIC:
        raise HarnessError(
            f"cannot render a {prop.kind.value} property; only semantic is "
            "supported (reachability deferred, ADR-0009 D2)"
        )
    if not prop.expression.strip():
        raise HarnessError("semantic property has an empty expression")
    return SemanticSpec(
        postcondition=prop.expression,
        preconditions=tuple(prop.domain),
    )


def renderability_reason(signature: UnitSignature, spec: SemanticSpec) -> str | None:
    """None if `spec` renders to compilable, sound C for `signature`, else why not.

    The static, parser-free, source-free authority on whether a `(signature, spec)`
    pair can become a valid harness -- kept *here*, the module that emits the C, so
    callers (the proposer's static gate, `render_semantic_harness` itself) consult
    one predicate instead of re-deriving harness internals. It covers, in order:

    * an **empty postcondition** -- there is nothing to assert;
    * a **`result_var` that collides with a parameter name** -- the result binding
      would shadow the parameter;
    * a **`result_var` referenced under a void return** -- there is no result to
      bind;
    * a **scalar-backed output** (a trailing single-element ``out`` buffer) that is
      dereferenced or subscripted (``*cp``, ``cp[0]``, ``*(cp + 0)``) -- it is bound
      as a plain scalar passed by address, so the postcondition may only *name* it;
    * a **precondition constraining an output parameter** -- emitted as
      ``__ESBMC_assume(...)`` *before* the call, it would preconstrain a would-be
      result on uninitialized storage, masking a unit that never writes it; domains
      constrain inputs only. The one exception is a bare non-NULL check
      (``tmp != NULL``) on a scratch buffer (`UnitSignature.scratch_param_names`),
      which constrains no contents and holds by construction (#438).
    * a **precondition mixing a buffer and its length** -- a single clause naming
      both a buffer and a buffer-length identifier *outside a subscript index*
      (``n >= 1 && a[0] >= 0``) has no correct emission point: the length bound must
      precede the ``int *a = malloc(n * sizeof(int))`` allocation and the
      buffer-content predicate must follow it, so it is rejected rather than
      mis-ordered. A length named only as an index (``a[n - 1] >= 0``) is a
      buffer-content predicate like any other and is accepted (the renderer's
      `_split_assumptions` reuses this very predicate).

    Best-effort and conservative on the emission rules: it may over-reject an exotic
    postcondition, but a rejected *renderable* property is safe whereas emitting
    un-compilable C is not. It does **not** cover the one structural guard that needs
    the source text -- a `unit_source` that defines its own ``main`` -- which
    `render_semantic_harness` checks directly.
    """
    if not spec.postcondition.strip():
        return "semantic property has an empty postcondition"

    if spec.result_var in signature.param_names:
        return f"result_var {spec.result_var!r} collides with a parameter name"

    if signature.return_ctype.strip() == "void" and references(
        spec.postcondition, spec.result_var
    ):
        return (
            f"postcondition references {spec.result_var!r} but "
            f"{signature.symbol!r} returns void"
        )

    output_params = signature.output_param_names
    scratch = signature.scratch_param_names
    scalar_outputs = {
        p.name
        for p in signature.params
        if isinstance(p, BufferParam) and p.scalar_backed
    }
    for name in scalar_outputs:
        if derefs_or_subscripts(spec.postcondition, name):
            return (
                f"expression dereferences/subscripts scalar-backed output "
                f"{name!r}; the harness binds it as a scalar -- name it directly"
            )
    # Word-boundary detection, not a raw identifier scan: `\bu\b` does not match the
    # `u` in a suffixed literal like `10u`, so an output named `u`/`L`/`UL` cannot be
    # spuriously flagged by an input-only precondition such as `x < 10u`.
    for pre in spec.preconditions:
        # `output_param_names` is an unordered frozenset; sort so that a clause
        # naming several outputs reports the same one every run (reproducible
        # verdicts -- salted hashing would otherwise vary the message).
        for name in sorted(output_params):
            if references(pre, name) and not (
                name in scratch and _is_non_null_check(pre, name)
            ):
                return (
                    f"domain expr {pre!r} constrains output parameter "
                    f"{name!r} (preconditions apply to inputs only)"
                )
    return _mixed_buffer_length_reason(
        spec.preconditions,
        _plain_buffers(signature.params),
    )


def _is_non_null_check(clause: str, name: str) -> bool:
    """Whether `clause` is exactly ``name != NULL`` (or ``!= 0``, either order)."""
    text = unwrap_parens(clause)
    ident, null = re.escape(name), r"(?:NULL|0)"
    return (
        re.fullmatch(rf"{ident}\s*!=\s*{null}|{null}\s*!=\s*{ident}", text) is not None
    )


def plan_length_bounds(
    signature: UnitSignature, spec: SemanticSpec, max_len: int | None
) -> LengthBounds:
    """The ``(param, max_len)`` caps the harness applies to unconstrained lengths.

    A malloc-backed buffer's length is a nondet scalar bounded only by the
    allocation's overflow guard, so the nondet-fill loop iterates a symbolic,
    effectively unbounded number of times and no finite unwind settles it: every
    property over a ``(ptr, len)`` buffer reports UNKNOWN (#299). The cap is what
    ``synth --max-len`` already does for the safety path.

    Only a *scalar parameter* named in a buffer's length expression is capped (a
    macro or literal length is not nondet, and assuming a bound on it could make
    the harness vacuous). The cap applies on top of whatever the domain says about
    that length, an upper bound included (``hi <= 4``, ``hi == lo + 3``): a bound
    above `max_len` is narrowed to it, so the check stays decidable and the
    verdict reads as scoped to ``len <= max_len`` (#438). The one exception is a
    domain whose literal *floor* the cap contradicts (``len >= 16`` with a cap of
    8, `_contradicts_cap`): assuming both is ``false`` and would silently turn the
    check into a vacuous HELD, so that domain stays authoritative and uncapped.
    A floor not readable as a literal (``hi - lo >= 20``) cannot be detected here;
    the vacuity probe (`build_property_harness`) catches a HELD that went vacuous.

    Deduplicated in first-seen order, so two buffers sharing one length emit one
    assume. ``max_len=None`` plans nothing; a negative one is a `HarnessError`
    (``len <= -1`` on an unsigned length assumes ``false``).
    """
    if max_len is None:
        return ()
    if max_len < 0:
        raise HarnessError(f"max_len must be >= 0, got {max_len}")
    scalar_names = {p.name for p in signature.params if isinstance(p, ScalarParam)}
    buffers = _plain_buffers(signature.params)
    buffer_names = {buf.name for buf in buffers}
    constraining = [
        _strip_buffer_subscripts(pre, buffer_names) for pre in spec.preconditions
    ]
    capped: list[str] = []
    for buf in buffers:
        if buf.scalar_backed:
            continue
        for ident in identifiers(buf.length):
            if (
                ident in scalar_names
                and ident not in capped
                and not any(
                    _contradicts_cap(pre, ident, max_len) for pre in constraining
                )
            ):
                capped.append(ident)
    return tuple((ident, max_len) for ident in capped)


def _contradicts_cap(clause: str, ident: str, max_len: int) -> bool:
    """Whether a conjunct of `clause` forces `ident` above `max_len`.

    Reads each top-level ``&&`` conjunct as a literal lower bound
    (`_length_floor`); a clause with a top-level ``||``, or a conjunct that is not
    a plain literal bound, is not read and so never contradicts the cap.
    """
    return any(
        (floor := _length_floor(conjunct, ident)) is not None and floor > max_len
        for conjunct in conjuncts(clause)
    )


def _length_floor(clause: str, ident: str) -> int | None:
    """The smallest length a literal lower-bound `clause` on `ident` allows.

    Recognises only ``ident >= N``, ``ident > N``, ``ident == N``, ``ident != N``
    and their mirrored forms (``N <= ident``), with a decimal, hex or octal literal `N`
    and optional integer suffix; returns None for anything else (a conjunction, an
    upper bound, an expression), which the caller does not read as a floor. The
    fullmatch keeps it conservative: ``(a >= 1) && (b >= 1)`` is a non-match
    rather than being misread as one bound.
    """
    text = unwrap_parens(clause)
    name = re.escape(ident)
    literal = r"(0[xX][0-9a-fA-F]+|0[0-7]*|[1-9]\d*)[uUlL]*"
    if match := re.fullmatch(rf"{name}\s*(>=|>|==|!=)\s*{literal}", text):
        op, digits = match.group(1), match.group(2)
    elif match := re.fullmatch(rf"{literal}\s*(<=|<|==|!=)\s*{name}", text):
        op, digits = match.group(2), match.group(1)
    else:
        return None
    base = 16 if digits[:2] in ("0x", "0X") else 8 if digits[0] == "0" else 10
    value = int(digits, base)
    return {">=": value, ">": value + 1, "<=": value, "<": value + 1, "==": value}.get(
        op, 1 if value == 0 else 0
    )


def _plain_buffers(params: Sequence[Param]) -> list[BufferParam]:
    """The buffer params that are not a single struct instance (`StructParam`)."""
    return [
        p
        for p in params
        if isinstance(p, BufferParam) and not isinstance(p, StructParam)
    ]


def _strip_buffer_subscripts(expr: str, buffer_names: Collection[str]) -> str:
    """`expr` with every ``buf[...]`` subscript reduced to the bare buffer name.

    Isolates the *index* uses of a buffer from the rest of the clause, so a caller
    can ask what a clause says about an identifier **outside** any subscript.
    One pattern per name rather than one alternation, so a name that prefixes
    another (`a` and `ab`) needs no ordering argument. Iterated to a fixpoint so a
    nested index (``a[b[0]]``) collapses inside-out: each substitution strictly
    shortens the text and an unbalanced ``[`` simply stops matching, so the loop
    always terminates -- with no buffers it settles after one no-op pass.
    """
    strippers = [
        re.compile(rf"\b({re.escape(name)})\s*\[[^\[\]]*\]") for name in buffer_names
    ]
    previous = ""
    while previous != expr:
        previous = expr
        for pattern in strippers:
            expr = pattern.sub(r"\1", expr)
    return expr


def _mixed_buffer_length_reason(
    preconditions: Sequence[str], buffers: Sequence[BufferParam]
) -> str | None:
    """Why a precondition cannot be ordered around its allocation, or None if all can.

    A single clause that constrains *both* a buffer and the buffer's *size*
    (e.g. ``n >= 1 && n <= 2 && a[0] >= 0``) has no correct emission point: the
    length bound must precede the ``int *a = malloc(n * sizeof(int))``
    allocation, the buffer-content predicate must follow the nondet fill, and
    splitting arbitrary C on ``&&`` is unsound under ``||`` precedence.

    Naming the length identifier is *not* itself a size constraint: in
    ``a[n - 1] >= 0`` the `n` is an index into an already-declared buffer, so the
    whole clause emits after the allocation and fill. Only a length identifier
    appearing **outside** a buffer subscript forces pre-allocation ordering, so
    the check strips ``a[...]`` (`_strip_buffer_subscripts`) before looking for
    one -- an index-use is renderable, an index-use combined with a genuine
    bound (``a[n - 1] >= 0 && n >= 1``) is still not.

    This decides *emission ordering* only, never index bounds: whether ``n - 1``
    is in range for ``a[n]`` is ESBMC's to catch in the emitted harness, exactly as
    it already is for the long-accepted constant-index ``a[5] >= 0``.

    Source-free -- it needs only the signature-derived buffers and the spec's
    preconditions -- so it is a `(signature, spec)` guard `renderability_reason`
    owns and `_split_assumptions` reuses: one rule, two callers, one message.
    """
    buffer_names = {buf.name for buf in buffers}
    length_idents = {ident for buf in buffers for ident in identifiers(buf.length)}
    for raw in preconditions:
        stripped = raw.strip()
        if not stripped or not any(references(stripped, n) for n in buffer_names):
            continue
        outside_indices = _strip_buffer_subscripts(stripped, buffer_names)
        if any(references(outside_indices, ident) for ident in length_idents):
            return (
                f"precondition {stripped!r} constrains both a buffer and a "
                "buffer-length identifier outside a subscript index; split it into "
                "separate domain entries so the length bound can precede the "
                "allocation it sizes"
            )
    return None


def _split_assumptions(
    preconditions: Sequence[str], buffers: Sequence[BufferParam]
) -> tuple[list[str], list[str]]:
    """Partition preconditions by whether they reference a buffer parameter.

    A precondition over buffer *contents* (e.g. ``a[0] >= 0``, ``a[n - 1] >= 0``)
    can only be emitted once the buffer is declared and nondet-filled, so it goes
    *after* parameter storage is prepared -- including when its index names the
    length, which by then is a bound scalar. A length/scalar-only precondition
    (e.g. ``len <= 4``)
    must stay *before* the allocation it sizes. Blank entries are dropped.
    Returns ``(pre_buffer, post_buffer)``.

    A single clause constraining *both* a buffer and the buffer's size cannot be
    placed correctly; `_mixed_buffer_length_reason` -- the same guard
    `renderability_reason` consults statically -- names why, raised here as
    `HarnessError`. The caller supplies the length bound and the buffer predicate
    as separate `domain` entries instead. (In practice `render_semantic_harness`
    delegates to `renderability_reason` first, so this raise is the renderer's
    own invariant rather than the only line of defense.)
    """
    mixed = _mixed_buffer_length_reason(preconditions, buffers)
    if mixed is not None:
        raise HarnessError(mixed)
    buffer_names = {buf.name for buf in buffers}
    pre_buffer: list[str] = []
    post_buffer: list[str] = []
    for raw in preconditions:
        stripped = raw.strip()
        if not stripped:
            continue
        if any(references(stripped, name) for name in buffer_names):
            post_buffer.append(stripped)
        else:
            pre_buffer.append(stripped)
    return pre_buffer, post_buffer


def _defines_main(source: str) -> bool:
    """Whether `source` has a genuine, definition-shaped `main`.

    Reuses `find_definition_brace` (comment-aware; requires a `{` body, not
    just the name) rather than a raw `\\bmain\\s*\\(` substring search — the
    latter also matches a bare comment mentioning `main`, which is not code
    at all (issue #95 review).

    Deliberately narrower than "would this fail to compile": a *declaration*
    (prototype) is not itself a second definition the way this check cares
    about, but a signature mismatch between a left-behind prototype and the
    harness's own generated `int main(void) { ... }` is still a hard esbmc
    parse error ("conflicting types"), verified against a live esbmc run —
    this function alone does not catch that. Ensuring no `main`-named
    declaration of any signature reaches here is `Unit.from_path`'s job
    (`orchestrator/ports.py`'s `rename_all_declarations_and_definitions`; it
    renames a declaration alongside every definition), not this render-level
    guard's.
    """
    return find_definition_brace(source, "main") is not None

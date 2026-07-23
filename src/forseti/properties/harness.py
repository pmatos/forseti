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
from collections.abc import Sequence
from dataclasses import dataclass

from .cexpr import derefs_or_subscripts, identifiers, references
from .model import Property, PropertyKind

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


class HarnessError(ValueError):
    """The unit/property cannot be rendered into a valid harness (fail-loud)."""


@dataclass(frozen=True)
class ScalarParam:
    """A scalar arithmetic parameter, drawn from a single ``nondet_<T>()`` call."""

    ctype: str  # type spelling as in the signature, e.g. "int64_t", "unsigned"
    name: str  # identifier the property references, e.g. "x"


@dataclass(frozen=True)
class BufferParam:
    """A pointer parameter backed by a nondet-filled (or output) VLA.

    `length` is a C expression for the element count (usually another param's
    name, e.g. "len"); `out=True` means an output buffer -- allocated but NOT
    nondet-filled, referenced by `name` in the postcondition after the call. A
    single-element output (`length == "1"`) is backed by a scalar and passed by
    address (e.g. utf8's ``uint32_t *cp``); anything else is an array that decays
    to a pointer at the call.
    """

    elem_ctype: str  # e.g. "unsigned char", "int"
    name: str
    length: str
    const: bool = False
    out: bool = False


Param = ScalarParam | BufferParam  # sealed union (repo style)


@dataclass(frozen=True)
class UnitSignature:
    """The C signature of the unit ``path::symbol``.

    The Property (#62) keys the unit as "path::symbol" but carries no types, so
    the signature is supplied here -- parsed by `extract_signature` or provided
    directly by the caller/proposer. `return_ctype` is "void" when the unit
    returns nothing.
    """

    symbol: str
    return_ctype: str
    params: tuple[Param, ...]


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
    `unit_source` that defines its own ``main``, or an unknown Param subtype).
    """
    postcondition = spec.postcondition.strip()
    if not postcondition:
        raise HarnessError("semantic property has an empty postcondition")

    param_names = {p.name for p in signature.params}
    if spec.result_var in param_names:
        raise HarnessError(
            f"result_var {spec.result_var!r} collides with a parameter name"
        )

    returns_void = signature.return_ctype.strip() == "void"
    if returns_void and references(postcondition, spec.result_var):
        raise HarnessError(
            f"postcondition references {spec.result_var!r} but "
            f"{signature.symbol!r} returns void"
        )

    if _defines_main(unit_source):
        raise HarnessError(
            "unit_source defines main; pass the kernel slice, not the example harness"
        )

    reason = renderability_reason(signature, spec)
    if reason is not None:
        raise HarnessError(reason)

    scalars: list[ScalarParam] = []
    buffers: list[BufferParam] = []
    for param in signature.params:
        if isinstance(param, ScalarParam):
            scalars.append(param)
        elif isinstance(param, BufferParam):
            buffers.append(param)
        else:
            raise HarnessError(f"unknown Param subtype: {type(param).__name__}")

    lines: list[str] = [f"#include <{inc}>" for inc in includes]
    lines.append("")
    lines.append(unit_source.strip("\n"))
    lines.append("")
    for ctype in _nondet_ctypes(signature.params):
        lines.append(f"{ctype} {_nondet_slug(ctype)}(void);")
    lines.append("")
    lines.append("int main(void) {")
    for scalar in scalars:
        lines.append(
            f"    {scalar.ctype} {scalar.name} = {_nondet_slug(scalar.ctype)}();"
        )
    pre_buffer, post_buffer = _split_assumptions(spec.preconditions, buffers)
    for pre in pre_buffer:
        lines.append(f"    __ESBMC_assume(({pre}));")
    for buf in buffers:
        lines += _render_buffer(buf)
    for pre in post_buffer:
        lines.append(f"    __ESBMC_assume(({pre}));")
    call = f"{signature.symbol}({', '.join(_call_arg(p) for p in signature.params)})"
    if returns_void:
        lines.append(f"    {call};")
    else:
        lines.append(f"    {signature.return_ctype} {spec.result_var} = {call};")
    lines.append(f'    __ESBMC_assert(({postcondition}), "forseti:semantic");')
    lines.append("    return 0;")
    lines.append("}")
    return "\n".join(lines) + "\n"


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

    The static, parser-free authority on two emission rules the postcondition and
    preconditions must obey -- kept *here*, the module that emits the C, so callers
    (the proposer's static gate, the renderer itself) need not re-derive harness
    internals:

    * A **scalar-backed output** (a trailing single-element ``out`` buffer) is
      bound as a plain scalar passed by address, so the postcondition may only
      *name* it -- dereferencing or subscripting it (``*cp``, ``cp[0]``,
      ``*(cp + 0)``) would deref/subscript a non-pointer and fail to compile.
    * A **precondition** is emitted as ``__ESBMC_assume(...)`` *before* the call,
      so it may not constrain an **output** parameter -- that would preconstrain a
      would-be result on uninitialized storage, masking a unit that never writes
      it; domains constrain inputs only.

    Best-effort and conservative: it may over-reject an exotic postcondition, but a
    rejected *renderable* property is safe whereas emitting un-compilable C is not.
    It does not duplicate the structural guards `render_semantic_harness` raises on
    directly (empty postcondition, `result_var` clash, void return, a `unit_source`
    that defines ``main``).
    """
    output_params = [
        p.name for p in signature.params if isinstance(p, BufferParam) and p.out
    ]
    scalar_outputs = {
        p.name
        for p in signature.params
        if isinstance(p, BufferParam) and _is_scalar_backed(p)
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
        for name in output_params:
            if references(pre, name):
                return (
                    f"domain expr {pre!r} constrains output parameter "
                    f"{name!r} (preconditions apply to inputs only)"
                )
    return None


_STORAGE_SPECIFIERS = frozenset({"static", "inline", "extern", "_Noreturn"})
_INT_TYPE_RE = re.compile(
    r"\b(?:unsigned|signed|int|short|long|char|_Bool|bool"
    r"|s?size_t|ptrdiff_t|u?int(?:_least|_fast)?\d+_t|u?intptr_t|u?intmax_t)\b"
)
_FLOAT_TYPE_RE = re.compile(r"\b(?:float|double)\b")


def extract_signature(unit_source: str, symbol: str) -> UnitSignature:
    """Best-effort regex parse of `symbol`'s signature from `unit_source`.

    No libclang/pycparser (the base install is dependency-free). Isolated from
    `render_semantic_harness` so rendering stays fully testable without C
    parsing. Classifies a pointer param as a `BufferParam`, inferring its length
    from an immediately-following integer parameter (the ``(buf, len)`` idiom); a
    trailing non-const pointer is treated as a single-element output. Covers the
    corpus styles (abs, utf8_decode, murmur3_32); K&R decls and function-pointer
    params are out of scope. Raises `HarnessError` when the definition isn't
    found or a param can't be classified -- non-load-bearing and fail-loud, so a
    parser miss never silently corrupts a harness (the caller can always hand-
    build a `UnitSignature`).
    """
    pattern = re.compile(
        r"([A-Za-z_][\w \t\*]*?[\s\*])" + re.escape(symbol) + r"\s*\(([^)]*)\)\s*\{"
    )
    match = pattern.search(unit_source)
    if match is None:
        raise HarnessError(f"could not find a definition of {symbol!r} in unit_source")
    return_ctype = " ".join(
        t for t in match.group(1).split() if t not in _STORAGE_SPECIFIERS
    )
    if not return_ctype:
        raise HarnessError(f"could not parse the return type of {symbol!r}")
    raws = [_parse_fragment(frag, symbol) for frag in _split_params(match.group(2))]
    return UnitSignature(
        symbol=symbol,
        return_ctype=return_ctype,
        params=_to_params(raws),
    )


def _nondet_slug(ctype: str) -> str:
    """Nondet generator name for a C type: ``int64_t`` -> ``nondet_int64_t``.

    ESBMC models any *undefined* function named ``nondet_*`` as returning a fresh
    unconstrained value of its return type, so the mapping is general -- no fixed
    type enum.
    """
    return "nondet_" + re.sub(r"[^A-Za-z0-9_]", "_", ctype.strip())


def _nondet_ctypes(params: Sequence[Param]) -> list[str]:
    """The distinct nondet-source ctypes, in first-seen order (one prototype each).

    Only nondet-*filled* inputs contribute: scalars always, and non-out buffers
    (their element type); an output buffer is allocated, never filled, so it
    needs no generator.
    """
    ordered: list[str] = []
    for param in params:
        if isinstance(param, ScalarParam):
            ctype = param.ctype.strip()
        elif isinstance(param, BufferParam) and not param.out:
            ctype = param.elem_ctype.strip()
        else:
            continue
        if ctype not in ordered:
            ordered.append(ctype)
    return ordered


def _render_buffer(buf: BufferParam) -> list[str]:
    """Decl (+ nondet fill for inputs) for one buffer param.

    A single-element output is a plain scalar (passed by address at the call
    site); everything else is a VLA sized to the *logical* length, so a read past
    it is a genuine out-of-bounds rather than a read into slack. Input buffers are
    nondet-filled element by element; output buffers are left for the unit to
    write.
    """
    if _is_scalar_backed(buf):
        return [f"    {buf.elem_ctype} {buf.name};"]
    lines = [f"    {buf.elem_ctype} {buf.name}[{buf.length}];"]
    if not buf.out:
        lines.append(
            f"    for (size_t _i = 0; _i < ({buf.length}); _i++) "
            f"{buf.name}[_i] = {_nondet_slug(buf.elem_ctype)}();"
        )
    return lines


def _call_arg(param: Param) -> str:
    """The call-site argument for a param: name, or ``&name`` for a scalar-backed
    output buffer (arrays decay to pointers on their own)."""
    if isinstance(param, ScalarParam):
        return param.name
    if isinstance(param, BufferParam):
        return f"&{param.name}" if _is_scalar_backed(param) else param.name
    raise HarnessError(f"unknown Param subtype: {type(param).__name__}")


def _is_scalar_backed(buf: BufferParam) -> bool:
    return buf.out and buf.length.strip() == "1"


def _split_assumptions(
    preconditions: Sequence[str], buffers: Sequence[BufferParam]
) -> tuple[list[str], list[str]]:
    """Partition preconditions by whether they reference a buffer parameter.

    A precondition over buffer *contents* (e.g. ``a[0] >= 0``) can only be
    emitted once the buffer is declared and nondet-filled, so it goes *after*
    `_render_buffer`; a length/scalar-only precondition (e.g. ``len <= 4``) must
    stay *before* the VLA declaration it sizes. Blank entries are dropped.
    Returns ``(pre_buffer, post_buffer)``.

    A single clause that constrains *both* a buffer and a buffer-length
    identifier (e.g. ``n >= 1 && n <= 2 && a[0] >= 0``) cannot be placed
    correctly — the length bound must precede the VLA, the buffer predicate must
    follow it — so it is rejected with `HarnessError` (splitting arbitrary C on
    ``&&`` is unsound under ``||`` precedence). The caller supplies the length
    bound and the buffer predicate as separate `domain` entries instead.
    """
    buffer_names = {buf.name for buf in buffers}
    length_idents = {ident for buf in buffers for ident in identifiers(buf.length)}
    pre_buffer: list[str] = []
    post_buffer: list[str] = []
    for raw in preconditions:
        stripped = raw.strip()
        if not stripped:
            continue
        if not any(references(stripped, name) for name in buffer_names):
            pre_buffer.append(stripped)
            continue
        if any(references(stripped, ident) for ident in length_idents):
            raise HarnessError(
                f"precondition {stripped!r} constrains both a buffer and a "
                "buffer-length identifier; split it into separate domain entries "
                "so the length bound can precede the VLA it sizes"
            )
        post_buffer.append(stripped)
    return pre_buffer, post_buffer


def _defines_main(source: str) -> bool:
    return re.search(r"\bmain\s*\(", source) is not None


@dataclass(frozen=True)
class _RawParam:
    """A parameter fragment before buffer length/out inference."""

    type_str: str
    name: str
    is_ptr: bool
    is_const: bool
    array_len: str | None


def _split_params(raw: str) -> list[str]:
    """Split a parameter list on top-level commas; ``""``/``void`` -> no params."""
    stripped = raw.strip()
    if stripped in ("", "void"):
        return []
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in stripped:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return [p.strip() for p in parts if p.strip()]


def _parse_fragment(frag: str, symbol: str) -> _RawParam:
    array = re.search(r"\[([^\]]*)\]", frag)
    array_len = (array.group(1).strip() or None) if array is not None else None
    body = (frag[: array.start()] + frag[array.end() :]) if array is not None else frag
    body = body.strip()
    name_match = re.search(r"([A-Za-z_]\w*)\s*$", body)
    if name_match is None:
        raise HarnessError(f"could not parse parameter {frag!r} of {symbol!r}")
    type_str = body[: name_match.start()].strip()
    if not type_str:
        raise HarnessError(f"could not parse the type of parameter {frag!r}")
    return _RawParam(
        type_str=type_str,
        name=name_match.group(1),
        is_ptr="*" in type_str or array is not None,
        is_const=bool(re.search(r"\bconst\b", type_str)),
        array_len=array_len,
    )


def _to_params(raws: Sequence[_RawParam]) -> tuple[Param, ...]:
    params: list[Param] = []
    for i, raw in enumerate(raws):
        if not raw.is_ptr:
            params.append(ScalarParam(ctype=raw.type_str, name=raw.name))
            continue
        nxt = raws[i + 1] if i + 1 < len(raws) else None
        if raw.array_len:
            length, out = raw.array_len, False
        elif nxt is not None and not nxt.is_ptr and _is_integer_type(nxt.type_str):
            length, out = nxt.name, False
        elif nxt is None:
            # a trailing pointer with no length param is a single-element output
            # (e.g. utf8_decode's `uint32_t *cp`); non-const -> written by the unit.
            length, out = "1", not raw.is_const
        else:
            # an interior pointer not followed by its integer length: ambiguous
            # (e.g. `dot(const int *a, const int *b, size_t n)`). Fail loud rather
            # than invent length 1 -- the caller hand-builds a UnitSignature for
            # multi-buffer signatures.
            raise HarnessError(
                f"cannot infer the length of pointer parameter {raw.name!r}: it is "
                "not the (buffer, length) idiom nor a trailing output; supply a "
                "UnitSignature explicitly for multi-buffer signatures"
            )
        params.append(
            BufferParam(
                elem_ctype=_elem_ctype(raw.type_str),
                name=raw.name,
                length=length,
                const=raw.is_const,
                out=out,
            )
        )
    return tuple(params)


def _elem_ctype(type_str: str) -> str:
    """A pointer/array param's element type: drop ``const`` and ``*``."""
    without_const = re.sub(r"\bconst\b", "", type_str).replace("*", " ")
    return " ".join(without_const.split())


def _is_integer_type(type_str: str) -> bool:
    return (
        _INT_TYPE_RE.search(type_str) is not None
        and _FLOAT_TYPE_RE.search(type_str) is None
    )

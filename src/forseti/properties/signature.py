"""The typed C-signature model of a verification unit + a best-effort parser.

A verification unit is keyed ``path::symbol`` (#62) but carries no types, so the
harness renderer needs the target symbol's C signature: its parameters, their
roles (scalar / input buffer / output buffer), and the return type. This module
owns that vocabulary (`ScalarParam`, `BufferParam`, `UnitSignature`) and the
regex parser (`extract_signature`) that recovers it from a unit's source text --
the concern `render_semantic_harness` (`harness.py`) deliberately keeps out of
the renderer so rendering stays fully testable without C parsing.

The dependency is one-directional: ``harness`` imports from here, never the
reverse. `HarnessError` -- the fail-loud error shared by parsing and rendering --
therefore lives here (the parser raises it, `harness` re-imports it); a cycle is
the only alternative. No libclang/pycparser: the base install is dependency-free.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass


class HarnessError(ValueError):
    """The unit/property cannot be rendered into a valid harness (fail-loud)."""


@dataclass(frozen=True)
class ScalarParam:
    """A scalar arithmetic parameter, drawn from a single ``nondet_<T>()`` call."""

    ctype: str  # type spelling as in the signature, e.g. "int64_t", "unsigned"
    name: str  # identifier the property references, e.g. "x"


@dataclass(frozen=True)
class BufferParam:
    """A pointer parameter backed by a nondet-filled (or output) heap buffer.

    `length` is a C expression for the element count (usually another param's
    name, e.g. "len"); `out=True` means an output buffer -- allocated but NOT
    nondet-filled, referenced by `name` in the postcondition after the call. A
    single-element output (`length == "1"`) is backed by a scalar and passed by
    address (e.g. utf8's ``uint32_t *cp``); anything else is `malloc`-allocated,
    already a pointer at the call.
    """

    elem_ctype: str  # e.g. "unsigned char", "int"
    name: str
    length: str
    const: bool = False
    out: bool = False


@dataclass(frozen=True)
class StructParam(BufferParam):
    """A pointer to one of the caller-named struct types (a representation-
    invariant subject, #387): a single instance (`length == "1"`), never a
    `(buffer, length)` pair.

    A `BufferParam` subtype, so `Param` and every consumer that treats a pointer
    as a one-element buffer stay valid; the invariant renderer tests for it first
    and binds one nondet instance passed by address. `elem_ctype` is the type
    (``ring_t`` or ``struct node``); `const` is about the *pointee*
    (``const T *p``), the only thing that decides whether the unit can mutate it --
    ``T *const p`` is still a mutator. `fields` are the member names the unit's
    own definition of the struct declares (empty when it is not visible), so a
    property can be checked for naming a member that does not exist.
    """

    fields: tuple[str, ...] = ()


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

    @property
    def struct_params(self) -> tuple[StructParam, ...]:
        """The struct-pointer parameters (one nondet instance each)."""
        return tuple(p for p in self.params if isinstance(p, StructParam))

    @property
    def param_names(self) -> frozenset[str]:
        """Every parameter name, regardless of role."""
        return frozenset(p.name for p in self.params)

    @property
    def output_param_names(self) -> frozenset[str]:
        """Names of the output parameters -- the `out=True` buffer params.

        The unit *writes* these; the postcondition may name them after the call
        but a precondition may not constrain them (there is nothing there yet). A
        scalar-backed output (`length == "1"`) is an output like any other.
        """
        return frozenset(
            p.name for p in self.params if isinstance(p, BufferParam) and p.out
        )

    @property
    def input_param_names(self) -> frozenset[str]:
        """Names of the input parameters -- every parameter that is not an output."""
        return self.param_names - self.output_param_names


_STORAGE_SPECIFIERS = frozenset({"static", "inline", "extern", "_Noreturn"})
_INT_TYPE_RE = re.compile(
    r"\b(?:unsigned|signed|int|short|long|char|_Bool|bool"
    r"|s?size_t|ptrdiff_t|u?int(?:_least|_fast)?\d+_t|u?intptr_t|u?intmax_t)\b"
)
_FLOAT_TYPE_RE = re.compile(r"\b(?:float|double)\b")


def extract_signature(
    unit_source: str,
    symbol: str,
    *,
    struct_types: Collection[str] = (),
    infer_structs: bool = False,
) -> UnitSignature:
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

    A pointer whose pointee is one of `struct_types` (spelled as in the source,
    ``ring_t`` or ``struct node``) is a `StructParam` instead, whatever follows it:
    ``(ring_t *r, int v)`` is not a buffer of length ``v``. Empty by default, so
    every existing caller parses exactly as before.

    `infer_structs=True` additionally treats a pointer to any struct *defined in
    `unit_source`* (`struct_definitions`) as a `StructParam`, unless an integer
    parameter follows it *and* the body indexes or advances the pointer
    (``(point_t *pts, size_t n)`` with ``pts[i]`` stays the buffer idiom). A struct
    the source does not define keeps the old reading.

    A pointer followed by a ``(lo, hi)`` / ``(start, end)`` / ``(begin, end)`` pair
    is the range idiom: the buffer is ``hi`` long, and a trailing non-const pointer
    of the same element type (a scratch buffer, ``msort``'s ``tmp``) is as long and
    an output, not a single element.
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
    # comments are blanked length-preservingly, so `match` offsets stay valid
    code = _without_comments(unit_source) if infer_structs or struct_types else ""
    defined = _struct_definitions_in(code)
    body = _function_body(code, match.end() - 1) if infer_structs else ""
    return UnitSignature(
        symbol=symbol,
        return_ctype=return_ctype,
        params=_to_params(raws, frozenset(struct_types), defined, infer_structs, body),
    )


_COMMENT_OR_LITERAL = re.compile(
    r"""/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'""", re.DOTALL
)
_STRUCT_HEAD = re.compile(r"\bstruct\b\s*(\w+)?\s*\{")
_FIELD_FUNCTION_POINTER = re.compile(r"\(\s*\*\s*(\w+)")
_FIELD_NOISE = re.compile(r"\[[^\]]*\]|:\s*\w+\s*$|=.*$|\*")
_TYPEDEF_BEFORE = re.compile(r"\btypedef\s*\Z")
_TYPEDEF_TAIL = re.compile(r"\s*([^;{}]*);")


def struct_definitions(unit_source: str) -> dict[str, tuple[str, ...]]:
    """Member names of every struct `unit_source` defines, keyed by spelling.

    ``typedef struct {...} ring_t;`` is keyed ``ring_t``; ``struct node {...}`` is
    keyed ``struct node``; ``typedef struct node {...} node_t;`` gets both. Regex,
    comment-aware and best-effort like the rest of this parser: a member of a
    nested struct is not listed (the nested member is named, not its contents), and
    a struct only reachable through an ``#include`` is not seen. A body with a
    member it cannot name (an anonymous union/struct member, an ``__attribute__``)
    yields an empty field list, which a caller reads as "unknown", never as "these
    are all the fields".
    """
    return _struct_definitions_in(_without_comments(unit_source))


def _struct_definitions_in(text: str) -> dict[str, tuple[str, ...]]:
    found: dict[str, tuple[str, ...]] = {}
    for head in _STRUCT_HEAD.finditer(text):
        close = _matching_brace(text, head.end() - 1)
        if close is None:
            continue
        fields = _struct_fields(text[head.end() : close])
        if head.group(1):
            found.setdefault(f"struct {head.group(1)}", fields)
        if _TYPEDEF_BEFORE.search(text, max(0, head.start() - 64), head.start()):
            tail = _TYPEDEF_TAIL.match(text, close + 1)
            for declarator in (tail.group(1) if tail else "").split(","):
                name = declarator.strip()
                if re.fullmatch(r"[A-Za-z_]\w*", name):
                    found.setdefault(name, fields)
    return found


def _without_comments(text: str) -> str:
    """`text` with comments blanked to spaces (same length); literals are kept whole."""
    return _COMMENT_OR_LITERAL.sub(
        lambda m: m.group(0) if m.group(0)[0] in "\"'" else " " * len(m.group(0)), text
    )


def _function_body(text: str, open_at: int) -> str:
    return text[open_at : _matching_brace(text, open_at)]


def _matching_brace(text: str, open_at: int) -> int | None:
    depth = 0
    for i in range(open_at, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    return None


def _struct_fields(body: str) -> tuple[str, ...]:
    flat: list[str] = []
    depth = 0
    for ch in body:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        elif depth == 0:
            flat.append(ch)
    names: list[str] = []
    for declaration in "".join(flat).split(";")[:-1]:
        declared = _declared_names(declaration)
        if declared is None:
            return ()
        names += declared
    return tuple(dict.fromkeys(names))


def _declared_names(declaration: str) -> list[str] | None:
    """The member names one declaration declares, or None if one cannot be named."""
    if pointer := _FIELD_FUNCTION_POINTER.search(declaration):
        return [pointer.group(1)]
    names: list[str] = []
    for index, declarator in enumerate(_split_params(declaration)):
        words = _FIELD_NOISE.sub(" ", declarator).split()
        # the first declarator carries the type, so a lone word there is an
        # unnamed member (``int : 3``, an anonymous union), not a named one
        if len(words) <= (1 if index == 0 else 0) or not re.fullmatch(
            r"\w+", words[-1]
        ):
            return None
        names.append(words[-1])
    return names


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


def _to_params(
    raws: Sequence[_RawParam],
    struct_types: frozenset[str],
    defined: Mapping[str, tuple[str, ...]],
    infer: bool,
    body: str,
) -> tuple[Param, ...]:
    params: list[Param] = []
    scratch_of: tuple[str, str] | None = None
    for i, raw in enumerate(raws):
        if not raw.is_ptr:
            params.append(ScalarParam(ctype=raw.type_str, name=raw.name))
            continue
        pointee = _elem_ctype(raw.type_str)
        nxt = raws[i + 1] if i + 1 < len(raws) else None
        single = raw.array_len is None and raw.type_str.count("*") == 1
        if single and (
            pointee in struct_types
            or (infer and pointee in defined and not _is_buffer_idiom(raw, nxt, body))
        ):
            params.append(
                StructParam(
                    elem_ctype=pointee,
                    name=raw.name,
                    length="1",
                    const=_pointee_is_const(raw.type_str),
                    fields=defined.get(pointee, ()),
                )
            )
            continue
        range_upper = _range_upper_bound(raws[i + 1 : i + 3])
        if raw.array_len:
            length, out = raw.array_len, False
        elif range_upper is not None:
            length, out = range_upper, False
        elif nxt is not None and not nxt.is_ptr and _is_integer_type(nxt.type_str):
            length, out = nxt.name, False
        elif nxt is None and scratch_of is not None and scratch_of[0] == pointee:
            length, out = scratch_of[1], True
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
        if range_upper is not None and scratch_of is None:
            scratch_of = (pointee, range_upper)
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


_RANGE_BOUNDS = (("lo", "hi"), ("start", "end"), ("begin", "end"))


def _range_upper_bound(following: Sequence[_RawParam]) -> str | None:
    """The upper-bound param when a pointer is followed by a ``(lo, hi)`` pair.

    ``msort(int *a, int lo, int hi, ...)`` addresses ``a[lo..hi)``, so the buffer
    holds ``hi`` elements; reading ``lo`` as its length (the ``(buf, len)`` idiom)
    allocates it too short and every property over it fails out of bounds (#425).
    Matched on the conventional names only, so ``(buf, n, m)`` keeps its reading.
    """
    if len(following) < 2 or any(
        p.is_ptr or not _is_integer_type(p.type_str) for p in following
    ):
        return None
    lower, upper = following
    return upper.name if (lower.name, upper.name) in _RANGE_BOUNDS else None


def _is_buffer_idiom(raw: _RawParam, nxt: _RawParam | None, body: str) -> bool:
    """A struct pointer followed by an integer that the body treats as an array.

    ``(point_t *pts, size_t n)`` with ``pts[i]`` is a buffer of ``n`` structs;
    ``(ring_t *r, int n)`` with ``r->count`` is one instance and a scalar.
    """
    if nxt is None or nxt.is_ptr or not _is_integer_type(nxt.type_str):
        return False
    name = re.escape(raw.name)
    return re.search(rf"\b{name}\s*\[|\b{name}\s*\+|\+\+\s*{name}\b", body) is not None


def _elem_ctype(type_str: str) -> str:
    """A pointer/array param's element type: drop ``const`` and ``*``."""
    without_const = re.sub(r"\bconst\b", "", type_str).replace("*", " ")
    return " ".join(without_const.split())


def _pointee_is_const(type_str: str) -> bool:
    """``const T *`` (a ``const`` before the ``*``), not ``T *const``."""
    return re.search(r"\bconst\b[^*]*\*", type_str) is not None


def _is_integer_type(type_str: str) -> bool:
    return (
        _INT_TYPE_RE.search(type_str) is not None
        and _FLOAT_TYPE_RE.search(type_str) is None
    )

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
from collections.abc import Sequence
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

"""Locate a C function's body and tokenize it, for the mutation operator kit.

Everything here scans a *masked* view of the source (comments, string/char
literals and preprocessor lines blanked to spaces, offsets preserved), so an
operator spelled inside a comment or a literal is never a mutation site, while
every offset found is still valid against the original text.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from forseti.esbmc import find_definition_brace, mask_comments
from forseti.esbmc.preprocessor import mask_string_literals

_DIRECTIVE_RE = re.compile(r"^[ \t]*#(?:\\\n|[^\n])*", re.MULTILINE)

_TOKEN_RE = re.compile(
    r"""
    (?P<num>\.?[0-9](?:[eEpP][+-]|[A-Za-z0-9_.])*)
    |(?P<id>[A-Za-z_$][A-Za-z0-9_$]*)
    |(?P<op><<=|>>=|\.\.\.|->|\+\+|--|<<|>>|<=|>=|==|!=|&&|\|\||[-+*/%&|^]=|\S)
    """,
    re.VERBOSE,
)

_TYPE_NOISE = frozenset(
    {
        "static",
        "inline",
        "__inline",
        "__inline__",
        "extern",
        "const",
        "volatile",
        "restrict",
        "__restrict",
        "_Noreturn",
    }
)
_OPENERS = frozenset("([{")
_CLOSERS = frozenset(")]}")
_ATTRIBUTE_RE = re.compile(r"__attribute__\s*\(\(.*?\)\)", re.DOTALL)


@dataclass(frozen=True)
class Token:
    kind: str
    text: str
    start: int

    @property
    def end(self) -> int:
        return self.start + len(self.text)


@dataclass(frozen=True)
class OutputParam:
    """A pointer/array parameter that can be written through (`*p`, `p[i]`)."""

    name: str
    elem_ctype: str


@dataclass(frozen=True)
class FunctionBody:
    """The target function's body span and the signature facts the operators need.

    `open_brace`/`close_brace` index the body's ``{`` and its matching ``}`` in the
    original text. `return_ctype` is ``None`` when the function returns ``void``,
    a pointer, or a type that cannot be read off the header text.
    """

    open_brace: int
    close_brace: int
    tokens: tuple[Token, ...]
    return_ctype: str | None
    output_params: tuple[OutputParam, ...]


class BodyNotFound(ValueError):
    """The symbol has no isolable definition in the source."""


def masked_view(source: str) -> str:
    """`source` with comments, literals and directives blanked, offsets kept."""
    masked = mask_string_literals(mask_comments(source))
    return _DIRECTIVE_RE.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), masked)


def tokenize(masked: str, start: int, end: int) -> tuple[Token, ...]:
    tokens: list[Token] = []
    for match in _TOKEN_RE.finditer(masked, start, end):
        kind = match.lastgroup or "op"
        tokens.append(Token(kind, match.group(), match.start()))
    return tuple(tokens)


def _match_close(masked: str, open_idx: int, open_ch: str, close_ch: str) -> int | None:
    depth = 0
    for i in range(open_idx, len(masked)):
        ch = masked[i]
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return i
    return None


def _param_list(masked: str, symbol: str, brace: int) -> tuple[int, str] | None:
    """``(name_start, params_text)`` of the last ``symbol (`` before `brace`."""
    found = None
    for match in re.finditer(rf"\b{re.escape(symbol)}\s*\(", masked[:brace]):
        found = match
    if found is None:
        return None
    close = _match_close(masked, found.end() - 1, "(", ")")
    if close is None or close > brace:
        return None
    return found.start(), masked[found.end() : close]


def _normalize_type(text: str) -> str:
    text = _ATTRIBUTE_RE.sub(" ", text)
    words = [w for w in re.split(r"\s+", text.replace("*", " * ")) if w]
    return " ".join(w for w in words if w not in _TYPE_NOISE)


def _return_ctype(masked: str, name_start: int) -> str | None:
    head = masked[:name_start]
    cut = max(head.rfind(";"), head.rfind("}"), head.rfind("{"))
    ctype = _normalize_type(head[cut + 1 :])
    if not ctype or ctype == "void" or "*" in ctype:
        return None
    return ctype


def _split_params(params: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in params:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return [p.strip() for p in parts if p.strip()]


_PARAM_RE = re.compile(
    r"^(?P<type>.*?)(?P<name>[A-Za-z_]\w*)\s*(?P<dims>(?:\[[^\]]*\]\s*)*)$", re.DOTALL
)


def _output_param(param: str) -> OutputParam | None:
    match = _PARAM_RE.match(param)
    if match is None:
        return None
    type_text, name, dims = match["type"], match["name"], match["dims"]
    levels = type_text.count("*") + dims.count("[")
    if levels != 1:
        return None
    pointee = type_text[: type_text.rfind("*")] if "*" in type_text else type_text
    if re.search(r"\bconst\b", pointee):
        return None
    elem = _normalize_type(type_text)
    elem = " ".join(w for w in elem.split() if w != "*")
    if not elem or elem == "void":
        return None
    return OutputParam(name, elem)


def locate_body(source: str, symbol: str) -> FunctionBody:
    """The body of `symbol`'s definition in `source`.

    Raises `BodyNotFound` when no definition is isolable or its braces do not
    balance on the masked text.
    """
    brace = find_definition_brace(source, symbol)
    if brace is None:
        raise BodyNotFound(f"no definition of {symbol!r} found")
    masked = masked_view(source)
    close = _match_close(masked, brace, "{", "}")
    if close is None:
        raise BodyNotFound(f"unbalanced braces in the body of {symbol!r}")
    located = _param_list(masked, symbol, brace)
    if located is None:
        raise BodyNotFound(f"cannot read the signature of {symbol!r}")
    name_start, params = located
    outputs = tuple(
        out for p in _split_params(params) if (out := _output_param(p)) is not None
    )
    return FunctionBody(
        open_brace=brace,
        close_brace=close,
        tokens=tokenize(masked, brace + 1, close),
        return_ctype=_return_ctype(masked, name_start),
        output_params=outputs,
    )


def match_close(
    tokens: Sequence[Token], idx: int, open_text: str, close_text: str
) -> int | None:
    """Index of the token closing the `open_text` at `idx`, or ``None``."""
    depth = 0
    for i in range(idx, len(tokens)):
        if tokens[i].text == open_text:
            depth += 1
        elif tokens[i].text == close_text:
            depth -= 1
            if depth == 0:
                return i
    return None


def statement_end(tokens: Sequence[Token], idx: int) -> int | None:
    """Index of the first top-level ``;`` at or after `idx`, or ``None`` when a
    closer or a top-level comma comes first (not a plain expression statement)."""
    depth = 0
    for i in range(idx, len(tokens)):
        text = tokens[i].text
        if text in _OPENERS:
            depth += 1
        elif text in _CLOSERS:
            depth -= 1
            if depth < 0:
                return None
        elif text == ";" and depth == 0:
            return i
        elif text == "," and depth == 0:
            return None
    return None

"""Deterministic mutants of one C function, for differential grading (W3).

A property is scored by *holds on the real code AND breaks on mutants*; this
module produces the mutants. Five v1 operators rewrite tokens inside the target
function's body only -- the rest of the translation unit stays byte-identical --
and every emitted mutant has passed a validity check (by default: ESBMC's own
parser still accepts it and still defines the target). A mutant that fails the
check is dropped and counted, never scored.

Mutation is token-level on a comment/literal-masked view of the source. Cheap
syntactic mutants may be semantically equivalent to the original; nothing here
claims otherwise -- it only guarantees they compile. Sites are taken from every
preprocessor arm, so a mutant of code the preprocessor drops (an inactive
``#if`` arm) is also equivalent to the original.
"""

from __future__ import annotations

import random
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from forseti.esbmc import ListUnitsError, list_units

from .body import (
    BodyNotFound,
    FunctionBody,
    Token,
    locate_body,
    match_close,
    statement_end,
)

RELATIONAL_FLIP = "relational-flip"
ARITH_SWAP = "arith-swap"
CONST_OFF_BY_ONE = "const-off-by-one"
GUARD_DROP = "guard-drop"
OUTPUT_HAVOC = "output-havoc"

OPERATORS: tuple[str, ...] = (
    RELATIONAL_FLIP,
    ARITH_SWAP,
    CONST_OFF_BY_ONE,
    GUARD_DROP,
    OUTPUT_HAVOC,
)

_RELATIONAL = {
    "<": "<=",
    "<=": "<",
    ">": ">=",
    ">=": ">",
    "==": "!=",
    "!=": "==",
}
_ARITH = {"+": "-", "-": "+", "+=": "-=", "-=": "+="}
_NOT_AN_OPERAND = frozenset(
    {"return", "case", "else", "do", "sizeof", "goto", "typeof", "_Alignof"}
)
_INT_LITERAL_RE = re.compile(
    r"(?P<body>0[xX][0-9a-fA-F]+|0[bB][01]+|[0-9]+)(?P<suffix>[uUlL]*)"
)
_GUARD_PREVIEW = 60
_DIRECTIVE_LINE_RE = re.compile(r"^[ \t]*#", re.MULTILINE)
_STATEMENT_START = ("{", ";", "}", ")", "else", ":")


class MutationError(RuntimeError):
    """The mutation kit could not run (unknown symbol, esbmc missing, bad input)."""


@dataclass(frozen=True)
class Edit:
    """Replace ``source[start:end]`` with `text`; always inside the target body."""

    start: int
    end: int
    text: str


@dataclass(frozen=True)
class MutantId:
    """``operator@line:column#ordinal`` -- stable across runs on the same source.

    `ordinal` numbers the mutants one operator produces at one location (the
    ``+1`` and ``-1`` of a constant share line and column).
    """

    operator: str
    line: int
    column: int
    ordinal: int

    def __str__(self) -> str:
        return f"{self.operator}@{self.line}:{self.column}#{self.ordinal}"


@dataclass(frozen=True)
class Mutant:
    id: MutantId
    source: str
    description: str
    edits: tuple[Edit, ...]

    @property
    def operator(self) -> str:
        return self.id.operator


class MutationResult(NamedTuple):
    """`dropped` counts candidates the validity check rejected (never scored)."""

    mutants: tuple[Mutant, ...]
    dropped: int


@dataclass(frozen=True)
class _Candidate:
    op_index: int
    offset: int
    edits: tuple[Edit, ...]
    description: str


def _line_col(source: str, offset: int) -> tuple[int, int]:
    line = source.count("\n", 0, offset) + 1
    return line, offset - (source.rfind("\n", 0, offset) + 1) + 1


def _nondet_name(ctype: str) -> str:
    return "nondet_" + re.sub(r"[^A-Za-z0-9_]", "_", ctype.strip())


def _prototype(ctype: str, at: int) -> Edit:
    return Edit(at, at, f" {ctype} {_nondet_name(ctype)}(void);")


def _relational(source: str, body: FunctionBody, op_index: int) -> Iterable[_Candidate]:
    for tok in body.tokens:
        flipped = _RELATIONAL.get(tok.text) if tok.kind == "op" else None
        if flipped is None:
            continue
        line = _line_col(source, tok.start)[0]
        yield _Candidate(
            op_index,
            tok.start,
            (Edit(tok.start, tok.end, flipped),),
            f"`{tok.text}` -> `{flipped}` at line {line}",
        )


def _is_binary(tokens: Sequence[Token], i: int) -> bool:
    if i == 0:
        return False
    prev = tokens[i - 1]
    if prev.kind == "num":
        return True
    if prev.kind == "id":
        return prev.text not in _NOT_AN_OPERAND
    return prev.text in (")", "]", "++", "--")


def _arith(source: str, body: FunctionBody, op_index: int) -> Iterable[_Candidate]:
    tokens = body.tokens
    for i, tok in enumerate(tokens):
        swapped = _ARITH.get(tok.text) if tok.kind == "op" else None
        if swapped is None:
            continue
        if tok.text in ("+", "-") and not _is_binary(tokens, i):
            continue
        line = _line_col(source, tok.start)[0]
        yield _Candidate(
            op_index,
            tok.start,
            (Edit(tok.start, tok.end, swapped),),
            f"`{tok.text}` -> `{swapped}` at line {line}",
        )


def _int_value(text: str) -> int | None:
    match = _INT_LITERAL_RE.fullmatch(text)
    if match is None:
        return None
    digits = match["body"]
    try:
        if len(digits) > 1 and digits[0] == "0" and digits[1].isdigit():
            return int(digits, 8)
        return int(digits, 0)
    except ValueError:
        return None


def _const(source: str, body: FunctionBody, op_index: int) -> Iterable[_Candidate]:
    for tok in body.tokens:
        if tok.kind != "num":
            continue
        value = _int_value(tok.text)
        if value is None:
            continue
        line = _line_col(source, tok.start)[0]
        for sign, delta in (("+", 1), ("-", -1)):
            yield _Candidate(
                op_index,
                tok.start,
                (Edit(tok.start, tok.end, f"({tok.text} {sign} 1)"),),
                f"`{tok.text}` -> `{value + delta}` at line {line}",
            )


def _guard_end(tokens: Sequence[Token], i: int) -> int | None:
    """Token index ending the ``if (c) return ...;`` guard at `i`, else ``None``."""
    if tokens[i].text != "if" or i + 1 >= len(tokens) or tokens[i + 1].text != "(":
        return None
    close = match_close(tokens, i + 1, "(", ")")
    if close is None or close + 1 >= len(tokens):
        return None
    j = close + 1
    braced = tokens[j].text == "{"
    if braced:
        j += 1
    if j >= len(tokens) or tokens[j].text != "return":
        return None
    end = statement_end(tokens, j)
    if end is None:
        return None
    if braced:
        end += 1
        if end >= len(tokens) or tokens[end].text != "}":
            return None
    return end


def _guard_drop(source: str, body: FunctionBody, op_index: int) -> Iterable[_Candidate]:
    tokens = body.tokens
    for i, tok in enumerate(tokens):
        end = _guard_end(tokens, i)
        if end is None:
            continue
        if i > 0 and tokens[i - 1].text == "else":
            continue
        if end + 1 < len(tokens) and tokens[end + 1].text == "else":
            continue
        span_end = tokens[end].end
        if _DIRECTIVE_LINE_RE.search(source, tok.start, span_end):
            continue
        standalone = i == 0 or tokens[i - 1].text in ("{", ";", "}")
        text = " ".join(source[tok.start : span_end].split())
        if len(text) > _GUARD_PREVIEW:
            text = text[: _GUARD_PREVIEW - 3] + "..."
        line = _line_col(source, tok.start)[0]
        yield _Candidate(
            op_index,
            tok.start,
            (Edit(tok.start, span_end, "" if standalone else ";"),),
            f"drop guard `{text}` at line {line}",
        )


def _havoc_candidate(
    source: str,
    body: FunctionBody,
    op_index: int,
    tokens: Sequence[Token],
    value_start: int,
    ctype: str,
    what: str,
) -> _Candidate | None:
    """Replace the expression statement starting at token `value_start` with a
    nondet call of `ctype`, or ``None`` if it is not a plain, non-havoc one."""
    end = statement_end(tokens, value_start)
    if end is None or end == value_start:
        return None
    call = f"{_nondet_name(ctype)}()"
    start, stop = tokens[value_start].start, tokens[end - 1].end
    if source[start:stop] == call:
        return None
    line = _line_col(source, start)[0]
    return _Candidate(
        op_index,
        start,
        (Edit(start, stop, call), _prototype(ctype, body.open_brace + 1)),
        f"havoc {what} with `{call}` at line {line}",
    )


def _havoc_returns(
    source: str, body: FunctionBody, op_index: int
) -> Iterable[_Candidate]:
    ctype = body.return_ctype
    if ctype is None:
        return
    tokens = body.tokens
    for i, tok in enumerate(tokens):
        if tok.text != "return" or tok.kind != "id" or i + 1 >= len(tokens):
            continue
        cand = _havoc_candidate(
            source, body, op_index, tokens, i + 1, ctype, "returned value"
        )
        if cand is not None:
            yield cand


def _havoc_outputs(
    source: str, body: FunctionBody, op_index: int
) -> Iterable[_Candidate]:
    outputs = {out.name: out for out in body.output_params}
    tokens = body.tokens
    for i, tok in enumerate(tokens):
        if i > 0 and tokens[i - 1].text not in _STATEMENT_START:
            continue
        eq = None
        if tok.text == "*" and i + 2 < len(tokens) and tokens[i + 1].text in outputs:
            name, eq = tokens[i + 1].text, i + 2
        elif tok.text in outputs and i + 1 < len(tokens) and tokens[i + 1].text == "[":
            name = tok.text
            close = match_close(tokens, i + 1, "[", "]")
            eq = None if close is None else close + 1
        if eq is None or eq >= len(tokens) or tokens[eq].text != "=":
            continue
        cand = _havoc_candidate(
            source,
            body,
            op_index,
            tokens,
            eq + 1,
            outputs[name].elem_ctype,
            f"write through `{name}`",
        )
        if cand is not None:
            yield cand


def _havoc(source: str, body: FunctionBody, op_index: int) -> Iterable[_Candidate]:
    yield from _havoc_outputs(source, body, op_index)
    yield from _havoc_returns(source, body, op_index)


_GENERATORS: dict[str, Callable[[str, FunctionBody, int], Iterable[_Candidate]]] = {
    RELATIONAL_FLIP: _relational,
    ARITH_SWAP: _arith,
    CONST_OFF_BY_ONE: _const,
    GUARD_DROP: _guard_drop,
    OUTPUT_HAVOC: _havoc,
}


def _apply(source: str, edits: Sequence[Edit]) -> str:
    out = source
    for edit in sorted(edits, key=lambda e: (e.start, e.end), reverse=True):
        out = out[: edit.start] + edit.text + out[edit.end :]
    return out


def candidates(
    source: str, body: FunctionBody, operators: Iterable[str]
) -> list[_Candidate]:
    wanted = set(operators)
    unknown = wanted - set(OPERATORS)
    if unknown:
        raise ValueError(f"unknown mutation operator(s): {sorted(unknown)}")
    found: list[_Candidate] = []
    for index, name in enumerate(OPERATORS):
        if name in wanted:
            found.extend(_GENERATORS[name](source, body, index))
    found.sort(key=lambda c: (c.offset, c.op_index))
    return found


def _assign_ids(source: str, cands: Sequence[_Candidate]) -> list[MutantId]:
    """Ids over *all* candidates, so a mutant's id does not depend on the cap."""
    ordinals: dict[tuple[str, int, int], int] = {}
    ids: list[MutantId] = []
    for cand in cands:
        line, column = _line_col(source, cand.offset)
        name = OPERATORS[cand.op_index]
        ordinal = ordinals.get((name, line, column), 0)
        ordinals[(name, line, column)] = ordinal + 1
        ids.append(MutantId(name, line, column, ordinal))
    return ids


def generate_mutants(
    source: str,
    symbol: str,
    *,
    validate: Callable[[str], bool],
    operators: Iterable[str] = OPERATORS,
    cap: int | None = None,
    seed: int = 0,
) -> MutationResult:
    """Mutants of `symbol`'s body in `source`, in (offset, operator order) order.

    `validate` is the validity oracle: a mutant it rejects is dropped and counted
    in `MutationResult.dropped`, never returned. With `cap`, candidates are tried
    in a `seed`-fixed random order until `cap` valid mutants are collected (so a
    cap spares the validator, the expensive step), then returned in canonical
    order; `dropped` then counts only the candidates tried. The same source,
    operator set, cap and seed give the same mutants in the same order.
    """
    try:
        body = locate_body(source, symbol)
    except BodyNotFound as exc:
        raise MutationError(str(exc)) from exc
    cands = candidates(source, body, operators)
    order = list(range(len(cands)))
    if cap is not None:
        random.Random(seed).shuffle(order)
    kept: dict[int, str] = {}
    dropped = 0
    for idx in order:
        if cap is not None and len(kept) >= cap:
            break
        mutated = _apply(source, cands[idx].edits)
        if validate(mutated):
            kept[idx] = mutated
        else:
            dropped += 1
    ids = _assign_ids(source, cands)
    mutants = [
        Mutant(ids[idx], kept[idx], cands[idx].description, cands[idx].edits)
        for idx in sorted(kept)
    ]
    return MutationResult(tuple(mutants), dropped)


def esbmc_validator(
    symbol: str,
    *,
    suffix: str = ".c",
    esbmc_bin: str = "esbmc",
    timeout_s: float = 30.0,
    extra_flags: Sequence[str] = (),
) -> Callable[[str], bool]:
    """A validity oracle: ESBMC parses the source and it still defines `symbol`."""
    if shutil.which(esbmc_bin) is None:
        raise MutationError(f"esbmc binary not found: {esbmc_bin}")

    def validate(source: str) -> bool:
        with tempfile.TemporaryDirectory(prefix="forseti-mutant-") as tmp:
            path = Path(tmp) / f"mutant{suffix}"
            path.write_text(source)
            try:
                units = list_units(
                    path,
                    esbmc_bin=esbmc_bin,
                    timeout_s=timeout_s,
                    extra_flags=extra_flags,
                )
            except ListUnitsError as exc:
                if isinstance(exc.__cause__, OSError | subprocess.TimeoutExpired):
                    raise MutationError(f"cannot run {esbmc_bin}: {exc}") from exc
                return False
        return any(unit.name == symbol for unit in units)

    return validate


def mutate_function(
    path: Path,
    symbol: str,
    *,
    operators: Iterable[str] = OPERATORS,
    cap: int | None = None,
    seed: int = 0,
    esbmc_bin: str = "esbmc",
    timeout_s: float = 30.0,
    extra_flags: Sequence[str] = (),
) -> MutationResult:
    """`generate_mutants` for the unit ``path::symbol``, validated with ESBMC.

    Raises `MutationError` if the unmutated file does not itself parse, since
    every mutant would then be dropped and the count would mean nothing.
    """
    source = path.read_text()
    validate = esbmc_validator(
        symbol,
        suffix=path.suffix or ".c",
        esbmc_bin=esbmc_bin,
        timeout_s=timeout_s,
        extra_flags=(f"-I{path.resolve().parent}", *extra_flags),
    )
    if not validate(source):
        raise MutationError(f"{path}: the original does not parse as defining {symbol}")
    return generate_mutants(
        source, symbol, validate=validate, operators=operators, cap=cap, seed=seed
    )

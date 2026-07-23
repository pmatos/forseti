"""Lexical analysis of the C boolean expressions used in properties.

A candidate property is a single C boolean expression (its postcondition) plus
zero or more precondition clauses. Two callers reason over that text: the
proposer's `validate_candidate` (which identifiers does it name? is it a pure
expression or does it smuggle a side effect?) and the harness writer (does a
clause reference a given buffer/length name? does it dereference a scalar-backed
output?). Both need the same C-token rules, so they live here -- one home for
"what counts as an identifier / a reference / a safe expression / a pointer use"
-- instead of a regex apiece drifting out of step across the two modules.

The analysis is deliberately lexical, not a parser: `references`/
`derefs_or_subscripts` are best-effort over well-formed clauses, and
`unsafe_reason` is a conservative reject-list, not a grammar.
"""

from __future__ import annotations

import re

_NUMERIC_LITERAL = re.compile(r"\b0[xX][0-9A-Fa-f]+[uUlL]*\b|\b\d+[uUlL]*\b")
_IDENT = re.compile(r"[A-Za-z_]\w*")
_CALL = re.compile(r"[A-Za-z_]\w*\s*\(")
_RELATIONAL = re.compile(r"[=!<>]=")


def identifiers(expr: str) -> list[str]:
    """Identifier tokens in `expr`, in first-seen order, numeric literals removed.

    Stripping literals first keeps a hex constant like ``0x1F`` from being misread
    as an identifier ``x1F``.
    """
    return _IDENT.findall(_NUMERIC_LITERAL.sub(" ", expr))


def references(expr: str, ident: str) -> bool:
    """True if `expr` names the identifier `ident` as a whole word.

    Word-boundary matching, so ``result`` does not match ``result2`` and the ``u``
    suffix inside ``10u`` is not read as a reference to an output named ``u``.
    """
    return re.search(rf"\b{re.escape(ident)}\b", expr) is not None


def unsafe_reason(expr: str) -> str | None:
    """None if `expr` is a pure C boolean expression, else why it is rejected.

    Blocks statement separators, assignments (a bare ``=`` that is not part of a
    relational operator), increment/decrement operators, backticks, function
    calls, and unbalanced parentheses -- everything that would let a "property"
    smuggle in a side effect. ``++``/``--`` matter because a ``domain`` clause is
    emitted as ``__ESBMC_assume((<expr>))`` *before* the call, so mutating an
    input there would change the value actually passed to the unit.
    """
    if ";" in expr:
        return "contains ';'"
    if "`" in expr:
        return "contains a backtick"
    if "++" in expr or "--" in expr:
        return "contains an increment/decrement operator"
    if "<<=" in expr or ">>=" in expr:
        # `_RELATIONAL` would strip the `<=`/`>=` embedded in `<<=`/`>>=`, hiding
        # the compound assignment from the bare-`=` check below; catch it first.
        return "contains a compound assignment operator"
    if "=" in _RELATIONAL.sub("", expr):
        return "contains an assignment"
    if _CALL.search(expr):
        return "contains a function call"
    if expr.count("(") != expr.count(")"):
        return "unbalanced parentheses"
    return None


def derefs_or_subscripts(expr: str, name: str) -> bool:
    """True if `name` is subscripted (``name[...]``) or dereferenced (``*name`` or
    ``*(...name...)``) in `expr` -- the pointer uses a scalar binding cannot compile.

    Best-effort: the parenthesized form catches ``*(cp + 0)`` / ``*(cp)`` that a
    bare ``*name`` test misses; it does not see arbitrarily nested derefs.
    """
    n = re.escape(name)
    return (
        re.search(rf"\b{n}\s*\[", expr) is not None
        or re.search(rf"\*\s*{n}\b", expr) is not None
        or re.search(rf"\*\s*\([^()]*\b{n}\b", expr) is not None
    )

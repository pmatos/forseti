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
# The last non-blank char before a `*` when it is the *right* operand of binary
# multiplication: an identifier char, a digit, or a closing `)`/`]`. Its absence
# (start of clause, or an operator) marks the `*` as a unary pointer dereference.
# A closing `)` is the ambiguous case, resolved by `_CLOSING_CAST` below.
_OPERAND_TAIL = re.compile(r"[\w)\]]$")
# The pre-`*` text (right-stripped) whose closing `)` ends a C *cast* -- a
# parenthesized type such as `(int)`, `(uint32_t)`, `(char *)`, `(const unsigned
# int)` -- rather than a value group like `(a + b)` or `(a * b)`. A cast's `)` is
# not an operand, so a `*` after it is a unary dereference, not multiplication. The
# negative lookbehind rejects `name(...)` (a call, whose `)` yields a value); the
# body admits only type-shaped tokens -- identifier/keyword words (which may hold
# digits, as in `uint32_t`) and trailing pointer `*`s -- so any group carrying an
# operator is left as multiplication.
_CLOSING_CAST = re.compile(r"(?<!\w)\(\s*[A-Za-z_]\w*(?:\s+[A-Za-z_]\w*)*\s*\**\s*\)$")


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

    A leading ``*`` counts as a dereference only when it is *unary*: an operand
    (identifier, number, ``)``, or ``]``) immediately before it -- across
    whitespace -- makes the ``*`` binary multiplication, so ``result * cp`` and
    ``result * (cp + 1)`` name `cp` as a scalar factor and render fine, whereas
    ``*cp`` / ``*(cp + 0)`` / ``*(cp)`` do not. A closing *cast* ``)`` is not an
    operand: ``(int)*cp`` and ``(uint32_t) * cp`` unary-dereference `cp`, so they
    are flagged even though a ``)`` precedes the ``*``. Best-effort: the
    parenthesized form catches derefs a bare ``*name`` test misses; it does not see
    arbitrarily nested derefs, and a parenthesized single name (``(x) * cp``, a
    typedef-name cast lexically) is read as a cast -- the conservative reject side.
    """
    n = re.escape(name)
    if re.search(rf"\b{n}\s*\[", expr) is not None:
        return True
    for star in re.finditer(r"\*\s*", expr):
        head = expr[: star.start()].rstrip()
        if _OPERAND_TAIL.search(head) and not _CLOSING_CAST.search(head):
            continue  # an operand (not a cast's `)`) precedes this `*`: not a deref
        rest = expr[star.end() :]
        if re.match(rf"{n}\b", rest) or re.match(rf"\([^()]*\b{n}\b", rest):
            return True
    return False

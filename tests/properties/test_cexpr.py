"""Tests for `cexpr`: lexical analysis of the C boolean expressions used in
properties and preconditions.

These pin the module's public seam directly -- identifier tokenization,
word-boundary reference detection, the side-effect safety gate, and pointer-use
detection -- the rules the proposer's validation and the harness writer both
consume. Expected values come from the C lexical spec (what a hex/suffixed
literal is, what a word boundary is, what a dereference is), not from re-running
the regexes, so a test can disagree with the code.
"""

from __future__ import annotations

import pytest

from forseti.properties.cexpr import (
    derefs_or_subscripts,
    identifiers,
    references,
    unsafe_reason,
)


class TestIdentifiers:
    def test_extracts_a_bare_identifier(self) -> None:
        assert identifiers("result") == ["result"]

    def test_drops_decimal_literals(self) -> None:
        assert identifiers("result >= 0") == ["result"]

    def test_preserves_left_to_right_order(self) -> None:
        assert identifiers("a < b") == ["a", "b"]

    def test_empty_expression_has_no_identifiers(self) -> None:
        assert identifiers("") == []

    def test_hex_literal_is_not_misread_as_identifier(self) -> None:
        # `0x1F` is a single numeric literal; stripping it first stops the `x1F`
        # tail from being tokenized as an identifier.
        assert identifiers("0x1F") == []

    def test_suffixed_literal_is_not_misread_as_identifier(self) -> None:
        # The `u`/`L`/`UL` integer suffixes are part of the literal, never idents.
        assert identifiers("10u") == []
        assert identifiers("10UL") == []

    def test_identifier_beside_hex_literal(self) -> None:
        assert identifiers("count <= 0x10FFFF") == ["count"]

    def test_identifier_beside_suffixed_literal(self) -> None:
        assert identifiers("x < 10u") == ["x"]

    def test_macro_and_parameter_names(self) -> None:
        assert identifiers("INT64_MIN < x") == ["INT64_MIN", "x"]


class TestReferences:
    def test_matches_a_whole_word(self) -> None:
        assert references("a + result", "result") is True

    def test_does_not_match_a_longer_identifier(self) -> None:
        # `result2` is a distinct identifier; a whole-word match must not fire.
        assert references("result2 > 0", "result") is False

    def test_does_not_match_a_literal_suffix(self) -> None:
        # `\bu\b` cannot match the `u` inside `10u`: no word boundary between
        # `0` and `u`. An output named `u` is not referenced by `x < 10u`.
        assert references("x < 10u", "u") is False

    def test_matches_a_subscripted_name(self) -> None:
        assert references("a[0] + b", "a") is True

    def test_missing_identifier(self) -> None:
        assert references("a + b", "c") is False

    def test_empty_expression(self) -> None:
        assert references("", "x") is False


class TestUnsafeReason:
    def test_pure_boolean_expression_is_safe(self) -> None:
        assert unsafe_reason("result >= 0 && x < 10") is None

    def test_relational_operators_are_safe(self) -> None:
        # `==`, `<=`, `>=`, `!=` are comparisons, not assignments.
        assert unsafe_reason("a == b") is None
        assert unsafe_reason("a <= b && c >= d") is None

    def test_left_shift_is_safe(self) -> None:
        # A bare `<<`/`>>` is not a compound assignment and carries no `=`.
        assert unsafe_reason("a << 2 == b") is None

    def test_statement_separator_rejected(self) -> None:
        assert unsafe_reason("a; b") == "contains ';'"

    def test_backtick_rejected(self) -> None:
        assert unsafe_reason("`ls`") == "contains a backtick"

    @pytest.mark.parametrize("expr", ["i++", "i--", "++i", "--i"])
    def test_increment_decrement_rejected(self, expr: str) -> None:
        assert unsafe_reason(expr) == "contains an increment/decrement operator"

    @pytest.mark.parametrize("expr", ["x <<= 1", "x >>= 1"])
    def test_compound_shift_assignment_rejected(self, expr: str) -> None:
        # `<<=`/`>>=` must be caught before the bare-`=` check, whose `_RELATIONAL`
        # strip would otherwise hide the embedded `<=`/`>=`.
        assert unsafe_reason(expr) == "contains a compound assignment operator"

    def test_bare_assignment_rejected(self) -> None:
        assert unsafe_reason("x = 0") == "contains an assignment"

    def test_function_call_rejected(self) -> None:
        assert unsafe_reason("f(x) > 0") == "contains a function call"

    def test_unbalanced_parentheses_rejected(self) -> None:
        assert unsafe_reason("(a + b") == "unbalanced parentheses"


class TestDerefsOrSubscripts:
    def test_subscript(self) -> None:
        assert derefs_or_subscripts("cp[0] == 0", "cp") is True

    def test_leading_dereference(self) -> None:
        assert derefs_or_subscripts("*cp == 0", "cp") is True

    def test_parenthesized_dereference(self) -> None:
        # The parenthesized form is the propose-path evasion a bare `*name` misses.
        assert derefs_or_subscripts("*(cp + 0) == 0", "cp") is True
        assert derefs_or_subscripts("*(cp) == 0", "cp") is True

    def test_bare_use_is_not_a_pointer_use(self) -> None:
        assert derefs_or_subscripts("cp <= 0x10FFFF", "cp") is False

    def test_other_name_untouched(self) -> None:
        assert derefs_or_subscripts("*cp == 0", "count") is False

    @pytest.mark.parametrize(
        "expr",
        [
            "result * cp >= 0",  # scalar output as the right factor -- #106
            "result * (cp + 1) >= 0",  # parenthesized factor, not a deref
            "result*cp >= 0",  # no whitespace around the binary `*`
            "cp * result >= 0",  # left factor -- the form that never tripped it
            "2 * cp >= 0",  # a numeric left operand precedes the `*`
            "(a + b) * cp >= 0",  # a closing `)` precedes the `*`
            "arr[i] * cp >= 0",  # a closing `]` precedes the `*`
        ],
    )
    def test_multiplication_is_not_a_pointer_use(self, expr: str) -> None:
        # A binary `*` -- an operand (identifier, number, `)`, `]`) immediately
        # before it -- multiplies the scalar-bound output; only a *unary* `*`
        # dereferences it.
        assert derefs_or_subscripts(expr, "cp") is False

    def test_multiplication_beside_a_genuine_deref_still_flags(self) -> None:
        # The second `*` in `a * *cp` is unary (an operator, not an operand,
        # precedes it), so the deref is still caught despite the multiplication.
        assert derefs_or_subscripts("a * *cp >= 0", "cp") is True

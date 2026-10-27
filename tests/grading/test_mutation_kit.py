"""Tests for `forseti.grading` -- the W3.1 mutation operator kit.

The operator, ordering, cap and containment tests use a fake validity oracle and
need no ESBMC; the kernel tests (every emitted mutant parses, `rb_push` reaches the
`ring_buffer_bug.c` defect) run the real `esbmc --parse-tree-only` and skip
without it.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterable
from pathlib import Path

import pytest

from forseti.grading import (
    OPERATORS,
    Mutant,
    MutationError,
    esbmc_validator,
    generate_mutants,
    mutate_function,
)
from forseti.grading.body import locate_body, masked_view

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
needs_esbmc = pytest.mark.skipif(
    shutil.which("esbmc") is None, reason="esbmc binary not on PATH"
)


def _accept(_source: str) -> bool:
    return True


def _mutants(
    source: str,
    symbol: str = "f",
    *,
    operators: Iterable[str] = OPERATORS,
    cap: int | None = None,
    seed: int = 0,
) -> tuple[Mutant, ...]:
    return generate_mutants(
        source, symbol, validate=_accept, operators=operators, cap=cap, seed=seed
    ).mutants


def _only(source: str, operator: str, symbol: str = "f") -> list[Mutant]:
    return list(_mutants(source, symbol, operators=[operator]))


def _swaps(source: str, mutants: list[Mutant]) -> list[str]:
    return [m.source for m in mutants]


class TestRelationalFlip:
    @pytest.mark.parametrize(
        ("op", "flipped"),
        [
            ("<", "<="),
            ("<=", "<"),
            (">", ">="),
            (">=", ">"),
            ("==", "!="),
            ("!=", "=="),
        ],
    )
    def test_each_operator_flips(self, op: str, flipped: str) -> None:
        src = f"int f(int a, int b) {{ return a {op} b; }}\n"
        [m] = _only(src, "relational-flip")
        assert m.source == f"int f(int a, int b) {{ return a {flipped} b; }}\n"
        assert m.description == f"`{op}` -> `{flipped}` at line 1"

    def test_shifts_arrows_and_comments_are_not_sites(self) -> None:
        src = (
            "int f(int *p, int a) {\n"
            "    /* a < b */ int s = p->x << 2 >> 1; // a == b\n"
            '    char *m = "a >= b";\n'
            "    return s;\n"
            "}\n"
        )
        assert _only(src, "relational-flip") == []


class TestArithSwap:
    def test_binary_plus_and_minus_swap(self) -> None:
        src = "int f(int a, int b) { return a + b - 1; }\n"
        got = _swaps(src, _only(src, "arith-swap"))
        assert got == [
            "int f(int a, int b) { return a - b - 1; }\n",
            "int f(int a, int b) { return a + b + 1; }\n",
        ]

    def test_compound_assignment_swaps(self) -> None:
        src = "int f(int a) { a += 2; a -= 1; return a; }\n"
        got = _swaps(src, _only(src, "arith-swap"))
        assert got == [
            "int f(int a) { a -= 2; a -= 1; return a; }\n",
            "int f(int a) { a += 2; a += 1; return a; }\n",
        ]

    def test_unary_signs_and_increments_are_not_sites(self) -> None:
        src = "int f(int a) { a++; --a; return -a + +a * (-1); }\n"
        got = _swaps(src, _only(src, "arith-swap"))
        assert got == ["int f(int a) { a++; --a; return -a - +a * (-1); }\n"]


class TestConstOffByOne:
    def test_plus_and_minus_one_with_ordinals(self) -> None:
        src = "int f(void) { return 7; }\n"
        plus, minus = _only(src, "const-off-by-one")
        assert plus.source == "int f(void) { return (7 + 1); }\n"
        assert minus.source == "int f(void) { return (7 - 1); }\n"
        assert plus.description == "`7` -> `8` at line 1"
        assert minus.description == "`7` -> `6` at line 1"
        assert (plus.id.ordinal, minus.id.ordinal) == (0, 1)
        assert plus.id.line == minus.id.line == 1
        assert plus.id.column == minus.id.column == 22

    def test_hex_octal_and_suffixed_literals_keep_their_spelling(self) -> None:
        src = "unsigned f(void) { return 0x10u + 010; }\n"
        descriptions = [m.description for m in _only(src, "const-off-by-one")]
        assert descriptions == [
            "`0x10u` -> `17` at line 1",
            "`0x10u` -> `15` at line 1",
            "`010` -> `9` at line 1",
            "`010` -> `7` at line 1",
        ]
        assert "(0x10u + 1)" in _only(src, "const-off-by-one")[0].source

    def test_floats_and_exponents_are_not_sites(self) -> None:
        src = "double f(void) { return 1.5 + 2e-3 + .25; }\n"
        assert _only(src, "const-off-by-one") == []


class TestGuardDrop:
    def test_statement_guard_is_deleted(self) -> None:
        src = "int f(int a) {\n    if (a == 0) return 1;\n    return a;\n}\n"
        [m] = _only(src, "guard-drop")
        assert m.source == "int f(int a) {\n    \n    return a;\n}\n"
        assert m.description == "drop guard `if (a == 0) return 1;` at line 2"

    def test_braced_single_return_guard(self) -> None:
        src = "int f(int a) {\n    if (a) { return 1; }\n    return a;\n}\n"
        [m] = _only(src, "guard-drop")
        assert "if" not in m.source

    def test_void_return_guard(self) -> None:
        src = "void f(int a) { if (a) return; a = 1; }\n"
        [m] = _only(src, "guard-drop")
        assert m.source == "void f(int a) {  a = 1; }\n"

    def test_guard_under_a_loop_becomes_an_empty_statement(self) -> None:
        src = "int f(int n) { for (;;) if (n) return 1; return 0; }\n"
        [m] = _only(src, "guard-drop")
        assert m.source == "int f(int n) { for (;;) ; return 0; }\n"

    def test_non_guards_are_not_sites(self) -> None:
        src = (
            "int f(int a) {\n"
            "    if (a) { a = 1; return a; }\n"
            "    if (a > 2) return 3; else return 4;\n"
            "    if (a > 5) a = 0; else if (a > 6) return 5;\n"
            "    return a;\n"
            "}\n"
        )
        assert _only(src, "guard-drop") == []


class TestOutputHavoc:
    def test_returned_value_is_replaced_and_prototype_stays_in_the_body(self) -> None:
        src = "unsigned char f(int a) {\n    return a & 1;\n}\n"
        [m] = _only(src, "output-havoc")
        assert m.source == (
            "unsigned char f(int a) { unsigned char nondet_unsigned_char(void);\n"
            "    return nondet_unsigned_char();\n}\n"
        )
        assert m.description == (
            "havoc returned value with `nondet_unsigned_char()` at line 2"
        )

    def test_write_through_output_parameter(self) -> None:
        src = (
            "void f(const int *in, int *out, unsigned char buf[4]) {\n"
            "    *out = *in + 1;\n"
            "    buf[2] = 7;\n"
            "}\n"
        )
        a, b = _only(src, "output-havoc")
        assert "*out = nondet_int();" in a.source
        assert "int nondet_int(void);" in a.source
        assert "buf[2] = nondet_unsigned_char();" in b.source

    def test_const_pointee_void_pointer_and_member_writes_are_not_sites(self) -> None:
        src = (
            "typedef struct { int x; } s_t;\n"
            "void f(const int *in, void *raw, s_t *s, int **pp) {\n"
            "    *in = 1; s->x = 2; **pp = 3; (*s).x = 4;\n"
            "}\n"
        )
        assert _only(src, "output-havoc") == []

    def test_pointer_and_void_returns_are_not_sites(self) -> None:
        src = "int *f(int *p) { return p; }\nvoid g(void) { return; }\n"
        assert _only(src, "output-havoc", "f") == []
        assert _only(src, "output-havoc", "g") == []


class TestEdgeShapes:
    def test_long_guard_description_is_truncated(self) -> None:
        cond = " && ".join(f"a{i} == {i}" for i in range(12))
        src = f"int f(int a0) {{ if ({cond}) return 1; return 0; }}\n"
        [m] = _only(src, "guard-drop")
        assert m.description.startswith("drop guard `if (a0 == 0 && ")
        assert "...` at line 1" in m.description

    def test_comma_returns_and_existing_nondet_are_not_havoc_sites(self) -> None:
        src = (
            "int f(int a, int b) { return a, b; }\n"
            "int g(void) { return nondet_int(); }\n"
        )
        assert _only(src, "output-havoc", "f") == []
        assert _only(src, "output-havoc", "g") == []

    def test_unusual_parameter_shapes_are_skipped(self) -> None:
        src = (
            "void f(int (*cb)(int), int **pp, int *rows[4], int *o) {\n"
            "    *o = 1; *o += 2; o[0] = 3;\n"
            "}\n"
        )
        got = [m.description for m in _only(src, "output-havoc")]
        assert got == [
            "havoc write through `o` with `nondet_int()` at line 2",
            "havoc write through `o` with `nondet_int()` at line 2",
        ]

    def test_unbalanced_body_is_an_error(self) -> None:
        with pytest.raises(MutationError, match="unbalanced"):
            _mutants("int f(void) { if (1) {\n return 0; }\n")

    def test_qualified_return_type_and_attributes(self) -> None:
        src = "static inline const unsigned f(unsigned a) { return a; }\n"
        [m] = _only(src, "output-havoc")
        assert "unsigned nondet_unsigned(void);" in m.source


class TestContainment:
    SRC = (
        "#define CAP 4\n"
        "int before(int a) { return a < 1 ? a + 1 : 2; }\n"
        "static int f(int a, int *out) {\n"
        "    if (a > CAP) return 0;\n"
        "    *out = a + 1;\n"
        "    return a < 9;\n"
        "}\n"
        "int after(int a) { if (a == 3) return 1; return a - 7; }\n"
    )

    def test_every_operator_leaves_the_rest_of_the_file_byte_identical(self) -> None:
        body = locate_body(self.SRC, "f")
        head = self.SRC[: body.open_brace + 1]
        tail = self.SRC[body.close_brace :]
        mutants = _mutants(self.SRC)
        assert {m.operator for m in mutants} == set(OPERATORS)
        for m in mutants:
            assert m.source.startswith(head), m.id
            assert m.source.endswith(tail), m.id
            assert m.source != self.SRC
            for edit in m.edits:
                assert body.open_brace < edit.start <= edit.end <= body.close_brace

    def test_unknown_symbol_and_prototype_only_are_errors(self) -> None:
        with pytest.raises(MutationError):
            _mutants(self.SRC, "missing")
        with pytest.raises(MutationError):
            _mutants("int g(int);\n", "g")

    def test_unknown_operator_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="nope"):
            _mutants(self.SRC, "f", operators=["nope"])


GOLDEN_SRC = """\
int clamp(int x, int *flag) {
    if (x < 0) return 0;
    *flag = x > 9;
    return x + 1;
}
"""

GOLDEN = [
    ("guard-drop@2:5#0", "drop guard `if (x < 0) return 0;` at line 2"),
    ("relational-flip@2:11#0", "`<` -> `<=` at line 2"),
    ("const-off-by-one@2:13#0", "`0` -> `1` at line 2"),
    ("const-off-by-one@2:13#1", "`0` -> `-1` at line 2"),
    ("const-off-by-one@2:23#0", "`0` -> `1` at line 2"),
    ("const-off-by-one@2:23#1", "`0` -> `-1` at line 2"),
    ("output-havoc@2:23#0", "havoc returned value with `nondet_int()` at line 2"),
    ("output-havoc@3:13#0", "havoc write through `flag` with `nondet_int()` at line 3"),
    ("relational-flip@3:15#0", "`>` -> `>=` at line 3"),
    ("const-off-by-one@3:17#0", "`9` -> `10` at line 3"),
    ("const-off-by-one@3:17#1", "`9` -> `8` at line 3"),
    ("output-havoc@4:12#0", "havoc returned value with `nondet_int()` at line 4"),
    ("arith-swap@4:14#0", "`+` -> `-` at line 4"),
    ("const-off-by-one@4:16#0", "`1` -> `2` at line 4"),
    ("const-off-by-one@4:16#1", "`1` -> `0` at line 4"),
]


class TestDeterminism:
    def test_golden_ids_descriptions_and_order(self) -> None:
        got = [(str(m.id), m.description) for m in _mutants(GOLDEN_SRC, "clamp")]
        assert got == GOLDEN

    def test_repeated_runs_and_operator_order_do_not_matter(self) -> None:
        first = _mutants(GOLDEN_SRC, "clamp")
        assert _mutants(GOLDEN_SRC, "clamp") == first
        shuffled = _mutants(GOLDEN_SRC, "clamp", operators=reversed(OPERATORS))
        assert shuffled == first

    def test_operator_subset(self) -> None:
        got = _mutants(GOLDEN_SRC, "clamp", operators=["guard-drop"])
        assert [str(m.id) for m in got] == ["guard-drop@2:5#0"]


class TestValidityAndCap:
    def test_rejected_mutants_are_dropped_and_counted(self) -> None:
        total = len(_mutants(GOLDEN_SRC, "clamp"))
        result = generate_mutants(
            GOLDEN_SRC, "clamp", validate=lambda s: "nondet_int" not in s
        )
        assert result.dropped == 3
        assert len(result.mutants) == total - 3
        assert all("nondet_int" not in m.source for m in result.mutants)

    def test_cap_keeps_canonical_order_and_is_seed_deterministic(self) -> None:
        full = _mutants(GOLDEN_SRC, "clamp")
        capped = _mutants(GOLDEN_SRC, "clamp", cap=5, seed=3)
        assert len(capped) == 5
        assert capped == _mutants(GOLDEN_SRC, "clamp", cap=5, seed=3)
        positions = [full.index(m) for m in capped]
        assert positions == sorted(positions)

    def test_different_seeds_can_pick_different_mutants(self) -> None:
        picks = {
            tuple(str(m.id) for m in _mutants(GOLDEN_SRC, "clamp", cap=4, seed=s))
            for s in range(8)
        }
        assert len(picks) > 1

    def test_cap_stops_validating_once_full(self) -> None:
        calls: list[str] = []

        def validate(source: str) -> bool:
            calls.append(source)
            return True

        result = generate_mutants(GOLDEN_SRC, "clamp", validate=validate, cap=3)
        assert len(result.mutants) == 3
        assert len(calls) == 3

    def test_cap_larger_than_candidates_returns_everything(self) -> None:
        assert _mutants(GOLDEN_SRC, "clamp", cap=999) == _mutants(GOLDEN_SRC, "clamp")

    def test_cap_refills_past_invalid_candidates(self) -> None:
        result = generate_mutants(
            GOLDEN_SRC,
            "clamp",
            validate=lambda s: "nondet_int" not in s,
            cap=len(GOLDEN) - 3,
            seed=1,
        )
        assert len(result.mutants) == len(GOLDEN) - 3

    def test_missing_esbmc_is_an_error_not_a_silent_drop(self) -> None:
        with pytest.raises(MutationError, match="not found"):
            esbmc_validator("f", esbmc_bin="forseti-no-such-esbmc")


class TestEsbmcPlumbing:
    """The validator and `mutate_function` against a stub that cannot parse."""

    @staticmethod
    def _stub(tmp_path: Path, exit_code: int) -> str:
        stub = tmp_path / "fake-esbmc"
        stub.write_text(f"#!/bin/sh\nexit {exit_code}\n")
        stub.chmod(0o755)
        return str(stub)

    def test_parse_failure_and_empty_listing_both_reject(self, tmp_path: Path) -> None:
        for code in (1, 0):
            validate = esbmc_validator("f", esbmc_bin=self._stub(tmp_path, code))
            assert not validate("int f(void) { return 0; }\n")

    def test_unparseable_original_aborts_before_any_scoring(
        self, tmp_path: Path
    ) -> None:
        src = tmp_path / "k.c"
        src.write_text("int f(void) { return 0; }\n")
        with pytest.raises(MutationError, match="does not parse"):
            mutate_function(src, "f", esbmc_bin=self._stub(tmp_path, 1))


def _body_tokens(path: Path | str, symbol: str, text: str | None = None) -> str:
    source = text if text is not None else Path(path).read_text()
    body = locate_body(source, symbol)
    return " ".join(masked_view(source)[body.open_brace : body.close_brace + 1].split())


# (file, symbol, operators that must each yield at least one mutant). Hand-written
# from the function bodies, so "applicable" is not derived from the kit itself.
_KERNELS = [
    ("ring_buffer.c", "rb_push", OPERATORS),
    ("ring_buffer.c", "rb_pop", OPERATORS),
    ("merge_sort.c", "msort", OPERATORS[:4]),
    (
        "utf8_decode.c",
        "utf8_decode",
        ("relational-flip", "const-off-by-one", "guard-drop", "output-havoc"),
    ),
    (
        "murmurhash.c",
        "murmur3_32",
        ("relational-flip", "arith-swap", "const-off-by-one", "output-havoc"),
    ),
    (
        "abs_fixed.c",
        "my_abs",
        ("relational-flip", "const-off-by-one", "guard-drop", "output-havoc"),
    ),
    ("frame_checksum.c", "payload_checksum", OPERATORS),
    (
        "sha1.c",
        "sha1_final",
        ("relational-flip", "arith-swap", "const-off-by-one", "output-havoc"),
    ),
]


@needs_esbmc
class TestExamplesKernels:
    @pytest.mark.parametrize(("name", "symbol", "applicable"), _KERNELS)
    def test_every_applicable_operator_yields_a_mutant_that_compiles(
        self, name: str, symbol: str, applicable: tuple[str, ...]
    ) -> None:
        path = EXAMPLES / name
        result = mutate_function(path, symbol)
        assert {m.operator for m in result.mutants} >= set(applicable)
        validate = esbmc_validator(symbol, extra_flags=(f"-I{EXAMPLES}",))
        for m in result.mutants:
            assert validate(m.source), m.id
        original = path.read_text()
        body = locate_body(original, symbol)
        for m in result.mutants:
            assert m.source.startswith(original[: body.open_brace + 1])
            assert m.source.endswith(original[body.close_brace :])

    def test_rb_push_mutants_reach_the_staged_ring_buffer_defect(self) -> None:
        result = mutate_function(EXAMPLES / "ring_buffer.c", "rb_push")
        bug = _body_tokens(EXAMPLES / "ring_buffer_bug.c", "rb_push")
        reached = [
            m for m in result.mutants if _body_tokens("", "rb_push", m.source) == bug
        ]
        assert [m.operator for m in reached] == ["guard-drop"]
        assert reached[0].description == (
            "drop guard `if (r->count == CAP) return 0;` at line 31"
        )

    def test_unparseable_original_is_an_error(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.c"
        bad.write_text("int f(int a) { return a +; }\n")
        with pytest.raises(MutationError, match="does not parse"):
            mutate_function(bad, "f")

    def test_validator_rejects_garbage_and_a_missing_target(self) -> None:
        validate = esbmc_validator("f")
        assert validate("int f(void) { return 0; }\n")
        assert not validate("int f(void) { return ; ; +; }\n")
        assert not validate("int g(void) { return 0; }\n")

    def test_dropped_mutants_are_counted(self) -> None:
        result = mutate_function(EXAMPLES / "murmurhash.c", "murmur3_32")
        assert result.dropped > 0

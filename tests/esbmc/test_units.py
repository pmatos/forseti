"""Tests for `forseti.esbmc.units` — listing function units from ESBMC's AST.

`parse_units` is tested purely against a captured-shape AST fixture (no ESBMC);
`list_units` has ESBMC-gated end-to-end cases.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from forseti.esbmc.units import (
    ListUnitsError,
    Param,
    Unit,
    _line_breakpoints,
    annotate_array_extents,
    find_definition_brace,
    list_units,
    mask_comments,
    parse_address_escapes,
    parse_asm_statements,
    parse_definitions,
    parse_external_callers,
    parse_implicit_invocations,
    parse_symbol_aliases,
    parse_units,
)

# A clang textual AST in ESBMC's `--parse-tree-only` shape, exercising: an
# intrinsic in another file (excluded), a definition in a *same-basename* file in
# another directory (`/other/foo.c` — must be excluded by full-path match, not
# misread as `/tmp/foo.c`), a typedef, a scalar definition, a multi-param pointer
# definition, a prototype (no body → excluded), and a typedef'd function-pointer
# parameter printed as `'written':'canonical'` (must resolve to a pointer).
_TARGET = "/tmp/foo.c"
_AST = """\
TranslationUnitDecl 0x1000 <<invalid sloc>> <invalid sloc>
|-FunctionDecl 0x1001 <esbmc_intrinsics.h:1:1> col:6 assume 'void (_Bool)'
| `-ParmVarDecl 0x1002 <col:14, col:19> col:19 '_Bool'
|-FunctionDecl 0x1050 </other/foo.c:9:1, col:22> col:6 collide 'void (int *)'
| |-ParmVarDecl 0x1051 <col:12, col:18> col:18 used q 'int *'
| `-CompoundStmt 0x1052 <col:20, col:22>
|-TypedefDecl 0x1003 </tmp/foo.c:2:1, col:26> col:16 referenced cb_t 'void (*)(void)'
|-FunctionDecl 0x1004 <line:3:1, col:29> col:5 scal 'int (int)'
| |-ParmVarDecl 0x1005 <col:10, col:14> col:14 used x 'int'
| `-CompoundStmt 0x1006 <col:22, col:29>
|   `-ReturnStmt 0x1007 <col:23, col:30>
|-FunctionDecl 0x1008 <line:4:1, col:77> col:10 hash 'u (p, n)'
| |-ParmVarDecl 0x1009 <col:15, col:30> col:30 used key 'const uint8_t *'
| |-ParmVarDecl 0x100a <col:37, col:50> col:50 used n 'unsigned long'
| `-CompoundStmt 0x100b <col:55, col:77>
|-FunctionDecl 0x100c <line:5:1, col:20> col:5 proto 'int (int)'
| `-ParmVarDecl 0x100d <col:15, col:18> col:18 'int'
`-FunctionDecl 0x1011 <line:6:1, col:27> col:6 reg 'void (cb_t)'
  |-ParmVarDecl 0x1012 <col:10, col:15> col:15 used cb 'cb_t':'void (*)(void)'
  `-CompoundStmt 0x1013 <col:20, col:27>
"""


def test_parse_units_definitions_only_in_target_file() -> None:
    units = {u.name: u for u in parse_units(_AST, _TARGET)}
    # assume (intrinsics.h) and collide (/other/foo.c) are other files; proto has
    # no CompoundStmt → not a definition.
    assert set(units) == {"scal", "hash", "reg"}


def test_parse_units_excludes_same_basename_other_dir() -> None:
    # `collide` lives in /other/foo.c — same basename as /tmp/foo.c but a
    # different file (a `#include`d same-named file); a full-path match excludes
    # it, where a basename-only match would have leaked it in.
    names = {u.name for u in parse_units(_AST, _TARGET)}
    assert "collide" not in names


def test_parse_units_pointer_classification() -> None:
    units = {u.name: u for u in parse_units(_AST, _TARGET)}
    assert units["scal"].takes_pointer is False
    assert units["hash"].takes_pointer is True  # const uint8_t *
    # the whole point of #131: a typedef'd function-pointer param resolves to a
    # pointer even though the written type `cb_t` shows no `*`.
    assert units["reg"].takes_pointer is True


def test_parse_units_param_types_are_canonical() -> None:
    reg = next(u for u in parse_units(_AST, _TARGET) if u.name == "reg")
    assert reg.params == (Param("cb", "void (*)(void)"),)
    hash_ = next(u for u in parse_units(_AST, _TARGET) if u.name == "hash")
    assert [p.type for p in hash_.params] == ["const uint8_t *", "unsigned long"]


def test_parse_units_captures_definition_line() -> None:
    # `def_line` anchors `annotate_array_extents`'s declarator search (issue #145);
    # each of these is read off the FunctionDecl's own AST location in `_AST`.
    units = {u.name: u for u in parse_units(_AST, _TARGET)}
    assert units["scal"].def_line == 3  # <line:3:1, col:29>
    assert units["hash"].def_line == 4  # <line:4:1, col:77>
    assert units["reg"].def_line == 6  # <line:6:1, col:27>


# PR #156 follow-up: when a `FunctionDecl`'s return type is a macro defined
# earlier in the file, clang's range *starts* at the macro's spelling location —
# here line 1 — not the function's own line. This is a real `esbmc
# --parse-tree-only` dump (captured against `#define API void` / `API f(int *p)
# { *p = 1; }` on line 5), trimmed to the one FunctionDecl.
_MACRO_RETURN_TYPE_AST = """\
TranslationUnitDecl 0x1000 <<invalid sloc>> <invalid sloc>
`-FunctionDecl 0x2000 </tmp/macro.c:1:13, line:5:25> col:5 f 'void (int *)'
  |-ParmVarDecl 0x2001 <col:7, col:12> col:12 used p 'int *'
  `-CompoundStmt 0x2002 <col:15, col:25>
"""


def test_parse_units_macro_return_type_uses_identifier_line_not_range_start() -> None:
    # The range start (`1:13`) is the macro definition's line; `def_line` must be
    # the function identifier's own line (5), which the AST separately states as
    # its abbreviated point location (`col:5`, inheriting from the range end
    # `line:5:25`). Anchoring on the range start instead would land `def_line` on
    # the macro's line, letting an inactive alternative definition sitting between
    # the macro and the real one donate its array extent (issue #145 follow-up).
    unit = next(iter(parse_units(_MACRO_RETURN_TYPE_AST, "/tmp/macro.c")))
    assert unit.def_line == 5


# PR #156 follow-up: clang echoes the input path verbatim, spaces included, when
# the source lives in a directory whose name has one. This is a real `esbmc
# --parse-tree-only` dump (captured against `/tmp/path space/test.c`), trimmed to
# the one FunctionDecl.
_WHITESPACE_PATH_AST = """\
TranslationUnitDecl 0x1000 <<invalid sloc>> <invalid sloc>
`-FunctionDecl 0x117363d0 </tmp/path space/test.c:1:1, col:26> col:6 f 'void (int *)'
  |-ParmVarDecl 0x11736308 <col:8, col:13> col:13 used p 'int *'
  `-CompoundStmt 0x11736510 <col:16, col:26>
"""


def test_parse_units_path_with_whitespace() -> None:
    # A token pattern that requires a whitespace-free path leaves this location
    # chain unmatched entirely, so `def_line` would silently retain whatever line
    # preceded it instead of this node's own — and, with an earlier inactive
    # same-named definition, that stale anchor can select the wrong one's array
    # extent (PR #156 follow-up to issue #145).
    unit = next(iter(parse_units(_WHITESPACE_PATH_AST, "/tmp/path space/test.c")))
    assert unit.def_line == 1


def test_parse_units_empty_on_declarations_only() -> None:
    header = (
        "TranslationUnitDecl 0x1 <<invalid sloc>>\n"
        "|-FunctionDecl 0x2 </tmp/bar.h:1:1, col:20> col:6 decl 'void (int *)'"
    )
    assert parse_units(header, "/tmp/bar.h") == []  # a prototype, no CompoundStmt


@pytest.mark.parametrize(
    "type_str, is_ptr",
    [
        ("int", False),
        ("const uint8_t *", True),
        ("void (*)(void)", True),  # function pointer
        ("int (*)[10]", True),  # pointer to array
        ("unsigned long", False),
        ("char *", True),
    ],
)
def test_param_is_pointer(type_str: str, is_ptr: bool) -> None:
    assert Param("p", type_str).is_pointer is is_ptr


def _annotated(source: str, param: Param, fn: str = "f") -> Param:
    """`param` as `annotate_array_extents` annotates it from `source`."""
    return annotate_array_extents([Unit(fn, (param,))], source)[0].params[0]


def _shape(source: str, param: Param, fn: str = "f") -> tuple[int | None, bool]:
    """The array shape `annotate_array_extents` harvests for `param` in `source`."""
    out = _annotated(source, param, fn)
    return out.array_extent, out.array_extent_unresolved


def _extent(source: str, param: Param, fn: str = "f") -> int | None:
    """The `array_extent` `annotate_array_extents` harvests for `param` in `source`."""
    return _shape(source, param, fn)[0]


@pytest.mark.parametrize(
    "decl, expected",
    [
        ("void f(uint8_t p[20]) {}", 20),
        ("void f(uint8_t p [64]) {}", 64),  # space before bracket
        ("void f(uint8_t p[ 32 ]) {}", 32),  # spaces inside bracket
        # C99 `static` / cv-qualifiers may precede the extent in a parameter
        # declarator, in either order — the literal is still readable.
        ("void f(uint8_t p[static 20]) {}", 20),
        ("void f(uint8_t p[static const 24]) {}", 24),
        ("void f(uint8_t p[const static 28]) {}", 28),
        ("void f(uint8_t p[restrict 12]) {}", 12),
        ("void f(uint8_t p[__restrict 16]) {}", 16),
        # C11 adds `_Atomic` to the qualifiers valid in that bracket.
        ("void f(uint8_t p[_Atomic 20]) {}", 20),
        ("void f(uint8_t p[static _Atomic 40]) {}", 40),
    ],
)
def test_annotate_array_extents_recovers_literal(decl: str, expected: int) -> None:
    assert _shape(decl, Param("p", "uint8_t *")) == (expected, False)


@pytest.mark.parametrize(
    "decl",
    [
        "void f(uint8_t *p) {}",  # plain pointer: no declarator bracket at all
        "void f(uint8_t p[]) {}",  # unsized: exactly as informative as `uint8_t *p`
        "void f(uint8_t p[ ]) {}",  # ... whitespace-only bracket is still unsized
        # A qualifier-only bracket qualifies the adjusted pointer (`uint8_t *const p`)
        # and states no extent — as unsized as `p[]`, not an unreadable extent.
        "void f(uint8_t p[const]) {}",
        "void f(uint8_t p[restrict]) {}",
        "void f(uint8_t p[__restrict]) {}",
        "void f(uint8_t p[ volatile ]) {}",
        "void f(uint8_t p[_Atomic]) {}",
        "void f(uint8_t p[const volatile]) {}",
    ],
)
def test_annotate_array_extents_no_extent_stated(decl: str) -> None:
    assert _shape(decl, Param("p", "uint8_t *")) == (None, False)


@pytest.mark.parametrize(
    "decl",
    [
        # A macro extent needs the preprocessor — unreadable, but the parameter is
        # still *written* as a fixed array, so it must not read as a plain pointer.
        "void f(uint8_t p[DLEN]) {}",
        "void f(uint8_t p[static DLEN]) {}",
        "void f(uint8_t p[N + 1]) {}",
        "void f(uint8_t p[sizeof(struct s)]) {}",
        "void f(uint8_t p[20u]) {}",  # a suffixed literal is not a bare decimal
        "void f(uint8_t p[0x20]) {}",
        "void f(uint8_t p[*]) {}",  # a `[*]` prototype declarator
        # C's grammar requires a size expression after `static`, so a bracket that
        # has `static` but no readable extent is never read as merely unsized.
        "void f(uint8_t p[static]) {}",
        "void f(uint8_t p[static const]) {}",
        "void f(int p[2][3]) {}",  # multi-dim (pointer-to-array) is not an L0 shape
        "void f(int p[2] [3]) {}",  # ... nor when the brackets are spaced apart
    ],
)
def test_annotate_array_extents_unreadable_extent_is_unresolved(decl: str) -> None:
    assert _shape(decl, Param("p", "uint8_t *")) == (None, True)


@pytest.mark.parametrize(
    "decl, expected",
    [
        ("void f(uint8_t p[static 20]) {}", True),
        ("void f(uint8_t p[static DLEN]) {}", True),  # meaningful even unreadable
        ("void f(uint8_t p[const static DLEN]) {}", True),
        ("void f(uint8_t p[static _Atomic 40]) {}", True),
        ("void f(uint8_t p[20]) {}", False),  # a conventional extent, not an obligation
        ("void f(uint8_t p[DLEN]) {}", False),
        ("void f(uint8_t *p) {}", False),
    ],
)
def test_annotate_array_static_min(decl: str, expected: bool) -> None:
    # C99 `[static N]` binds the caller to supply at least N elements, so it is
    # tracked separately from whether N itself could be read.
    assert _annotated(decl, Param("p", "uint8_t *")).array_static_min is expected


def test_annotate_array_extents_prefers_definition_over_prototype() -> None:
    # A prototype (`);`) and a call site must not be mistaken for the definition
    # whose `)` is followed by `{`.
    source = "void f(uint8_t p[20]);\nvoid g(void){ f(0); }\nvoid f(uint8_t p[20]){}\n"
    assert _extent(source, Param("p", "uint8_t *")) == 20


def test_annotate_array_extents_ignores_bracket_in_comment() -> None:
    # Neither a bogus extent nor a bogus "written as an array" signal: the
    # commented-out bracket is gone before the declarator is read.
    source = "void f(uint8_t *p /* was p[99] */) {}"
    assert _shape(source, Param("p", "uint8_t *")) == (None, False)


def test_annotate_array_extents_only_named_pointer_params() -> None:
    # An unnamed pointer param and a scalar param both stay None; only the named
    # pointer written `q[8]` is sized.
    source = "void f(int n, uint8_t q[8], uint8_t *) {}"
    unit = Unit(
        "f",
        (Param("n", "int"), Param("q", "uint8_t *"), Param("", "uint8_t *")),
    )
    out = annotate_array_extents([unit], source)[0].params
    assert [p.array_extent for p in out] == [None, 8, None]


def test_annotate_array_extents_unknown_function_unchanged() -> None:
    unit = Unit("missing", (Param("p", "uint8_t *"),))
    assert annotate_array_extents([unit], "void other(int x){}")[0] == unit


# Issue #145: textual order alone cannot tell an inactive `#if 0`/`#ifdef` body
# apart from the definition clang actually compiled — the first one is not
# necessarily the right one, and (for a literal extent) the wrong one is the more
# dangerous direction: an over-sized object can mask a real out-of-bounds.
_IF0_LITERAL_SOURCE = (
    "#if 0\nvoid f(int p[20]) { }\n#endif\nvoid f(int *p) { *p = 1; }\n"
)
_IF0_MACRO_SOURCE = "#if 0\nvoid f(int p[N]) { }\n#endif\nvoid f(int *p) { *p = 1; }\n"


def test_annotate_array_extents_anchors_past_inactive_if0_body() -> None:
    # `def_line=4` is what `parse_units` would report for the active `f` (clang
    # never sees the `#if 0` body at all) — anchoring to it must skip the inactive
    # literal-extent body, not read `p` as a fixed array of 20.
    unit = Unit("f", (Param("p", "int *"),), def_line=4)
    out = annotate_array_extents([unit], _IF0_LITERAL_SOURCE)[0].params[0]
    assert (out.array_extent, out.array_extent_unresolved) == (None, False)


def test_annotate_array_extents_without_def_line_uses_first_textual_match() -> None:
    # Documents the pre-#145 hazard the anchor fixes: absent a `def_line` hint,
    # the first definition-shaped occurrence wins even when it is the inactive
    # `#if 0` body — misreading a plain `int *p` as a fixed array of 20.
    unit = Unit("f", (Param("p", "int *"),))
    out = annotate_array_extents([unit], _IF0_LITERAL_SOURCE)[0].params[0]
    assert (out.array_extent, out.array_extent_unresolved) == (20, False)


def test_annotate_array_extents_anchors_past_inactive_macro_extent() -> None:
    # Same anchor, with the inactive body's extent stated as a macro instead of a
    # literal — the #137 unresolved-extent signal must not leak from it either.
    unit = Unit("f", (Param("p", "int *"),), def_line=4)
    out = annotate_array_extents([unit], _IF0_MACRO_SOURCE)[0].params[0]
    assert (out.array_extent, out.array_extent_unresolved) == (None, False)


def test_annotate_array_extents_preserves_def_line() -> None:
    # The returned Unit keeps its def_line, so a second annotation pass (or an
    # equality check against a hand-built Unit) is not silently reset to None.
    unit = Unit("f", (Param("p", "int *"),), def_line=4)
    out = annotate_array_extents([unit], _IF0_LITERAL_SOURCE)[0]
    assert out.def_line == 4


# Issue #145 follow-up: a `#line` directive resets clang's *presumed* line counter
# for everything after it, so an active definition's presumed line (`def_line`,
# what clang reports) can land well before its physical line — here 1, not the
# physical 5. Comparing `def_line` against a raw physical candidate line would
# silently re-anchor to the inactive `#if 0` body, sitting at physical line 2.
# Parametrized over both directive spellings clang honors: ISO C's `#line N` and
# GNU's linemarker `# N` (no `line` keyword).
@pytest.mark.parametrize("directive", ["#line 1", "# 1"])
def test_annotate_array_extents_anchors_across_a_line_directive(directive: str) -> None:
    source = (
        f"#if 0\nvoid f(int p[20]) {{ }}\n#endif\n{directive}\n"
        "void f(int *p) { *p = 1; }\n"
    )
    unit = Unit("f", (Param("p", "int *"),), def_line=1)
    out = annotate_array_extents([unit], source)[0].params[0]
    assert (out.array_extent, out.array_extent_unresolved) == (None, False)


# PR #156 follow-up: a naive textual scan would see two legal `#line 11`
# directives — one ahead of an inactive `#if 0` body, one ahead of the active
# definition — and translate both candidates to presumed line 11, tying with
# `def_line` (11, what clang reports for the active definition) equidistant from
# both. `_line_breakpoints` avoids the tie outright by excluding a `#line` inside
# a literal `#if 0`: only the second directive ever becomes a breakpoint, so the
# inactive candidate's presumed line falls back to its own (much more distant)
# physical line instead of colliding with the active one's.
def test_annotate_array_extents_ignores_a_line_directive_inside_a_dead_if0_body() -> (
    None
):
    source = (
        "#if 0\n#line 11\nvoid f(int p[20]) { }\n#endif\n"
        "#line 11\nvoid f(int *p) { *p = 1; }\n"
    )
    unit = Unit("f", (Param("p", "int *"),), def_line=11)
    out = annotate_array_extents([unit], source)[0].params[0]
    assert (out.array_extent, out.array_extent_unresolved) == (None, False)


def test_annotate_array_extents_ignores_a_dead_if0_line_directive_mirrored() -> None:
    # The mirror of the case above — the active definition physically *first*,
    # the inactive `#if 0` body (with its own colliding `#line 11`) second. A
    # naive tiebreak that always prefers the physically later occurrence would
    # get this arrangement backwards; the dead-region exclusion above avoids the
    # tie in both directions, so no such preference is needed at all.
    source = (
        "#line 11\nvoid f(int *p) { *p = 1; }\n"
        "#if 0\n#line 11\nvoid f(int p[20]) { }\n#endif\n"
    )
    unit = Unit("f", (Param("p", "int *"),), def_line=11)
    out = annotate_array_extents([unit], source)[0].params[0]
    assert (out.array_extent, out.array_extent_unresolved) == (None, False)


def test_annotate_array_extents_ignores_a_line_directive_inside_a_dead_elif0_body() -> (
    None
):
    # PR #156 follow-up: the inactive array-shaped definition sits behind an
    # `#elif 0`, not the `#if 0` itself. Treating every `#elif` as an
    # unconditional `#else` would reactivate this still-dead branch and let its
    # `#line 11` collide with the one guarding the active definition above.
    source = (
        "#line 11\nvoid f(int *p) { *p = 1; }\n"
        "#if 0\n#elif 0\n#line 11\nvoid f(int p[20]) { }\n#endif\n"
    )
    unit = Unit("f", (Param("p", "int *"),), def_line=11)
    out = annotate_array_extents([unit], source)[0].params[0]
    assert (out.array_extent, out.array_extent_unresolved) == (None, False)


def test_annotate_array_extents_ignores_a_line_directive_inside_a_dead_if1_body() -> (
    None
):
    # PR #156 follow-up: the inactive array-shaped definition sits behind an
    # `#else` paired with a literal `#if 1` — cpp always takes the `#if 1` arm,
    # so the `#else`'s own `#line 11` never reaches the compiler either. Before
    # this fix, `#if 1` was read as opaque and its `#else` was always assumed
    # live, so this `#line 11` collided with the active definition's and the
    # physical-line tiebreak picked the (physically later) inactive candidate.
    source = (
        "#if 1\n#line 11\nvoid f(int *p) { return; }\n"
        "#else\n#line 11\nvoid f(int p[20]) { return; }\n#endif\n"
    )
    unit = Unit("f", (Param("p", "int *"),), def_line=11)
    out = annotate_array_extents([unit], source)[0].params[0]
    assert (out.array_extent, out.array_extent_unresolved) == (None, False)


def test_line_breakpoints_excludes_a_directive_inside_a_dead_if0_body() -> None:
    source = "#if 0\n#line 5\nx\n#endif\n#line 9\ny\n"
    assert _line_breakpoints(source) == [(5, 9)]


def test_line_breakpoints_else_reactivates_a_dead_if0_branch() -> None:
    # `#else` after a literal `#if 0` is exactly the branch cpp *does* compile —
    # a `#line` inside it is reachable and must count, even though it sits inside
    # the same `#if 0`/`#endif` pair whose primary branch is dead.
    source = "#if 0\n#line 5\nx\n#else\n#line 9\ny\n#endif\n"
    assert _line_breakpoints(source) == [(5, 9)]


def test_line_breakpoints_ignores_an_unbalanced_endif() -> None:
    # A stray `#endif` with no matching `#if` must not raise — this scan reads
    # arbitrary real-world source, not a validated preprocessor input.
    source = "#endif\n#line 5\nx\n"
    assert _line_breakpoints(source) == [(2, 5)]


def test_line_breakpoints_does_not_treat_a_prefix_zero_as_known_false() -> None:
    # PR #156 follow-up: `#if 0 || FEATURE` merely *starts* with `0` — its full
    # condition is not the literal zero, so cpp might genuinely take this branch
    # and its `#line` must still count, not be silently dropped.
    source = "#if 0 || FEATURE\n#line 5\nx\n#endif\n#line 9\ny\n"
    assert _line_breakpoints(source) == [(2, 5), (5, 9)]


def test_line_breakpoints_elif_zero_stays_dead() -> None:
    # PR #156 follow-up: treating every `#elif` as an unconditional `#else`
    # would reactivate this still-dead `#elif 0` branch (its own condition is
    # false too) and let its `#line` collide with a real one elsewhere. Only the
    # unconditional `#else` that follows is guaranteed live.
    source = "#if 0\n#line 5\nx\n#elif 0\n#line 9\ny\n#else\n#line 11\nz\n#endif\n"
    assert _line_breakpoints(source) == [(8, 11)]


def test_line_breakpoints_excludes_a_directive_inside_a_dead_if1_else_body() -> None:
    # PR #156 follow-up: `#if 1` is a literal-true condition, so cpp always
    # takes it and never reaches the paired `#else` — a `#line` there must not
    # count, even though an `#else` is unconditionally live after an *opaque*
    # `#if` (contrast `test_line_breakpoints_else_reactivates_a_dead_if0_branch`).
    source = "#if 1\n#line 5\nx\n#else\n#line 9\ny\n#endif\n"
    assert _line_breakpoints(source) == [(2, 5)]


def test_line_breakpoints_elif_one_is_live_when_no_earlier_arm_won() -> None:
    # PR #156 follow-up: a literal `#elif 1` is live exactly when nothing
    # earlier in the chain was already known-taken — the opaque `#if FOO`
    # hasn't proven anything either way, so this arm is (optimistically)
    # assumed live, the same bias as an opaque `#if`/`#elif` on its own.
    source = "#if FOO\n#elif 1\n#line 5\nx\n#endif\n"
    assert _line_breakpoints(source) == [(3, 5)]


def test_line_breakpoints_elif_one_is_dead_after_an_earlier_if_one() -> None:
    # Mirror: once a literal `#if 1` has already won the chain, a later
    # `#elif 1` can never be reached, however true its own condition reads.
    source = "#if 1\n#elif 1\n#line 5\nx\n#endif\n"
    assert _line_breakpoints(source) == []


def test_line_breakpoints_ignores_an_unbalanced_elif_zero() -> None:
    # A stray `#elif 0` with no matching `#if` must not raise, mirroring
    # `test_line_breakpoints_ignores_an_unbalanced_endif` for `#endif`.
    source = "#elif 0\n#line 5\nx\n"
    assert _line_breakpoints(source) == [(2, 5)]


def test_line_breakpoints_ignores_an_unbalanced_elif_one() -> None:
    source = "#elif 1\n#line 5\nx\n"
    assert _line_breakpoints(source) == [(2, 5)]


def test_line_breakpoints_ignores_an_unbalanced_else() -> None:
    source = "#else\n#line 5\nx\n"
    assert _line_breakpoints(source) == [(2, 5)]


def test_line_breakpoints_ignores_a_line_directive_spliced_from_a_macro() -> None:
    # PR #156 follow-up: a physical line that only *looks* like `#line 1`
    # because it is spliced onto the previous line's backslash continuation
    # (here, a multi-line `#define`'s replacement text) is not a directive to
    # cpp at all — line splicing (translation phase 2) merges it into the
    # `#define` before cpp ever looks for a leading `#`.
    source = "#define UNUSED tokens \\\n#line 1\nx\n"
    assert _line_breakpoints(source) == []


def test_line_breakpoints_ignores_a_conditional_directive_spliced_from_a_macro() -> (
    None
):
    # The splicing hazard applies to every directive regex, not just
    # `#line` itself: an unfiltered, spliced-in `#if 0` would open a
    # permanently-dead frame (no matching `#endif` exists in the real source
    # to close it) and silently drop every later breakpoint in the file.
    source = "#define X \\\n#if 0\n#line 5\nx\n"
    assert _line_breakpoints(source) == [(3, 5)]


def test_annotate_array_extents_ignores_a_line_directive_spliced_from_a_macro() -> None:
    # End-to-end: the inactive array-shaped `f` sits behind a real `#if 0`,
    # and the active pointer `f` follows a `#define`/`#line`-lookalike pair.
    # Without splicing awareness, the phantom `#line 1` would make the active
    # definition's translated presumed line collide with (or fall closer to)
    # the inactive one's, than the definition clang actually reports (6).
    source = (
        "#if 0\nvoid f(int p[20]) { }\n#endif\n"
        "#define UNUSED tokens \\\n#line 1\nvoid f(int *p) { *p = 1; }\n"
    )
    unit = Unit("f", (Param("p", "int *"),), def_line=6)
    out = annotate_array_extents([unit], source)[0].params[0]
    assert (out.array_extent, out.array_extent_unresolved) == (None, False)


def test_line_breakpoints_joins_a_spliced_if_condition() -> None:
    # PR #156 follow-up: `#if \` + newline + `0` is one logical `#if 0` to cpp
    # (line splicing runs before the preprocessor ever evaluates a condition),
    # but capturing only the first physical line's text would see just a
    # trailing backslash and misclassify the branch as opaque/live, retaining
    # the `#line` inside what is actually a dead branch.
    source = "#if \\\n0\n#line 5\nx\n#endif\n"
    assert _line_breakpoints(source) == []


def test_line_breakpoints_joins_a_spliced_elif_condition() -> None:
    # Mirror of the `#if` case: `#elif \` + newline + `0` is one logical
    # `#elif 0` to cpp — still dead regardless of the opaque `#if X` before it
    # — so a `#line` inside it must not count.
    source = "#if X\n#elif \\\n0\n#line 5\nx\n#endif\n"
    assert _line_breakpoints(source) == []


def test_line_breakpoints_joins_a_spliced_line_directive_digit() -> None:
    # PR #156 follow-up: `#line \` + newline + `11` is one logical `#line 11`
    # to cpp (line splicing runs before cpp ever reads the directive), but
    # capturing only the first physical line's digits would see none at all
    # and silently drop the breakpoint — not a safe failure: with no
    # breakpoint recorded, both an active and an inactive same-named
    # candidate translate to their own physical line, and the nearest-before
    # tiebreak can then pick whichever sits physically closer to `def_line`,
    # regardless of which one cpp actually compiled.
    source = "#line \\\n11\nx\n"
    assert _line_breakpoints(source) == [(1, 11)]


def test_annotate_array_extents_ignores_a_line_directive_spliced_from_a_line() -> None:
    # End-to-end, confirmed against esbmc's own parse tree: `#line \` +
    # newline + `11` reports `f`'s definition at presumed line 12. An
    # inactive array-shaped `f` sits behind a real `#if 0` right after it; if
    # the spliced `#line`'s breakpoint is dropped, both candidates fall back
    # to their physical lines (3 and 5) and the nearest-before tiebreak picks
    # the closer, inactive one (5) over the active pointer definition (3),
    # copying its extent onto the compiled pointer parameter.
    source = (
        "#line \\\n11\nvoid f(int *p) { *p = 1; }\n"
        "#if 0\nvoid f(int p[20]) { }\n#endif\n"
    )
    unit = Unit("f", (Param("p", "int *"),), def_line=12)
    out = annotate_array_extents([unit], source)[0].params[0]
    assert (out.array_extent, out.array_extent_unresolved) == (None, False)


def test_annotate_array_extents_ignores_a_line_directive_in_a_spliced_if_zero() -> None:
    # End-to-end: without joining the spliced condition, `#if \` + newline +
    # `0` reads as opaque/live, so the `#line 11` guarding the inactive
    # array-shaped `f` stays a real breakpoint and can collide with the active
    # definition's own presumed line.
    source = (
        "#if \\\n0\n#line 11\nvoid f(int p[20]) { }\n#endif\n"
        "#line 11\nvoid f(int *p) { *p = 1; }\n"
    )
    unit = Unit("f", (Param("p", "int *"),), def_line=11)
    out = annotate_array_extents([unit], source)[0].params[0]
    assert (out.array_extent, out.array_extent_unresolved) == (None, False)


def test_annotate_array_extents_breaks_a_genuine_presumed_tie_by_physical_line() -> (
    None
):
    # A tie unrelated to `#if 0`/`#ifdef` entirely — two `#line 5` directives
    # outside any conditional, each immediately ahead of a definition-shaped `f`.
    # Nothing here is preprocessor-dead, so both directives are real breakpoints
    # and both candidates translate to presumed line 5, genuinely equidistant
    # from `def_line`. This is what the physical-line tiebreak (`-c[0]`) exists
    # for: `min`'s stability would otherwise keep the textually-first (here,
    # wrong) candidate.
    source = "#line 5\nvoid f(int p[20]) { }\n#line 5\nvoid f(int *p) { *p = 1; }\n"
    unit = Unit("f", (Param("p", "int *"),), def_line=5)
    out = annotate_array_extents([unit], source)[0].params[0]
    assert (out.array_extent, out.array_extent_unresolved) == (None, False)


def test_blank_comment_preserves_line_numbers_across_a_multiline_comment() -> None:
    # `_param_list_text` compares byte-offset-derived line numbers in the
    # comment-stripped text against `Unit.def_line`, which clang reports against
    # the *original* source. A multi-line block comment collapsed to a single
    # space (as a naive `re.sub(_COMMENT_RE, " ", ...)` would) shifts every line
    # after it, breaking that comparison — `mask_comments` must instead keep the
    # comment's newlines so line numbers stay aligned with the original source.
    source = "int a;\n/* line one\nline two\nline three */\nint b;\n"
    stripped = mask_comments(source)
    assert stripped.count("\n") == source.count("\n")
    b_line_original = next(
        i for i, line in enumerate(source.splitlines(), 1) if "int b;" in line
    )
    b_line_stripped = next(
        i for i, line in enumerate(stripped.splitlines(), 1) if "int b;" in line
    )
    assert b_line_stripped == b_line_original == 5


def test_annotate_array_extents_anchors_correctly_across_a_multiline_comment() -> None:
    # A realistic case combining both mechanisms: a multi-line doc comment sits
    # between the inactive `#if 0` body and the active definition, so a correct
    # anchor depends on `mask_comments` keeping the active definition's line
    # number aligned with the `def_line` clang reports against the real source.
    source = (
        "#if 0\n"
        "void f(int p[20]) { }\n"
        "#endif\n"
        "/* Documents f.\n"
        " * See also: nothing.\n"
        " */\n"
        "void f(int *p) { *p = 1; }\n"
    )
    def_line = next(
        i for i, line in enumerate(source.splitlines(), 1) if "int *p" in line
    )
    assert def_line == 7
    unit = Unit("f", (Param("p", "int *"),), def_line=def_line)
    out = annotate_array_extents([unit], source)[0].params[0]
    assert (out.array_extent, out.array_extent_unresolved) == (None, False)


def test_blank_comment_absorbs_a_backslash_continued_line_comment() -> None:
    # PR #156 follow-up: cpp splices a `\`-terminated line onto the next before
    # it ever recognizes a `//` comment (translation phase 2 runs before phase
    # 3), so a directive-looking line right after such a splice is part of the
    # comment, not a directive. `_COMMENT_RE` must absorb the continuation so
    # the directive-looking text is blanked away with it, not left exposed.
    source = "int a;\n// comment \\\n#line 1\nint b;\n"
    stripped = mask_comments(source)
    assert stripped.count("\n") == source.count("\n")
    assert "#line" not in stripped
    b_line_original = next(
        i for i, line in enumerate(source.splitlines(), 1) if "int b;" in line
    )
    b_line_stripped = next(
        i for i, line in enumerate(stripped.splitlines(), 1) if "int b;" in line
    )
    assert b_line_stripped == b_line_original == 4


def test_annotate_array_extents_ignores_a_line_directive_spliced_from_a_comment() -> (
    None
):
    # End-to-end: the earlier continuation fix (1dc377c) only scanned the
    # already-blanked text, where a backslash inside a `//` comment was already
    # erased along with the comment — invisible to that continuation check. The
    # phantom `#line 1` must instead never survive comment-blanking at all.
    source = (
        "#if 0\nvoid f(int p[20]) { }\n#endif\n"
        "// c \\\n#line 1\nvoid f(int *p) { *p = 1; }\n"
    )
    unit = Unit("f", (Param("p", "int *"),), def_line=6)
    out = annotate_array_extents([unit], source)[0].params[0]
    assert (out.array_extent, out.array_extent_unresolved) == (None, False)


_HAVE_ESBMC = shutil.which("esbmc") is not None


@pytest.mark.skipif(not _HAVE_ESBMC, reason="needs esbmc on PATH")
def test_list_units_end_to_end(tmp_path: Path) -> None:
    src = tmp_path / "sig.c"
    src.write_text(
        "#include <stdint.h>\n"
        "typedef void (*cb_t)(void);\n"
        "int scal(int x) { return x; }\n"
        "uint32_t hash(const uint8_t *k, unsigned long n){return n?k[0]:0;}\n"
        "void reg(cb_t cb) { cb(); }\n"
    )
    units = {u.name: u.takes_pointer for u in list_units(src)}
    assert units == {"scal": False, "hash": True, "reg": True}


@pytest.mark.skipif(not _HAVE_ESBMC, reason="needs esbmc on PATH")
def test_list_units_recovers_fixed_array_extent(tmp_path: Path) -> None:
    # End-to-end: clang adjusts `uint8_t digest[20]` to `uint8_t *` (extent lost
    # from the type), so `list_units` must recover the 20 from the source.
    src = tmp_path / "sig.c"
    src.write_text(
        "#include <stdint.h>\n#include <stddef.h>\n"
        "void f(uint8_t digest[20], const uint8_t *data, size_t len) {\n"
        "  (void)digest; (void)data; (void)len;\n}\n"
    )
    f = next(u for u in list_units(src) if u.name == "f")
    extents = {p.name: p.array_extent for p in f.params}
    assert extents == {"digest": 20, "data": None, "len": None}
    assert not any(p.array_extent_unresolved for p in f.params)


@pytest.mark.skipif(not _HAVE_ESBMC, reason="needs esbmc on PATH")
def test_list_units_marks_macro_extent_unresolved(tmp_path: Path) -> None:
    # End-to-end (issue #137): clang adjusts both parameters to `uint8_t *`, so the
    # macro extent is unrecoverable from the type *and* from the source declarator
    # (it needs the preprocessor) — it must be flagged, not read as a plain pointer.
    # A `[static N]` extent in the same signature is still recovered.
    src = tmp_path / "sig.c"
    src.write_text(
        "#include <stdint.h>\n"
        "#define DLEN 20\n"
        "void f(uint8_t digest[DLEN], uint8_t tag[static 16], uint8_t *raw) {\n"
        "  (void)digest; (void)tag; (void)raw;\n}\n"
    )
    f = next(u for u in list_units(src) if u.name == "f")
    shapes = {p.name: (p.array_extent, p.array_extent_unresolved) for p in f.params}
    assert shapes == {
        "digest": (None, True),
        "tag": (16, False),
        "raw": (None, False),
    }


@pytest.mark.skipif(not _HAVE_ESBMC, reason="needs esbmc on PATH")
def test_list_units_anchors_extent_to_active_if0_definition(tmp_path: Path) -> None:
    # End-to-end (issue #145): an inactive `#if 0` body must not donate its array
    # extent to the definition clang actually compiled. The over-sized hazard this
    # closes is the dangerous direction — it can mask a real out-of-bounds by
    # backing a plain `int *p` with a too-large object (VIOLATED -> VERIFIED).
    src = tmp_path / "sig.c"
    src.write_text("#if 0\nvoid f(int p[20]) { }\n#endif\nvoid f(int *p) { *p = 1; }\n")
    f = next(u for u in list_units(src) if u.name == "f")
    assert f.params[0].array_extent is None
    assert f.params[0].array_extent_unresolved is False


@pytest.mark.skipif(not _HAVE_ESBMC, reason="needs esbmc on PATH")
def test_list_units_anchors_extent_to_ifdef_selected_definition(tmp_path: Path) -> None:
    # Same anchor, for the other conditional-compilation shape the issue calls
    # out: an `#ifdef`-selected alternative definition. Whichever branch clang
    # actually compiles is the one the extent must come from.
    src = tmp_path / "sig.c"
    src.write_text(
        "#ifdef WIDGET\n"
        "void f(int p[20]) { (void)p; }\n"
        "#else\n"
        "void f(int *p) { *p = 1; }\n"
        "#endif\n"
    )
    f_default = next(u for u in list_units(src) if u.name == "f")
    assert f_default.params[0].array_extent is None
    assert f_default.params[0].array_extent_unresolved is False

    f_widget = next(
        u for u in list_units(src, extra_flags=("-DWIDGET",)) if u.name == "f"
    )
    assert f_widget.params[0].array_extent == 20


@pytest.mark.skipif(not _HAVE_ESBMC, reason="needs esbmc on PATH")
def test_list_units_anchors_extent_across_a_line_directive(tmp_path: Path) -> None:
    # End-to-end (issue #145 follow-up): a `#line` directive makes clang report the
    # active definition's presumed line (1) well before its physical line (5) —
    # earlier, even, than the inactive `#if 0` body's physical line (2). Comparing
    # `def_line` straight against physical candidate lines would pick the inactive
    # body and copy its extent onto the active `int *p`.
    src = tmp_path / "sig.c"
    src.write_text(
        "#if 0\nvoid f(int p[20]) { }\n#endif\n#line 1\nvoid f(int *p) { *p = 1; }\n"
    )
    f = next(u for u in list_units(src) if u.name == "f")
    assert f.params[0].array_extent is None
    assert f.params[0].array_extent_unresolved is False


@pytest.mark.skipif(not _HAVE_ESBMC, reason="needs esbmc on PATH")
def test_list_units_anchors_extent_past_a_macro_return_type(tmp_path: Path) -> None:
    # End-to-end (PR #156 follow-up): with a macro'd return type, clang's
    # `FunctionDecl` range starts at the macro's *definition* line, not the
    # function's own line. Anchoring `def_line` on the range start would land it
    # on the `#define` line, ahead of the inactive `#if 0` body sitting between
    # the macro and the active definition — donating the inactive body's extent.
    src = tmp_path / "sig.c"
    src.write_text(
        "#define API void\n"
        "#if 0\n"
        "API f(int p[20]) { }\n"
        "#endif\n"
        "API f(int *p) { *p = 1; }\n"
    )
    f = next(u for u in list_units(src) if u.name == "f")
    assert f.params[0].array_extent is None
    assert f.params[0].array_extent_unresolved is False


@pytest.mark.skipif(not _HAVE_ESBMC, reason="needs esbmc on PATH")
def test_list_units_ignores_a_line_directive_inside_a_dead_if0_body(
    tmp_path: Path,
) -> None:
    # End-to-end (PR #156 follow-up): two `#line 11` directives, one ahead of an
    # inactive `#if 0` body and one ahead of the active definition, would
    # translate both candidates to presumed line 11 if both counted — the same
    # value clang reports for the active definition's `def_line`, an unbreakable
    # tie. `_line_breakpoints` excludes the one inside the dead `#if 0`, so the
    # inactive candidate never collides with the active one in the first place.
    src = tmp_path / "sig.c"
    src.write_text(
        "#if 0\n#line 11\nvoid f(int p[20]) { }\n#endif\n"
        "#line 11\nvoid f(int *p) { *p = 1; }\n"
    )
    f = next(u for u in list_units(src) if u.name == "f")
    assert f.params[0].array_extent is None
    assert f.params[0].array_extent_unresolved is False


@pytest.mark.skipif(not _HAVE_ESBMC, reason="needs esbmc on PATH")
def test_list_units_anchors_extent_with_whitespace_in_the_path(
    tmp_path: Path,
) -> None:
    # End-to-end (PR #156 follow-up): clang echoes the input path verbatim,
    # spaces included, when the source lives in a directory whose name has one.
    # A token pattern that requires a whitespace-free path would leave `def_line`
    # unset, letting the inactive `#if 0` body's array extent donate to the
    # active `int *p` — the exact over-sized-object hazard issue #145 exists to
    # close.
    src_dir = tmp_path / "path space"
    src_dir.mkdir()
    src = src_dir / "sig.c"
    src.write_text("#if 0\nvoid f(int p[20]) { }\n#endif\nvoid f(int *p) { *p = 1; }\n")
    f = next(u for u in list_units(src) if u.name == "f")
    assert f.params[0].array_extent is None
    assert f.params[0].array_extent_unresolved is False


# The brittleness class from issue #131: every shape a regex over the source text
# gets wrong. Each case is `(source, {function: takes_pointer})` — the *full*
# expected unit list, so a case also pins which functions are enumerated at all
# (a `#if 0` body must not appear; a function-like macro is not a unit).
_BRITTLE_CASES: list[tuple[str, str, dict[str, bool]]] = [
    (
        "comment_in_params",
        # The false negative that motivated #131 (caught by the Codex reviewer on
        # #130): the `*` of `/* input */` made the regex read a pointer, parking a
        # scalar unit in non-blocking NEEDS_CONTRACT with its ESBMC run skipped.
        "int neg(int x /* input */) { return -x; }\n",
        {"neg": False},
    ),
    (
        "adjusted_function_type_param",
        # C adjusts a function-type parameter to pointer-to-function. No `*`, `[`
        # or `(` appears in `int cb(void)`'s param text, so a regex sees a scalar
        # and the gate would then hit a phantom pointer failure at verify time.
        "void reg_adj(int cb(void)) { (void)cb; }\n",
        {"reg_adj": True},
    ),
    (
        "typedefd_function_pointer_param",
        # The form a `(`-based regex patch still could not catch (#131 comment).
        "typedef void (*cb_t)(void);\nvoid reg_td(cb_t cb) { cb(); }\n",
        {"reg_td": True},
    ),
    (
        "typedefd_pointer_param",
        # The issue body's example: `str` hides the `*` behind a typedef.
        "typedef char *str;\nvoid takes_str(str s) { (void)s; }\n",
        {"takes_str": True},
    ),
    (
        "knr_definition",
        # K&R: the parameter *types* are not in the parenthesised list at all.
        "int knr(a, b)\nint a;\nchar *b;\n{ return a + (b ? 1 : 0); }\n",
        {"knr": True},
    ),
    (
        "return_type_on_its_own_line",
        "static unsigned long\nown_line(unsigned long n)\n{ return n; }\n",
        {"own_line": False},
    ),
    (
        "multi_line_signature",
        "int multi(\n    int a,\n    char *b\n) { return a + (b ? 1 : 0); }\n",
        {"multi": True},
    ),
    (
        "function_like_macro",
        # `SQUARE(x)` looks exactly like a definition to a regex; it is not a unit,
        # and the function that *uses* it stays scalar.
        "#define SQUARE(x) ((x) * (x))\nint use_macro(int v) { return SQUARE(v); }\n",
        {"use_macro": False},
    ),
    (
        "preprocessor_conditional",
        # A regex has no preprocessor: it would enumerate the `#if 0` body and gate
        # a function that does not exist in this translation unit.
        "#if 0\nvoid never_defined(char *p) { (void)p; }\n#endif\n"
        "#if 1\nint only_in_if(int x) { return x; }\n#endif\n",
        {"only_in_if": False},
    ),
    (
        "attribute_decorated",
        "__attribute__((noinline)) int attr_fn(int x) { return x; }\n"
        "int attr_ptr(char *p) __attribute__((nonnull));\n"
        "int attr_ptr(char *p) { return *p; }\n",
        {"attr_fn": False, "attr_ptr": True},
    ),
    (
        "star_in_string_and_char_literal",
        'const char *g;\nint lit(int n) { g = "a * b"; return n; }\n'
        "int lit_char(int n) { char c = '*'; return n + c; }\n",
        {"lit": False, "lit_char": False},
    ),
    (
        "array_param",
        # Clang adjusts `int p[10]` to `int *`; the unit still takes a pointer.
        "void arr(int p[10]) { (void)p; }\n",
        {"arr": True},
    ),
]


@pytest.mark.skipif(not _HAVE_ESBMC, reason="needs esbmc on PATH")
@pytest.mark.parametrize(
    "name, source, expected", _BRITTLE_CASES, ids=[c[0] for c in _BRITTLE_CASES]
)
def test_list_units_classifies_the_brittleness_class(
    tmp_path: Path, name: str, source: str, expected: dict[str, bool]
) -> None:
    # Issue #131's acceptance list, end-to-end through the real frontend: each of
    # these is a shape the old adapter regex misread. Asserting the *whole* mapping
    # (not just the pointer flags) also pins the enumerated set, so a macro or a
    # `#if 0` body appearing as a unit fails here too.
    src = tmp_path / f"{name}.c"
    src.write_text(source)
    assert {u.name: u.takes_pointer for u in list_units(src)} == expected


@pytest.mark.skipif(not _HAVE_ESBMC, reason="needs esbmc on PATH")
def test_list_units_forwards_build_flags(tmp_path: Path) -> None:
    # `extra_flags` is what makes a real project's C parseable at all. Without the
    # `-I` this source cannot resolve its `#include` and esbmc exits nonzero (see
    # the companion assertion below); with it, `-DWIDGET` further changes *which*
    # functions the translation unit defines — so the flags are load-bearing for
    # the unit list, not a convenience.
    inc = tmp_path / "inc"
    inc.mkdir()
    (inc / "mytypes.h").write_text("typedef unsigned char byte_t;\n")
    src = tmp_path / "u.c"
    src.write_text(
        '#include "mytypes.h"\n'
        "int scal(byte_t b) { return b; }\n"
        "void ptr(byte_t *p) { (void)p; }\n"
        "#ifdef WIDGET\n"
        "int only_with_define(int x) { return x; }\n"
        "#endif\n"
    )
    units = list_units(src, extra_flags=("-I", str(inc), "-DWIDGET"))
    assert {u.name: u.takes_pointer for u in units} == {
        "scal": False,
        "ptr": True,
        "only_with_define": False,
    }
    with pytest.raises(ListUnitsError):
        list_units(src)  # the same source without its include path


@pytest.mark.skipif(not _HAVE_ESBMC, reason="needs esbmc on PATH")
def test_list_units_raises_on_failed_parse(tmp_path: Path) -> None:
    # A failed esbmc run (nonzero exit) must raise, never return [] — [] is
    # indistinguishable from a valid declaration-only file and would let the gate
    # silently skip a unit.
    with pytest.raises(ListUnitsError):
        list_units(tmp_path / "does_not_exist.c")  # missing source → esbmc exit 6
    bad = tmp_path / "bad.c"
    bad.write_text("int f( {\n")  # malformed C → parse error
    with pytest.raises(ListUnitsError):
        list_units(bad)


# --- the call set and the body locator (RFC-0003 S3) ---------------------------

_CALLS_AST = """\
TranslationUnitDecl 0x2000 <<invalid sloc>> <invalid sloc>
|-FunctionDecl 0x2001 </tmp/foo.c:1:1, col:40> col:6 leaf 'void (int *)'
| |-ParmVarDecl 0x2002 <col:12, col:18> col:18 used p 'int *'
| `-CompoundStmt 0x2003 <col:24, col:40>
`-FunctionDecl 0x2010 <line:2:1, col:60> col:6 caller 'void (int *)'
  |-ParmVarDecl 0x2011 <col:12, col:18> col:18 used q 'int *'
  `-CompoundStmt 0x2012 <col:24, col:60>
    |-CallExpr 0x2013 <col:26, col:36> 'void'
    | `-ImplicitCastExpr 0x2014 <col:26> 'void (*)(int *)' <FunctionToPointerDecay>
    |   `-DeclRefExpr 0x2015 <col:26> 'void (int *)' Function 0x2001 'leaf' 'void (i)'
    |-DeclRefExpr 0x2016 <col:31> 'int *' lvalue ParmVar 0x2011 'q' 'int *'
    `-CallExpr 0x2017 <col:40, col:50> 'void'
      `-DeclRefExpr 0x2018 <col:40> 'void (int *)' Function 0x2001 'leaf' 'void (i)'
"""


def test_parse_units_collects_the_call_set() -> None:
    units = {u.name: u for u in parse_units(_CALLS_AST, _TARGET)}
    assert units["caller"].calls == ("leaf",)  # deduped, and only *functions*
    assert units["leaf"].calls == ()


def test_annotate_array_extents_keeps_the_call_set() -> None:
    unit = Unit("caller", (Param("q", "int *"),), ("leaf",))
    out = annotate_array_extents([unit], "void caller(int *q){ leaf(q); }")[0]
    assert out.calls == ("leaf",)


def test_mask_comments_preserves_offsets_and_lines() -> None:
    text = "int a; /* hidden {\n brace */ int b; // trailing {\nint c;\n"
    masked = mask_comments(text)
    assert len(masked) == len(text)
    assert masked.count("\n") == text.count("\n")
    assert "{" not in masked
    assert masked.startswith("int a; ")


def test_find_definition_brace_indexes_the_body_opener() -> None:
    source = "/* f(int *p) { decoy } */\nvoid f(int *p) { *p = 0; }\n"
    index = find_definition_brace(source, "f")
    assert index is not None
    assert source[index] == "{"
    assert source[:index].count("\n") == 1  # the real one, not the comment's


def test_find_definition_brace_skips_prototypes_and_call_sites() -> None:
    source = "void f(int *p);\nvoid g(int *p) { f(p); }\nvoid f(int *p) { *p = 0; }\n"
    index = find_definition_brace(source, "f")
    assert index is not None
    assert source[:index].count("\n") == 2


def test_find_definition_brace_absent_without_a_definition() -> None:
    assert find_definition_brace("void f(int *p);\n", "f") is None


_HEADER_CALLER_AST = """\
TranslationUnitDecl 0x3000 <<invalid sloc>> <invalid sloc>
|-FunctionDecl 0x3001 </tmp/foo.c:1:1, col:40> col:6 leaf 'void (int *)'
| |-ParmVarDecl 0x3002 <col:12, col:18> col:18 used p 'int *'
| `-CompoundStmt 0x3003 <col:24, col:40>
|-FunctionDecl 0x3010 </tmp/helper.h:2:1, col:60> col:20 header_client 'void (int *)'
| |-ParmVarDecl 0x3011 <col:12, col:18> col:18 used q 'int *'
| `-CompoundStmt 0x3012 <col:24, col:60>
|   `-DeclRefExpr 0x3013 <col:26> 'void (int *)' Function 0x3001 'leaf' 'void (i)'
|-FunctionDecl 0x3020 </tmp/helper.h:9:1, col:30> col:20 header_proto 'void (int *)'
| `-DeclRefExpr 0x3021 <col:26> 'void (int *)' Function 0x3001 'leaf' 'void (i)'
|-FunctionDecl 0x3025 </tmp/helper.h:14:1, col:30> col:20 header_other 'int ()'
| `-CompoundStmt 0x3026 <col:24, col:30>
`-FunctionDecl 0x3030 </tmp/foo.c:12:1, col:60> col:6 local_client 'void (int *)'
  |-ParmVarDecl 0x3031 <col:12, col:18> col:18 used q 'int *'
  `-CompoundStmt 0x3032 <col:24, col:60>
    `-DeclRefExpr 0x3033 <col:26> 'void (int *)' Function 0x3001 'leaf' 'void (i)'
"""


def test_parse_definitions_keeps_every_file() -> None:
    files = {name for name, _ in parse_definitions(_HEADER_CALLER_AST)}
    assert files == {"/tmp/foo.c", "/tmp/helper.h"}


def test_parse_external_callers_finds_header_definitions() -> None:
    # The blind spot `parse_units` has by construction: a definition in an
    # included header is in the same TU but is not a unit the gate enumerates.
    assert "header_client" not in {
        u.name for u in parse_units(_HEADER_CALLER_AST, _TARGET)
    }
    assert parse_external_callers(_HEADER_CALLER_AST, _TARGET, "leaf") == (
        "header_client",
    )


def test_parse_external_callers_ignores_prototypes_and_local_callers() -> None:
    external = parse_external_callers(_HEADER_CALLER_AST, _TARGET, "leaf")
    assert "header_proto" not in external  # no CompoundStmt → not a definition
    assert "local_client" not in external  # in the target file → an enumerable unit
    assert "header_other" not in external  # a header definition that never calls it


def test_parse_external_callers_is_relative_to_the_given_source() -> None:
    # Same dump, different file under test: what counts as "outside" flips.
    assert parse_external_callers(_HEADER_CALLER_AST, "/tmp/helper.h", "leaf") == (
        "local_client",
    )


_RECURSIVE_AST = """\
TranslationUnitDecl 0x4000 <<invalid sloc>> <invalid sloc>
`-FunctionDecl 0x4001 </tmp/helper.h:1:1, col:40> col:6 leaf 'void (int)'
  |-ParmVarDecl 0x4002 <col:12, col:18> col:18 used n 'int'
  `-CompoundStmt 0x4003 <col:24, col:40>
    `-DeclRefExpr 0x4004 <col:26> 'void (int)' Function 0x4001 'leaf' 'void (i)'
"""


def test_parse_external_callers_never_reports_the_symbol_itself() -> None:
    # A recursive definition outside the file under test references its own name;
    # reporting it would make every recursive callee permanently undischargeable
    # for a reason that is not a caller at all.
    assert parse_external_callers(_RECURSIVE_AST, _TARGET, "leaf") == ()


# Every way a C source can name `leaf` without calling it, in the shapes esbmc
# 8.3.0 prints them (captured from a probe, abbreviated): a file-scope
# initialiser, an `&leaf`, an `fp = leaf` inside a body, and a callback argument
# — against the shapes that *are* a direct call: `leaf(...)`, `(leaf)(...)`,
# `(*leaf)(...)`, and a recursive self-call. `indirect` calls through `fp` and so
# names only the variable, which is the whole reason the escapes must be found.
_FUNCTION_POINTER_AST = """\
TranslationUnitDecl 0x5000 <<invalid sloc>> <invalid sloc>
|-FunctionDecl 0x5001 </tmp/foo.c:1:1, col:38> col:6 used leaf 'void (int *, int)'
| |-ParmVarDecl 0x5002 <col:11, col:16> col:16 used p 'int *'
| `-CompoundStmt 0x5003 <col:26, col:38>
|   `-CallExpr 0x5004 <col:28, col:37> 'void'
|     |-ImplicitCastExpr 0x5005 <col:28> 'void (*)(i)' <FunctionToPointerDecay>
|     | `-DeclRefExpr 0x5006 <col:28> 'void (i)' Function 0x5001 'leaf' 'void (i)'
|     `-DeclRefExpr 0x5007 <col:33> 'int *' lvalue ParmVar 0x5002 'p' 'int *'
|-VarDecl 0x5010 <line:4:1, col:18> col:13 used fp 'cb_t':'void (*)(i)' static cinit
| `-ImplicitCastExpr 0x5011 <col:18> 'void (*)(i)' <FunctionToPointerDecay>
|   `-DeclRefExpr 0x5012 <col:18> 'void (i)' Function 0x5001 'leaf' 'void (i)'
|-VarDecl 0x5020 <line:5:1, col:22> col:13 alias 'cb_t':'void (*)(i)' static cinit
| `-UnaryOperator 0x5021 <col:21, col:22> 'void (*)(i)' prefix '&' cannot overflow
|   `-DeclRefExpr 0x5022 <col:22> 'void (i)' Function 0x5001 'leaf' 'void (i)'
|-FunctionDecl 0x5030 <line:7:1, col:42> col:6 direct 'void (int *, int)'
| |-ParmVarDecl 0x5031 <col:13, col:18> col:18 used q 'int *'
| `-CompoundStmt 0x5032 <col:28, col:42>
|   `-CallExpr 0x5033 <col:30, col:39> 'void'
|     |-ImplicitCastExpr 0x5034 <col:30> 'void (*)(i)' <FunctionToPointerDecay>
|     | `-DeclRefExpr 0x5035 <col:30> 'void (i)' Function 0x5001 'leaf' 'void (i)'
|     `-DeclRefExpr 0x5036 <col:35> 'int *' lvalue ParmVar 0x5031 'q' 'int *'
|-FunctionDecl 0x5040 <line:8:1, col:43> col:6 paren 'void (int *, int)'
| `-CompoundStmt 0x5041 <col:27, col:43>
|   `-CallExpr 0x5042 <col:29, col:40> 'void'
|     `-ImplicitCastExpr 0x5043 <col:29, col:34> 'void (*)(i)' <FunctionToPointerDecay>
|       `-ParenExpr 0x5044 <col:29, col:34> 'void (i)'
|         `-DeclRefExpr 0x5045 <col:30> 'void (i)' Function 0x5001 'leaf' 'void (i)'
|-FunctionDecl 0x5050 <line:9:1, col:44> col:6 deref 'void (int *, int)'
| `-CompoundStmt 0x5051 <col:27, col:44>
|   `-CallExpr 0x5052 <col:29, col:41> 'void'
|     `-ImplicitCastExpr 0x5053 <col:29, col:35> 'void (*)(i)' <FunctionToPointerDecay>
|       `-ParenExpr 0x5054 <col:29, col:35> 'void (i)'
|         `-UnaryOperator 0x5055 <col:30, col:31> 'void (i)' prefix '*' cannot overflow
|           `-ImplicitCastExpr 0x5056 <col:31> 'void (*)(i)' <FunctionToPointerDecay>
|             `-DeclRefExpr 0x5057 <col:31> 'void (i)' Function 0x5001 'leaf' 'void (i)'
|-FunctionDecl 0x5060 <line:10:1, col:42> col:6 indirect 'void (int *, int)'
| |-ParmVarDecl 0x5061 <col:15, col:20> col:20 used q 'int *'
| `-CompoundStmt 0x5062 <col:30, col:42>
|   `-CallExpr 0x5063 <col:32, col:39> 'void'
|     |-ImplicitCastExpr 0x5064 <col:32> 'cb_t':'void (*)(i)' <LValueToRValue>
|     | `-DeclRefExpr 0x5065 <col:32> 'cb_t':'void (*)(i)' lvalue Var 0x5010 'fp' 'cb_t'
|     `-DeclRefExpr 0x5066 <col:35> 'int *' lvalue ParmVar 0x5061 'q' 'int *'
|-FunctionDecl 0x5070 <line:11:1, col:31> col:6 taker 'void (void)'
| `-CompoundStmt 0x5071 <col:18, col:31>
|   `-BinaryOperator 0x5072 <col:20, col:25> 'cb_t':'void (*)(i)' '='
|     |-DeclRefExpr 0x5073 <col:20> 'cb_t':'void (*)(i)' lvalue Var 0x5010 'fp' 'cb_t'
|     `-ImplicitCastExpr 0x5074 <col:25> 'void (*)(i)' <FunctionToPointerDecay>
|       `-DeclRefExpr 0x5075 <col:25> 'void (i)' Function 0x5001 'leaf' 'void (i)'
|-FunctionDecl 0x5080 <line:12:1, col:39> col:6 used apply 'void (cb_t, int *)'
| |-ParmVarDecl 0x5081 <col:12, col:17> col:17 used c 'cb_t':'void (*)(i)'
| `-CompoundStmt 0x5082 <col:28, col:39>
`-FunctionDecl 0x5090 <line:13:1, col:39> col:6 passer 'void (int *)'
  |-ParmVarDecl 0x5091 <col:13, col:18> col:18 used q 'int *'
  `-CompoundStmt 0x5092 <col:21, col:39>
    `-CallExpr 0x5093 <col:23, col:36> 'void'
      |-ImplicitCastExpr 0x5094 <col:23> 'void (*)(c)' <FunctionToPointerDecay>
      | `-DeclRefExpr 0x5095 <col:23> 'void (c)' Function 0x5080 'apply' 'void (c)'
      |-ImplicitCastExpr 0x5096 <col:29> 'void (*)(i)' <FunctionToPointerDecay>
      | `-DeclRefExpr 0x5097 <col:29> 'void (i)' Function 0x5001 'leaf' 'void (i)'
      `-DeclRefExpr 0x5098 <col:35> 'int *' lvalue ParmVar 0x5091 'q' 'int *'
"""


def test_parse_address_escapes_reports_every_site_that_takes_the_address() -> None:
    # `fp`/`alias` are file-scope objects holding it, `taker` stores it, `passer`
    # hands it to a callback parameter. Each is a path to `leaf` that no name in
    # `Unit.calls` leads back from.
    assert parse_address_escapes(_FUNCTION_POINTER_AST, "leaf") == (
        "fp",
        "alias",
        "taker",
        "passer",
    )


def test_parse_address_escapes_ignores_every_way_of_writing_a_direct_call() -> None:
    # `leaf(...)`, `(leaf)(...)` and `(*leaf)(...)` all reach the callee through
    # the same decay cast an *argument* does; only the child position tells them
    # apart. Treating one as an escape would make ordinary code undischargeable.
    escapes = parse_address_escapes(_FUNCTION_POINTER_AST, "leaf")
    assert not {"direct", "paren", "deref"} & set(escapes)
    # ... including the recursive self-call in `leaf`'s own body.
    assert "leaf" not in escapes


def test_an_indirect_call_leaves_no_trace_in_unit_calls() -> None:
    # Why the escapes have to be reported separately: `indirect` calls `leaf`
    # through `fp`, and its body names only the variable.
    units = {u.name: u for u in parse_units(_FUNCTION_POINTER_AST, _TARGET)}
    assert "leaf" not in units["indirect"].calls
    assert units["direct"].calls == ("leaf",)


_ORPHAN_REFERENCE_AST = """\
TranslationUnitDecl 0x6000 <<invalid sloc>> <invalid sloc>
`-DeclRefExpr 0x6001 <col:18> 'void (i)' Function 0x6002 'leaf' 'void (i)'
"""


# A caller whose body opens with a block-scope function declaration — legal ISO C,
# and clang prints the inner `FunctionDecl` *inside* the outer one's subtree. The
# call to `leaf` comes after it, so a parser that closes `local_client` at the
# inner declaration loses the edge that makes it a caller at all.
_NESTED_DECL_AST = """\
TranslationUnitDecl 0x8000 <<invalid sloc>> <invalid sloc>
|-FunctionDecl 0x8001 </tmp/foo.c:1:1, col:42> col:13 used leaf 'void (int *)' static
| |-ParmVarDecl 0x8002 <col:15, col:20> col:20 used p 'int *'
| `-CompoundStmt 0x8003 <col:30, col:42>
`-FunctionDecl 0x8010 <line:2:1, col:60> col:6 local_client 'void (int *)'
  |-ParmVarDecl 0x8011 <col:8, col:13> col:13 used q 'int *'
  `-CompoundStmt 0x8012 <col:23, col:60>
    |-DeclStmt 0x8013 <col:25, col:49>
    | `-FunctionDecl 0x8014 parent 0x8000 <col:25, col:48> col:37 helper 'v ()' extern
    `-CallExpr 0x8015 <col:51, col:57> 'void'
      |-ImplicitCastExpr 0x8016 <col:51> 'void (*)(i)' <FunctionToPointerDecay>
      | `-DeclRefExpr 0x8017 <col:51> 'void (i)' Function 0x8001 'leaf' 'void (i)'
      `-DeclRefExpr 0x8018 <col:53> 'int *' lvalue ParmVar 0x8011 'q' 'int *'
"""


def test_parse_units_keeps_calls_written_after_a_block_scope_declaration() -> None:
    units = {u.name: u for u in parse_units(_NESTED_DECL_AST, _TARGET)}
    assert units["local_client"].calls == ("leaf",)
    # the inner declaration has no body, so it is not a unit of its own
    assert "helper" not in units


# Linkage, in the shapes esbmc 8.3.0 prints it: the storage class follows the
# quoted type, and a `static` written on a *prototype* is printed there and not on
# the definition it forward-declares. The directory is called `static` on purpose —
# a path is printed *before* the type, and must not be read as a storage class.
_LINKAGE_TARGET = "/tmp/static/foo.c"
_LINKAGE_AST = """\
TranslationUnitDecl 0x7000 <<invalid sloc>> <invalid sloc>
|-FunctionDecl 0x7001 </tmp/static/foo.c:1:1, col:36> col:13 priv 'void (int *)' static
| |-ParmVarDecl 0x7002 <col:11, col:16> col:16 used p 'int *'
| `-CompoundStmt 0x7003 <col:26, col:36>
|-FunctionDecl 0x7010 <line:2:1, col:28> col:6 pub 'void (int *)'
| |-ParmVarDecl 0x7011 <col:11, col:16> col:16 used p 'int *'
| `-CompoundStmt 0x7012 <col:20, col:28>
|-FunctionDecl 0x7020 <line:3:1, col:23> col:13 fwd 'void (int *)' static
| `-ParmVarDecl 0x7021 <col:11, col:16> col:16 'int *'
`-FunctionDecl 0x7030 prev 0x7020 <line:4:1, col:28> col:6 fwd 'void (int *)'
  |-ParmVarDecl 0x7031 <col:11, col:16> col:16 used p 'int *'
  `-CompoundStmt 0x7032 <col:20, col:28>
"""


def test_parse_units_reads_internal_linkage() -> None:
    units = {u.name: u for u in parse_units(_LINKAGE_AST, _LINKAGE_TARGET)}
    assert units["priv"].internal_linkage is True
    # `pub` is in a *directory* called `static`; only the storage class counts.
    assert units["pub"].internal_linkage is False


def test_parse_units_reads_static_written_on_a_prototype() -> None:
    # `static void fwd(int *); void fwd(int *p) {}` — C gives the definition
    # internal linkage, but clang prints the marker only on the declaration that
    # carried it, so the whole TU has to be consulted.
    units = {u.name: u for u in parse_units(_LINKAGE_AST, _LINKAGE_TARGET)}
    assert units["fwd"].internal_linkage is True


def test_annotate_array_extents_keeps_linkage() -> None:
    # The extent pass rebuilds the unit; dropping the linkage there would silently
    # turn every `static` callee external and withhold its discharge.
    unit = Unit("f", (Param("p", "uint8_t *"),), (), internal_linkage=True)
    out = annotate_array_extents([unit], "static void f(uint8_t p[20]) {}")[0]
    assert out.params[0].array_extent == 20
    assert out.internal_linkage is True


# A GNU alias: `fa` *is* `leaf` at link time, but the call in `client` references
# only `fa`, so nothing in the AST joins that call site to `leaf`.
_ALIAS_AST = """\
TranslationUnitDecl 0x9000 <<invalid sloc>> <invalid sloc>
|-FunctionDecl 0x9001 </tmp/foo.c:1:1, col:42> col:13 used leaf 'void (int *)' static
| |-ParmVarDecl 0x9002 <col:15, col:20> col:20 used p 'int *'
| `-CompoundStmt 0x9003 <col:30, col:42>
|-FunctionDecl 0x9010 <line:2:1, col:57> col:13 used fa 'void (int *)' static
| |-ParmVarDecl 0x9011 <col:15, col:20> col:20 p 'int *'
| `-AliasAttr 0x9012 <col:46, col:55> "leaf"
`-FunctionDecl 0x9020 <line:3:1, col:39> col:6 client 'void (int *)'
  |-ParmVarDecl 0x9021 <col:13, col:18> col:18 used q 'int *'
  `-CompoundStmt 0x9022 <col:21, col:39>
    `-CallExpr 0x9023 <col:23, col:36> 'void'
      |-ImplicitCastExpr 0x9024 <col:23> 'void (*)(i)' <FunctionToPointerDecay>
      | `-DeclRefExpr 0x9025 <col:23> 'void (i)' Function 0x9010 'fa' 'void (i)'
      `-DeclRefExpr 0x9026 <col:29> 'int *' lvalue ParmVar 0x9021 'q' 'int *'
"""


def test_parse_symbol_aliases_reports_a_second_name_for_the_symbol() -> None:
    assert parse_symbol_aliases(_ALIAS_AST, "leaf") == ("fa",)
    # ... and the reason it has to: the call site names `fa` alone, so neither the
    # caller enumeration nor the escape scan sees a path to `leaf`.
    client = next(u for u in parse_units(_ALIAS_AST, _TARGET) if u.name == "client")
    assert client.calls == ("fa",)
    assert parse_address_escapes(_ALIAS_AST, "leaf") == ()


def test_parse_symbol_aliases_ignores_an_alias_to_another_symbol() -> None:
    assert parse_symbol_aliases(_ALIAS_AST, "client") == ()


# The two mechanisms combine: `fa`'s alias attribute names `leaf`'s *linker*
# symbol ("impl.sym") — the assembly label `leaf` is relabelled to — not its bare
# C name. Comparing an `AliasAttr` target against `symbol` alone (rather than
# `symbol`'s effective symbol set, the same one the label loop below computes)
# would miss this alias entirely.
_ALIAS_TO_LABELLED_TARGET_AST = """\
TranslationUnitDecl 0xd000 <<invalid sloc>> <invalid sloc>
|-FunctionDecl 0xd001 </tmp/foo.c:1:1, col:42> col:13 used leaf 'void (int *)' static
| |-ParmVarDecl 0xd002 <col:15, col:20> col:20 used p 'int *'
| |-CompoundStmt 0xd003 <col:30, col:42>
| `-AsmLabelAttr 0xd004 <col:39> "impl.sym" IsLiteralLabel
`-FunctionDecl 0xd010 <line:2:1, col:57> col:13 used fa 'void (int *)' static
  |-ParmVarDecl 0xd011 <col:15, col:20> col:20 p 'int *'
  `-AliasAttr 0xd012 <col:46, col:55> "impl.sym"
"""


def test_an_alias_names_a_labelled_targets_linker_symbol() -> None:
    assert parse_symbol_aliases(_ALIAS_TO_LABELLED_TARGET_AST, "leaf") == ("fa",)
    # the direction that matters for discharge: asking for callers of `fa` itself
    # (which names no one) still finds nothing, exactly as a plain alias would
    assert parse_symbol_aliases(_ALIAS_TO_LABELLED_TARGET_AST, "fa") == ()


# A `constructor` attribute names no one: unlike every other attribute-borne
# path this module follows, it sits on the callee's own `FunctionDecl` with no
# `Function 0x… 'name'` reference anywhere — the loader invokes `f` directly.
_CONSTRUCTOR_ATTR_AST = """\
TranslationUnitDecl 0xe000 <<invalid sloc>> <invalid sloc>
|-FunctionDecl 0xe001 </tmp/foo.c:1:1, col:60> col:20 used f 'void (int *)' static
| |-ParmVarDecl 0xe002 <col:22, col:27> col:27 used p 'int *'
| |-CompoundStmt 0xe003 <col:37, col:60>
| `-ConstructorAttr 0xe004 <col:8, col:19>
`-FunctionDecl 0xe010 <line:2:1, col:39> col:6 g 'void (int *)'
  |-ParmVarDecl 0xe011 <col:13, col:18> col:18 used q 'int *'
  `-CompoundStmt 0xe012 <col:21, col:39>
    `-CallExpr 0xe013 <col:23, col:36> 'void'
      |-ImplicitCastExpr 0xe014 <col:23> 'void (*)(i)' <FunctionToPointerDecay>
      | `-DeclRefExpr 0xe015 <col:23> 'void (i)' Function 0xe001 'f' 'void (i)'
      `-DeclRefExpr 0xe016 <col:29> 'int *' lvalue ParmVar 0xe011 'q' 'int *'
"""

_DESTRUCTOR_ATTR_AST = _CONSTRUCTOR_ATTR_AST.replace(
    "ConstructorAttr", "DestructorAttr"
)


def test_parse_implicit_invocations_reports_a_constructor_attribute() -> None:
    assert parse_implicit_invocations(_CONSTRUCTOR_ATTR_AST, "f") == ("constructor",)
    # neither of the other two attribute-borne paths sees it: it names no one
    assert parse_address_escapes(_CONSTRUCTOR_ATTR_AST, "f") == ()
    assert parse_symbol_aliases(_CONSTRUCTOR_ATTR_AST, "f") == ()
    # and a function with no such attribute reports none
    assert parse_implicit_invocations(_CONSTRUCTOR_ATTR_AST, "g") == ()


def test_parse_implicit_invocations_reports_a_destructor_attribute() -> None:
    assert parse_implicit_invocations(_DESTRUCTOR_ATTR_AST, "f") == ("destructor",)


# Two more attribute-borne paths esbmc 8.3.0 prints: a `cleanup` handler, which
# clang renders as an ordinary `Function 0x… 'name'` reference on the variable
# (so it is a *reference outside a call*, not an alias), and a shared `__asm__`
# label, which is one function under two C names with no reference at all.
_ATTRIBUTE_PATH_AST = """\
TranslationUnitDecl 0xa000 <<invalid sloc>> <invalid sloc>
|-FunctionDecl 0xa001 </tmp/foo.c:1:1, col:42> col:13 used leaf 'void (int *)' static
| |-ParmVarDecl 0xa002 <col:15, col:20> col:20 used p 'int *'
| |-CompoundStmt 0xa003 <col:30, col:42>
| `-AsmLabelAttr 0xa004 <col:39> "impl_sym" IsLiteralLabel
|-FunctionDecl 0xa010 <line:2:1, col:50> col:13 used twin 'void (int *)' static
| |-ParmVarDecl 0xa011 <col:15, col:20> col:20 p 'int *'
| `-AsmLabelAttr 0xa012 <col:40> "impl_sym" IsLiteralLabel
`-FunctionDecl 0xa020 <line:3:1, col:71> col:6 scope 'void (void)'
  `-CompoundStmt 0xa021 <col:15, col:71>
    `-DeclStmt 0xa022 <col:17, col:60>
      `-VarDecl 0xa023 <col:17, col:59> col:22 used c 'char' cinit
        `-CleanupAttr 0xa024 <col:38, col:53> Function 0xa001 'leaf' 'void (i)'
"""


def test_a_cleanup_handler_is_a_reference_outside_a_call() -> None:
    # `char c __attribute__((cleanup(leaf)))` calls `leaf` at scope exit, and no
    # expression in the AST spells that call — but clang still prints the same
    # `Function 0x…` reference an address-of would, which is why the escape scan
    # tests *position* rather than node kind.
    assert parse_address_escapes(_ATTRIBUTE_PATH_AST, "leaf") == ("c",)


def test_a_shared_assembly_label_makes_two_names_one_function() -> None:
    # The linker joins on the label, not the C name, so a call to `twin` reaches
    # `leaf` while referencing neither it nor its address.
    assert parse_symbol_aliases(_ATTRIBUTE_PATH_AST, "leaf") == ("twin",)
    assert parse_symbol_aliases(_ATTRIBUTE_PATH_AST, "twin") == ("leaf",)
    assert parse_symbol_aliases(_ATTRIBUTE_PATH_AST, "scope") == ()


# A shared label that carries punctuation a C identifier cannot, exactly as clang
# 8.3.0 prints `__asm__("impl.sym")` (verified live against esbmc
# --parse-tree-only): unescaped, period intact. `_TARGET_NAME_RE` used to require
# `\w+` between the quotes, which cannot match a period and so silently dropped
# this label — leaving `twin` looking like it shares nothing with `leaf`.
_PUNCTUATED_LABEL_AST = """\
TranslationUnitDecl 0xc000 <<invalid sloc>> <invalid sloc>
|-FunctionDecl 0xc001 </tmp/foo.c:1:1, col:42> col:13 used leaf 'void (int *)' static
| |-ParmVarDecl 0xc002 <col:15, col:20> col:20 used p 'int *'
| |-CompoundStmt 0xc003 <col:30, col:42>
| `-AsmLabelAttr 0xc004 <col:39> "impl.sym" IsLiteralLabel
`-FunctionDecl 0xc010 <line:2:1, col:50> col:13 used twin 'void (int *)' static
  |-ParmVarDecl 0xc011 <col:15, col:20> col:20 p 'int *'
  `-AsmLabelAttr 0xc012 <col:40> "impl.sym" IsLiteralLabel
"""


def test_a_punctuated_assembly_label_still_makes_two_names_one_function() -> None:
    assert parse_symbol_aliases(_PUNCTUATED_LABEL_AST, "leaf") == ("twin",)
    assert parse_symbol_aliases(_PUNCTUATED_LABEL_AST, "twin") == ("leaf",)


# The asymmetric case: only one side carries a label, and it spells the *default*
# symbol name of the other. A declaration's symbol is its label when it has one
# and its C name otherwise, so these two are one function with one side unmarked.
_DEFAULT_LABEL_AST = """\
TranslationUnitDecl 0xb000 <<invalid sloc>> <invalid sloc>
|-FunctionDecl 0xb001 </tmp/foo.c:1:1, col:42> col:13 used leaf 'void (int *)' static
| |-ParmVarDecl 0xb002 <col:15, col:20> col:20 used p 'int *'
| `-CompoundStmt 0xb003 <col:30, col:42>
|-FunctionDecl 0xb010 <line:2:1, col:35> col:6 used twin 'void (int *)'
| |-ParmVarDecl 0xb011 <col:15, col:20> col:20 p 'int *'
| `-AsmLabelAttr 0xb012 <col:32> "leaf" IsLiteralLabel
`-FunctionDecl 0xb020 <line:3:1, col:38> col:6 other 'void (int *)'
  |-ParmVarDecl 0xb021 <col:15, col:20> col:20 p 'int *'
  `-CompoundStmt 0xb022 <col:30, col:38>
"""


def test_an_assembly_label_matches_an_unlabelled_default_name() -> None:
    assert parse_symbol_aliases(_DEFAULT_LABEL_AST, "leaf") == ("twin",)
    assert parse_symbol_aliases(_DEFAULT_LABEL_AST, "twin") == ("leaf",)
    # and a TU where nothing is labelled stays free of phantom aliases: two
    # distinct C names are two distinct symbols
    assert parse_symbol_aliases(_DEFAULT_LABEL_AST, "other") == ()
    assert parse_symbol_aliases(_NESTED_DECL_AST, "leaf") == ()


def test_parse_address_escapes_keeps_a_reference_it_cannot_name() -> None:
    # A reference under no named declaration is still a reference; naming it
    # `<file scope>` keeps the escape (and the withheld discharge) rather than
    # dropping the one thing that says the caller set is open.
    assert parse_address_escapes(_ORPHAN_REFERENCE_AST, "leaf") == ("<file scope>",)


# Verified empirically against both esbmc's pinned clang and a current upstream
# clang (issue #167): a `GCCAsmStmt`'s only children are its constraint operand
# expressions (`result`, `x` below, for `asm("..." : "=a"(result) : "D"(x))`) —
# the assembly *string* itself, which could spell `call helper`, is never
# printed. `bare_asm` is the operand-free shape (a bare `asm("nop")`, a leaf
# node with no children at all). `FileScopeAsmDecl` is a different node kind
# that *does* print its string (`StringLiteral` child) and sits outside any
# function — not a call site, so it must never be mistaken for one. And
# `header_asm` lives in `/tmp/other.h`, not `_TARGET`, standing in for a header
# like `sys/io.h` whose port-I/O wrappers use inline asm for reasons unrelated
# to `_TARGET`'s own callers (measured at 18 `GCCAsmStmt` nodes for that one
# header) — the scan must stay scoped to `_TARGET` or every function in every
# TU that merely includes such a header would become permanently unresolved.
_ASM_AST = """\
TranslationUnitDecl 0x7000 <<invalid sloc>> <invalid sloc>
|-FunctionDecl 0x7001 </tmp/foo.c:1:1, col:38> col:6 used helper 'int (int)'
| |-ParmVarDecl 0x7002 <col:18, col:22> col:22 used x 'int'
| `-CompoundStmt 0x7003 <col:25, col:38>
|-FunctionDecl 0x7010 <line:2:1, line:6:1> line:2:5 caller 'int (int)'
| |-ParmVarDecl 0x7011 <col:12, col:16> col:16 used x 'int'
| `-CompoundStmt 0x7012 <col:19, line:6:1>
|   |-DeclStmt 0x7013 <line:3:5, col:15>
|   | `-VarDecl 0x7014 <col:5, col:9> col:9 used result 'int'
|   |-GCCAsmStmt 0x7015 <line:4:5, col:46>
|   | |-DeclRefExpr 0x7016 <col:30> 'int' lvalue Var 0x7014 'result' 'int'
|   | `-ImplicitCastExpr 0x7017 <col:44> 'int' <LValueToRValue>
|   |   `-DeclRefExpr 0x7018 <col:44> 'int' lvalue ParmVar 0x7011 'x' 'int'
|   `-ReturnStmt 0x7019 <line:5:5, col:12>
|     `-DeclRefExpr 0x701a <col:12> 'int' lvalue Var 0x7014 'result' 'int'
|-FunctionDecl 0x7020 <line:8:1, line:10:1> line:8:5 bare_asm 'int (void)'
| `-CompoundStmt 0x7021 <col:18, line:10:1>
|   |-GCCAsmStmt 0x7022 <line:9:5, col:14>
|   `-ReturnStmt 0x7023 <line:9:6, col:1>
|-FileScopeAsmDecl 0x7030 <line:12:1, col:24> col:1
| `-StringLiteral 0x7031 <col:5> 'char[18]' lvalue ".global my_symbol"
`-FunctionDecl 0x7040 </tmp/other.h:1:1, col:38> col:6 header_asm 'void (void)'
  `-CompoundStmt 0x7041 <col:20, col:38>
    `-GCCAsmStmt 0x7042 <col:22, col:36>
"""


def test_parse_asm_statements_reports_every_enclosing_declaration() -> None:
    # Neither reports which symbol, if any, is invoked — the whole reason both
    # `caller`'s operand-bearing asm and `bare_asm`'s bare one must be reported
    # unconditionally, since there is no substring anywhere in this dump to
    # narrow either down to a specific callee.
    assert parse_asm_statements(_ASM_AST, _TARGET) == ("caller", "bare_asm")


def test_parse_asm_statements_ignores_a_different_translation_unit_file() -> None:
    # `header_asm`'s asm sits in `/tmp/other.h`, a stand-in for a header whose
    # inline asm has nothing to do with `_TARGET`'s own callers.
    sites = parse_asm_statements(_ASM_AST, _TARGET)
    assert "header_asm" not in sites


def test_parse_asm_statements_ignores_file_scope_asm() -> None:
    # `FileScopeAsmDecl` prints a string and sits outside any function — it
    # defines/renames a symbol rather than calling one from within a body, so
    # it must never surface as a caller-set concern: the full result is exactly
    # `caller`/`bare_asm`, with no third site (named or `<file scope>`) for it.
    assert parse_asm_statements(_ASM_AST, _TARGET) == ("caller", "bare_asm")

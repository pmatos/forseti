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
    annotate_array_extents,
    find_definition_brace,
    list_units,
    mask_comments,
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

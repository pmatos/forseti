"""Tests for `forseti.esbmc.units` — listing function units from ESBMC's AST.

`parse_units` is tested purely against a captured-shape AST fixture (no ESBMC);
`list_units` has one ESBMC-gated end-to-end case.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from forseti.esbmc.units import Param, list_units, parse_units

# A clang textual AST in ESBMC's `--parse-tree-only` shape, exercising: an
# intrinsic in another file (excluded), a typedef, a scalar definition, a
# multi-param pointer definition, a prototype (no body → excluded), a definition
# in a *header* (wrong file → excluded), and a typedef'd function-pointer
# parameter printed as `'written':'canonical'` (must resolve to a pointer).
_AST = """\
TranslationUnitDecl 0x1000 <<invalid sloc>> <invalid sloc>
|-FunctionDecl 0x1001 <esbmc_intrinsics.h:1:1> col:6 assume 'void (_Bool)'
| `-ParmVarDecl 0x1002 <col:14, col:19> col:19 '_Bool'
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
|-FunctionDecl 0x100e <bar.h:1:1, col:22> col:6 hdrfn 'void (char *)'
| |-ParmVarDecl 0x100f <col:12, col:18> col:18 used s 'char *'
| `-CompoundStmt 0x1010 <col:20, col:22>
`-FunctionDecl 0x1011 <foo.c:6:1, col:27> col:6 reg 'void (cb_t)'
  |-ParmVarDecl 0x1012 <col:10, col:15> col:15 used cb 'cb_t':'void (*)(void)'
  `-CompoundStmt 0x1013 <col:20, col:27>
"""


def test_parse_units_definitions_only_in_target_file() -> None:
    units = {u.name: u for u in parse_units(_AST, "foo.c")}
    # __ESBMC_assume (intrinsics.h) and hdrfn (bar.h) are other files; proto has
    # no CompoundStmt → not a definition.
    assert set(units) == {"scal", "hash", "reg"}


def test_parse_units_pointer_classification() -> None:
    units = {u.name: u for u in parse_units(_AST, "foo.c")}
    assert units["scal"].takes_pointer is False
    assert units["hash"].takes_pointer is True  # const uint8_t *
    # the whole point of #131: a typedef'd function-pointer param resolves to a
    # pointer even though the written type `cb_t` shows no `*`.
    assert units["reg"].takes_pointer is True


def test_parse_units_param_types_are_canonical() -> None:
    reg = next(u for u in parse_units(_AST, "foo.c") if u.name == "reg")
    assert reg.params == (Param("cb", "void (*)(void)"),)
    hash_ = next(u for u in parse_units(_AST, "foo.c") if u.name == "hash")
    assert [p.type for p in hash_.params] == ["const uint8_t *", "unsigned long"]


def test_parse_units_empty_on_declarations_only() -> None:
    header = "TranslationUnitDecl 0x1 <<invalid sloc>>\n" + "\n".join(
        "|-FunctionDecl 0x2 <bar.h:1:1, col:20> col:6 decl 'void (int *)'"
        for _ in range(1)
    )
    assert parse_units(header, "bar.h") == []  # a prototype, no CompoundStmt


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

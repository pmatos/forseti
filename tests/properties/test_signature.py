"""Tests for the C-signature model + parser, extracted from ``harness.py``.

These pin the parsing concern in isolation from the harness *renderer*: the
happy paths, every fail-loud guard, the ``UnitSignature`` role queries, and --
the invariant that makes this a real seam rather than a file split -- that
``signature.py`` does not depend on ``harness.py`` (acyclicity).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from forseti.properties import signature as sig_mod
from forseti.properties.harness import extract_signature as harness_extract_signature
from forseti.properties.signature import (
    BufferParam,
    HarnessError,
    Param,
    ScalarParam,
    UnitSignature,
    extract_signature,
)

ABS_SLICE = "int64_t my_abs(int64_t x) { return (x < 0) ? -x : x; }"
ABS_SIG = UnitSignature("my_abs", "int64_t", (ScalarParam("int64_t", "x"),))


def test_extract_signature_scalar() -> None:
    assert extract_signature(ABS_SLICE, "my_abs") == ABS_SIG


def test_extract_signature_buffer_and_output() -> None:
    sig = extract_signature(
        "int utf8_decode(const unsigned char *b, unsigned len, uint32_t *cp)"
        " { return 0; }",
        "utf8_decode",
    )
    assert sig == UnitSignature(
        "utf8_decode",
        "int",
        (
            BufferParam("unsigned char", "b", "len", const=True, out=False),
            ScalarParam("unsigned", "len"),
            BufferParam("uint32_t", "cp", "1", const=False, out=True),
        ),
    )


def test_extract_signature_strips_storage_class() -> None:
    sig = extract_signature(
        "static uint32_t murmur3_32(const uint8_t *key, size_t len, uint32_t seed)"
        " { return 0; }",
        "murmur3_32",
    )
    assert sig == UnitSignature(
        "murmur3_32",
        "uint32_t",  # "static" dropped
        (
            BufferParam("uint8_t", "key", "len", const=True),
            ScalarParam("size_t", "len"),
            ScalarParam("uint32_t", "seed"),
        ),
    )


def test_extract_signature_explicit_array_length_is_a_buffer() -> None:
    # An array declarator ``[N]`` supplies the buffer's length directly and
    # exercises the top-level bracket depth tracking in the param splitter.
    sig = extract_signature("void fill(int a[10]) { }", "fill")
    assert sig == UnitSignature(
        "fill",
        "void",
        (BufferParam("int", "a", "10", const=False, out=False),),
    )


def test_extract_signature_void_param_list_has_no_params() -> None:
    sig = extract_signature("int zero(void) { return 0; }", "zero")
    assert sig == UnitSignature("zero", "int", ())


def test_extract_signature_missing_symbol_is_error() -> None:
    with pytest.raises(HarnessError):
        extract_signature(ABS_SLICE, "not_there")


def test_extract_signature_ambiguous_multibuffer_is_error() -> None:
    # An interior pointer not followed by its length (two buffers sharing a
    # trailing length) is ambiguous -- fail loud instead of inventing length 1.
    with pytest.raises(HarnessError):
        extract_signature(
            "int dot(const int *a, const int *b, size_t n) { return 0; }", "dot"
        )


def test_extract_signature_empty_return_type_is_error() -> None:
    # "static" is the only token before the symbol; stripping storage classes
    # leaves no return type.
    with pytest.raises(HarnessError):
        extract_signature("static f(int x) { }", "f")


def test_extract_signature_param_without_name_is_error() -> None:
    # An abstract declarator (type, no identifier) cannot be classified.
    with pytest.raises(HarnessError):
        extract_signature("void f(int *) { }", "f")


def test_extract_signature_param_without_type_is_error() -> None:
    # A bare identifier with no preceding type cannot be classified.
    with pytest.raises(HarnessError):
        extract_signature("int f(x) { }", "f")


def _out_sig() -> UnitSignature:
    return UnitSignature(
        "utf8_decode",
        "int",
        (
            BufferParam("unsigned char", "b", "len", const=True, out=False),
            ScalarParam("unsigned", "len"),
            BufferParam("uint32_t", "cp", "1", const=False, out=True),
        ),
    )


def test_param_names_is_every_parameter() -> None:
    assert _out_sig().param_names == frozenset({"b", "len", "cp"})


def test_output_param_names_are_the_out_buffers() -> None:
    assert _out_sig().output_param_names == frozenset({"cp"})


def test_input_param_names_are_the_complement_of_outputs() -> None:
    assert _out_sig().input_param_names == frozenset({"b", "len"})


def test_all_scalar_signature_has_no_output_params() -> None:
    assert ABS_SIG.output_param_names == frozenset()
    assert ABS_SIG.input_param_names == ABS_SIG.param_names


def test_param_is_the_sealed_union() -> None:
    assert Param == ScalarParam | BufferParam


def test_reexported_from_harness_is_the_same_object() -> None:
    # ``proposer.py`` and ``properties/__init__.py`` import these names from
    # ``harness``; the re-export must resolve to the module that now owns them.
    assert harness_extract_signature is extract_signature


def test_signature_module_is_a_leaf_within_the_package() -> None:
    # The seam's load-bearing invariant: parsing must not depend on rendering.
    # Asserting signature.py is a pure leaf -- it imports nothing from within
    # ``forseti`` (no relative imports, no absolute ``forseti.*``) -- is strictly
    # stronger than "does not name harness" and cannot be fooled by the word
    # appearing in prose/docstrings. Inspect the AST (order-independent) rather
    # than ``sys.modules``.
    tree = ast.parse(Path(sig_mod.__file__).read_text(encoding="utf-8"))
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0, f"relative import re-enters the package: {node!r}"
            modules.append(node.module or "")
        elif isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
    intra_package = [m for m in modules if m == "forseti" or m.startswith("forseti.")]
    assert not intra_package, intra_package

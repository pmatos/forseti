"""Tests for `forseti.esbmc.preprocessor` — the C preprocessor lexical scanner.

This module owns the comment/string masking primitives and the ``#line``/``#if``
directive scanner that `forseti.esbmc.units` used to hold inline. These tests
pin its *public contract* and the one invariant the extraction has to keep: the
module is a leaf, so it must never import back into `units` (which would make the
`units -> preprocessor` dependency a cycle). The exhaustive behaviour coverage
lives in `test_units.py`, which exercises the same functions through this module.
"""

from __future__ import annotations

import ast
from pathlib import Path

from forseti.esbmc import preprocessor as pp


def test_module_exposes_the_extracted_contract() -> None:
    for name in (
        "mask_comments",
        "mask_string_literals",
        "_stripped_for_scan",
        "_splice_continuations",
        "_strip_literal_parens",
        "_macro_test",
        "_macro_verdict",
        "_line_breakpoints",
        "_guard_macro_names",
        "_presumed_line",
    ):
        assert callable(getattr(pp, name)), name


def test_preprocessor_is_a_leaf_no_import_of_units() -> None:
    # The whole point of the seam: `units` depends on `preprocessor`, never the
    # reverse. A substring check would false-match the docstring prose that names
    # `units`, so walk the AST and inspect only real import statements.
    source = Path(pp.__file__).read_text()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert "units" not in alias.name.split("."), alias.name
        elif isinstance(node, ast.ImportFrom):
            assert node.module is None or "units" not in node.module.split(".")


def test_mask_comments_blanks_preserving_offsets_and_newlines() -> None:
    src = "int a; // c\nint /* x\ny */ b;\n"
    masked = pp.mask_comments(src)
    assert len(masked) == len(src)
    assert masked.count("\n") == src.count("\n")
    assert "//" not in masked and "/*" not in masked
    assert masked.startswith("int a; ")


def test_stripped_for_scan_blanks_comments_and_literals() -> None:
    src = 'char *s = "foo(bar)"; // note\n'
    stripped = pp._stripped_for_scan(src)
    assert len(stripped) == len(src)
    assert "foo(bar)" not in stripped
    assert "note" not in stripped
    assert stripped.startswith("char *s = ")


def test_strip_literal_parens_peels_only_whole_span() -> None:
    assert pp._strip_literal_parens("((0))") == "0"
    assert pp._strip_literal_parens("(0 || FEATURE)") == "0 || FEATURE"
    assert pp._strip_literal_parens("(0) || FEATURE") == "(0) || FEATURE"


def test_splice_continuations_joins_backslash_newline() -> None:
    spliced, physical_line_of = pp._splice_continuations("#ifdef W\\\nIDE\nx\n")
    assert spliced.splitlines()[0] == "#ifdef WIDE"
    # The logical line still names the physical line it *starts* on.
    assert physical_line_of[0] == 1


def test_line_breakpoints_records_line_directive() -> None:
    assert pp._line_breakpoints("#line 100\n") == [(1, 100)]


def test_line_breakpoints_excludes_dead_if_zero_branch() -> None:
    src = "#if 0\n#line 50\n#endif\n#line 200\n"
    assert pp._line_breakpoints(src) == [(4, 200)]


def test_line_breakpoints_resolves_ifdef_from_predefined_seed() -> None:
    src = "#ifdef FOO\n#line 50\n#endif\n"
    assert pp._line_breakpoints(src, predefined=[("FOO", True)]) == [(2, 50)]
    assert pp._line_breakpoints(src, predefined=[("FOO", False)]) == []


def test_macro_verdict_opaque_without_a_predicate() -> None:
    # Documented contract: "no match" -> "op". `_line_breakpoints` never hands it
    # a None match, so this is the only place the guard is exercised directly.
    assert pp._macro_verdict(None, {}) == "op"
    no_name = pp._IFDEF_RE.match("#ifdef\n")
    assert no_name is not None
    assert pp._macro_verdict(no_name, {}) == "op"
    known = pp._IFDEF_RE.match("#ifdef FOO\n")
    assert known is not None
    assert pp._macro_verdict(known, {"FOO": True}) == "1"
    assert pp._macro_verdict(known, {"FOO": False}) == "0"
    # Name whose definedness is unproven stays opaque.
    assert pp._macro_verdict(known, {}) == "op"


def test_guard_macro_names_collects_defined_tests() -> None:
    names = pp._guard_macro_names("#ifdef FOO\n#if defined(BAR)\nx\n#endif\n#endif\n")
    assert set(names) == {"FOO", "BAR"}


def test_presumed_line_offsets_from_breakpoint() -> None:
    breakpoints = [(1, 100)]
    assert pp._presumed_line(2, breakpoints) == 100
    assert pp._presumed_line(3, breakpoints) == 101
    # No directive at or before the line: presumed == physical.
    assert pp._presumed_line(1, breakpoints) == 1

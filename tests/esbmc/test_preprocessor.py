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

import pytest

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


def test_guard_order_and_line_walk_share_definedness_grammar() -> None:
    source = (
        "#if defined(EARLY)\n#line 10\n"
        "#elif !defined(ALT)\n#line 20\n#endif\n"
        "#ifndef LATE\n#line 30\n#endif\n"
        "#if 0\n#elifdef MID\n#line 40\n#endif\n"
        "#ifdef EARLY\n#line 50\n#endif\n"
    )
    # Probe names keep their legacy spelling-group order, not source order.
    assert pp._guard_macro_names(source) == ("LATE", "EARLY", "MID", "ALT")
    assert pp._line_breakpoints(
        source,
        predefined=(("EARLY", False), ("ALT", False), ("LATE", False), ("MID", True)),
    ) == [(4, 20), (7, 30), (11, 40)]


@pytest.mark.parametrize("premasked", [False, True])
@pytest.mark.parametrize("defined, expected", [(True, [(8, 11)]), (False, [(11, 22)])])
def test_both_projections_read_spliced_text_with_original_physical_lines(
    premasked: bool, defined: bool, expected: list[tuple[int, int]]
) -> None:
    source = (
        "/\\\n* #ifdef HIDDEN\n#line 900\n*/\n"
        "#ifde\\\nf W\\\nIDE\n#line \\\n11\n#else\n#line 22\n#endif\n"
    )
    if premasked:
        source = pp._stripped_for_scan(source)
    assert pp._guard_macro_names(source) == ("WIDE",)
    assert pp._line_breakpoints(source, predefined=(("WIDE", defined),)) == expected


@pytest.mark.parametrize(
    "directive, kind, names, verdict",
    [
        ("#if ((0))", "if0", (), None),
        ("#elif (1)", "elif1", (), None),
        ("#if 0 || FEATURE", "ifop", (), None),
        ("#elif defined(GUARD) && OTHER", "elifop", (), None),
        ("#if !(defined(GUARD))", "ifop", (), None),
        ("#ifdef GUARD", "ifdef", ("GUARD",), "1"),
        ("#ifndef GUARD", "ifdef", ("GUARD",), "0"),
        ("#elifdef GUARD", "elifdef", ("GUARD",), "1"),
        ("#elifndef GUARD", "elifdef", ("GUARD",), "0"),
        ("#if ((defined GUARD))", "ifdef", ("GUARD",), "1"),
        ("#elif !defined(GUARD)", "elifdef", ("GUARD",), "0"),
        ("%:\vif!defined(GUARD)", "ifdef", ("GUARD",), "0"),
        ("#ifdef", "ifdef", (), "op"),
        ("#elifndef", "elifdef", (), "op"),
    ],
)
def test_directive_reading_classifies_without_resolving_macro_state(
    directive: str, kind: str, names: tuple[str, ...], verdict: str | None
) -> None:
    reading = pp._read_directives(f"{directive}\n#line 50\n#endif\n")
    assert reading.guard_names == names
    assert [(line, event_kind) for line, event_kind, _ in reading.events] == [
        (1, kind),
        (2, "line"),
        (3, "endif"),
    ]
    if verdict is not None:
        # A defined test stays unresolved until the walker supplies state.
        predicate = reading.events[0][2]
        assert pp._macro_verdict(predicate, {"GUARD": True}) == verdict
        assert pp._macro_verdict(predicate, {}) == "op"


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_directive_reading_keeps_event_order_within_a_spliced_line(
    newline: str,
) -> None:
    source = '#define RESTORE \\\n_Pragma("pop_macro")\n#line RESTORE\n'
    reading = pp._read_directives(source.replace("\n", newline))
    assert reading.guard_names == ()
    assert [(line, kind) for line, kind, _ in reading.events] == [
        (1, "define"),
        (1, "pragma_operator"),
        (3, "line_macro"),
    ]


def test_directive_reading_ignores_comments_and_spliced_literals() -> None:
    source = (
        "/\\\n*\n#ifdef COMMENT\n*/\n"
        'char *s = "\\\n#ifdef STRING";\n'
        "// #if defined(LINE_COMMENT)\n"
    )
    reading = pp._read_directives(source)
    assert reading.guard_names == ()
    assert reading.events == ()

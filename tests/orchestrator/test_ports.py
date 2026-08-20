"""`Unit.from_path`'s main-free contract — no esbmc, no network.

`Unit`'s own docstring promises `source_text` is a *main-free kernel slice*
(issue #95 review: a normal executable translation unit that also defines
`main` previously made `render_semantic_harness` reject every stored property
as an unconditional `ERROR`, since the generated harness defines its own
`main`). These test `from_path`'s rename-away-the-collision behavior directly,
without needing a real ESBMC run.
"""

from __future__ import annotations

from pathlib import Path

from forseti.orchestrator import Unit


def test_from_path_renames_colliding_main_definition(tmp_path: Path) -> None:
    source = tmp_path / "prog.c"
    source.write_text(
        "int helper(int x) {\n    return x + 1;\n}\n\n"
        "int main(void) {\n    return helper(1);\n}\n"
    )

    unit = Unit.from_path(source, "helper")

    assert "int helper(int x)" in unit.source_text
    assert "return x + 1;" in unit.source_text
    assert "int main(void)" not in unit.source_text
    assert "__forseti_unused_main(void)" in unit.source_text
    # The call inside the renamed function's own body is untouched.
    assert "return helper(1);" in unit.source_text


def test_from_path_renames_the_real_main_not_a_trailing_comment(
    tmp_path: Path,
) -> None:
    """A comment mentioning `main` right before the brace must not be the
    "last `main` before the brace" match -- that would rename comment text
    (a no-op) and leave the actual identifier defining `main` untouched,
    degrading back to the pre-fix ERROR."""
    source = tmp_path / "prog.c"
    source.write_text(
        "int helper(int x) {\n    return x + 1;\n}\n\n"
        "int main /* main */ (void) {\n    return helper(1);\n}\n"
    )

    unit = Unit.from_path(source, "helper")

    assert "int __forseti_unused_main /* main */ (void)" in unit.source_text


def test_from_path_leaves_main_free_source_unchanged(tmp_path: Path) -> None:
    source = tmp_path / "kernel.c"
    text = "int helper(int x) {\n    return x + 1;\n}\n"
    source.write_text(text)

    unit = Unit.from_path(source, "helper")

    assert unit.source_text == text


def test_from_path_renames_a_mismatched_main_prototype_too(tmp_path: Path) -> None:
    """A bare declaration (no body) is not *definition*-shaped, but a
    signature mismatch between it and whatever the definition gets renamed to
    is a hard esbmc parse error ("conflicting types"), not merely a Python-
    level `ERROR` verdict -- verified against a live esbmc run (issue #95
    review). `from_path` renames the prototype too, not just the definition,
    so no `main`-named declaration of any signature is left behind. A
    `main`-like substring (not a `\\b`-bounded match) is still untouched."""
    source = tmp_path / "decl.c"
    text = (
        "void main(void);  // forward-declared elsewhere, mismatched on purpose\n\n"
        "int helper(int x) {\n    return x + 1;\n}\n\n"
        "int mainframe(int x) {\n    return x;\n}\n\n"
        "int main(void) {\n    return helper(1);\n}\n"
    )
    source.write_text(text)

    unit = Unit.from_path(source, "helper")

    assert "void main(void);" not in unit.source_text
    assert "int main(void) {" not in unit.source_text
    assert unit.source_text.count("__forseti_unused_main(void)") == 2
    assert "int mainframe(int x)" in unit.source_text  # untouched substring


def test_from_path_leaves_a_main_mention_alone(tmp_path: Path) -> None:
    """A comment mention (not code) is neither declaration- nor
    definition-shaped -- nothing is renamed."""
    source = tmp_path / "decl.c"
    text = (
        "/* main() is mentioned here too */\n\n"
        "int helper(int x) {\n    return x + 1;\n}\n"
    )
    source.write_text(text)

    unit = Unit.from_path(source, "helper")

    assert unit.source_text == text


def test_from_path_renames_every_colliding_main_alternative(tmp_path: Path) -> None:
    """An inactive `#if 0` alternative ahead of the real definition is just as
    definition-shaped to this textual scan as the compiled one — leaving it
    untouched would still collide with the harness's own generated `main`
    (issue #95 review)."""
    source = tmp_path / "prog.c"
    source.write_text(
        "#if 0\nint main(void) { return -1; }\n#endif\n"
        "int helper(int x) {\n    return x + 1;\n}\n\n"
        "int main(void) {\n    return helper(1);\n}\n"
    )

    unit = Unit.from_path(source, "helper")

    assert unit.source_text.count("int __forseti_unused_main(void)") == 2
    assert "int main(void)" not in unit.source_text


def test_from_path_checking_main_itself_tracks_the_renamed_symbol(
    tmp_path: Path,
) -> None:
    """Checking `main`'s own semantic properties is a legitimate request —
    `from_path` still renames `main` away (the generated harness needs the
    name for its own entry point either way), but `Unit.symbol` must track
    the rename so `SemanticHarnessWriter` looks up the identifier that is
    actually still present, not the now-absent `"main"` (issue #95 review:
    without this, checking `main` itself made every property an unconditional
    `ERROR`, "no such symbol")."""
    source = tmp_path / "prog.c"
    source.write_text("int main(void) {\n    return 0;\n}\n")

    unit = Unit.from_path(source, "main")

    assert unit.unit_id == f"{source}::main"  # store lookup key is unchanged
    assert unit.symbol == "__forseti_unused_main"
    assert "int __forseti_unused_main(void)" in unit.source_text

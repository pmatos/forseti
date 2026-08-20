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


def test_from_path_leaves_main_free_source_unchanged(tmp_path: Path) -> None:
    source = tmp_path / "kernel.c"
    text = "int helper(int x) {\n    return x + 1;\n}\n"
    source.write_text(text)

    unit = Unit.from_path(source, "helper")

    assert unit.source_text == text


def test_from_path_leaves_main_prototype_and_mentions_alone(tmp_path: Path) -> None:
    """A bare declaration (no body) or a `main`-like substring is not a
    definition-shaped occurrence — `find_definition_brace` returns `None` for
    it, so nothing is renamed."""
    source = tmp_path / "decl.c"
    text = (
        "void main(void);  // forward-declared elsewhere, not defined here\n\n"
        "int helper(int x) {\n    return x + 1;\n}\n\n"
        "int mainframe(int x) {\n    return x;\n}\n"
    )
    source.write_text(text)

    unit = Unit.from_path(source, "helper")

    assert unit.source_text == text

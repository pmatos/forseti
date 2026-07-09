"""Tests for the versioned proposer prompt artifact and its sentinel renderer."""

from __future__ import annotations

from forseti.properties import (
    DEFAULT_PROMPT,
    PROMPTS,
    RESULT_IDENT,
    SEMANTIC_V1,
    render_prompt,
)


def test_default_is_semantic_v1_and_registered() -> None:
    assert DEFAULT_PROMPT is SEMANTIC_V1
    assert DEFAULT_PROMPT.ref == "semantic/v1"
    assert PROMPTS[DEFAULT_PROMPT.ref] is SEMANTIC_V1
    assert DEFAULT_PROMPT.prompt_id == "semantic"
    assert DEFAULT_PROMPT.version == "1"


def test_render_fills_every_sentinel() -> None:
    rendered = render_prompt(
        SEMANTIC_V1,
        unit_id="examples/abs.c::my_abs",
        symbol="my_abs",
        source_text="int64_t my_abs(int64_t x) { return x; }",
        max_candidates=4,
    )
    # No sentinel token survives substitution.
    assert "<<" not in rendered and ">>" not in rendered
    assert "examples/abs.c::my_abs" in rendered
    assert "my_abs" in rendered
    assert RESULT_IDENT in rendered
    assert "up to 4" in rendered


def test_render_preserves_c_source_verbatim() -> None:
    # C source is full of {, }, $ and % -- str.format/Template would choke; the
    # sentinel scheme must pass them through untouched.
    source = "int f(int a) { return a % 2 == 0 ? ${weird} : {0}; }"
    rendered = render_prompt(
        SEMANTIC_V1,
        unit_id="u.c::f",
        symbol="f",
        source_text=source,
    )
    assert source in rendered


def test_render_uses_custom_result_ident() -> None:
    rendered = render_prompt(
        SEMANTIC_V1,
        unit_id="u.c::f",
        symbol="f",
        source_text="int f(void);",
        result_ident="__ret",
    )
    assert "__ret" in rendered

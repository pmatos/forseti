"""Proposer input recipes (#388): context shape, determinism, and leakage."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from forseti.esbmc import find_definition_brace, mask_comments
from forseti.grading.body import locate_body
from forseti.properties import RECIPES, RecipeError, build_context

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"

TARGETS = {
    "abs.c": "my_abs",
    "abs_fixed.c": "my_abs",
    "frame_checksum.c": "sum_bytes",
    "frame_checksum_bug.c": "sum_bytes",
    "merge_sort.c": "merge",
    "merge_sort_bug.c": "merge",
    "murmurhash.c": "murmur3_32",
    "murmurhash_bug.c": "murmur3_32",
    "ring_buffer.c": "rb_push",
    "ring_buffer_bug.c": "rb_push",
    "sha1.c": "sha1_update",
    "sha1_bug.c": "sha1_update",
    "utf8_decode.c": "utf8_decode",
    "utf8_decode_bug.c": "utf8_decode",
}

SOURCE = """\
/* File header: names the staged bug. */
#include <stddef.h>
#define LIMIT 8
#define DOUBLE_LIMIT (LIMIT * 2)
#define UNUSED 99

typedef struct {
    int items[DOUBLE_LIMIT];
    size_t n;
} bag_t;

enum mode { MODE_A, MODE_B = 4 };
typedef int (*visit_fn)(int);
struct other { int z; };

/* Add one item.
 * Returns 0 when full. */
int bag_add(bag_t *b, int v, enum mode m) {
    if (b->n == DOUBLE_LIMIT) return 0;
    b->items[b->n++] = v + (int)m;
    return 1;
}

static int helper(int x) { return x + MODE_B; }

int main(void) {
    bag_t b = {0};
    return bag_add(&b, 1, MODE_A);
}
"""


def _ctx(recipe: str, source: str, symbol: str, root: Path = Path()) -> str:
    return build_context(
        recipe, unit_id=f"u.c::{symbol}", source_text=source, project_root=root
    ).text


def _code_words(text: str) -> set[str]:
    return set(re.findall(r"\w+", mask_comments(text)))


def test_registry_has_the_three_single_file_recipes() -> None:
    assert set(RECIPES) == {"baseline", "sighted", "blind"}
    assert RECIPES["blind"].ref == "blind/v1"
    ctx = build_context(
        "blind", unit_id="u.c::bag_add", source_text=SOURCE, project_root=Path()
    )
    assert ctx.recipe == "blind/v1"


def test_unknown_recipe_is_an_error() -> None:
    with pytest.raises(RecipeError, match="unknown recipe 'nope'"):
        _ctx("nope", SOURCE, "bag_add")


@pytest.mark.parametrize("recipe", ["sighted", "blind"])
def test_missing_target_is_an_error(recipe: str) -> None:
    with pytest.raises(RecipeError, match="no definition of 'absent'"):
        _ctx(recipe, SOURCE, "absent")


@pytest.mark.parametrize("recipe", sorted(RECIPES))
def test_same_input_always_yields_the_same_context(recipe: str, tmp_path: Path) -> None:
    first = _ctx(recipe, SOURCE, "bag_add")
    assert _ctx(recipe, SOURCE, "bag_add") == first
    assert _ctx(recipe, SOURCE, "bag_add", root=tmp_path) == first


@pytest.mark.parametrize(("name", "symbol"), sorted(TARGETS.items()))
def test_baseline_of_an_example_has_no_main_and_no_header(
    name: str, symbol: str
) -> None:
    source = (EXAMPLES / name).read_text()
    header = source[: source.index("*/") + 2]

    text = _ctx("baseline", source, symbol)

    assert find_definition_brace(text, "main") is None
    assert "main" not in _code_words(text)
    assert not text.lstrip().startswith(("/*", "//"))
    assert header not in text
    assert find_definition_brace(text, symbol) is not None


def test_examples_with_a_main_exercise_its_removal() -> None:
    with_main = [
        name
        for name in TARGETS
        if find_definition_brace((EXAMPLES / name).read_text(), "main") is not None
    ]
    assert len(with_main) >= 8


def test_baseline_keeps_everything_else_verbatim() -> None:
    text = _ctx("baseline", SOURCE, "bag_add")
    assert "File header" not in text
    assert "int main" not in text
    assert "bag_main" not in text
    for kept in ("#include <stddef.h>", "UNUSED", "static int helper", "/* Add one"):
        assert kept in text


def test_baseline_keeps_a_header_comment_that_is_the_targets_doc() -> None:
    source = "/* Doubles x. */\nint dbl(int x) { return 2 * x; }\n"
    assert _ctx("baseline", source, "dbl") == source


def test_baseline_of_main_keeps_main() -> None:
    assert "int main" in _ctx("baseline", SOURCE, "main")


@pytest.mark.parametrize(("name", "symbol"), sorted(TARGETS.items()))
def test_blind_context_holds_no_statement_of_the_target_body(
    name: str, symbol: str
) -> None:
    source = (EXAMPLES / name).read_text()
    body = locate_body(source, symbol)
    body_text = source[body.open_brace + 1 : body.close_brace]
    statements = {
        s.strip()
        for line in mask_comments(body_text).splitlines()
        for s in line.split(";")
        if len(s.strip()) > 5
    }
    assert statements

    text = _ctx("blind", source, symbol)

    for statement in statements:
        assert statement not in text
    assert find_definition_brace(text, symbol) is None
    assert re.search(rf"\b{symbol}\s*\([^)]*\)\s*;", text)


def test_blind_keeps_signature_doc_comment_and_signature_types_only() -> None:
    text = _ctx("blind", SOURCE, "bag_add")
    assert text == (
        "#define LIMIT 8\n"
        "#define DOUBLE_LIMIT (LIMIT * 2)\n"
        "typedef struct {\n"
        "    int items[DOUBLE_LIMIT];\n"
        "    size_t n;\n"
        "} bag_t;\n"
        "enum mode { MODE_A, MODE_B = 4 };\n"
        "\n"
        "/* Add one item.\n"
        " * Returns 0 when full. */\n"
        "int bag_add(bag_t *b, int v, enum mode m);\n"
    )


def test_sighted_adds_the_body_and_the_definitions_it_uses() -> None:
    text = _ctx("sighted", SOURCE, "bag_add")
    assert text.endswith(
        "/* Add one item.\n"
        " * Returns 0 when full. */\n"
        "int bag_add(bag_t *b, int v, enum mode m) {\n"
        "    if (b->n == DOUBLE_LIMIT) return 0;\n"
        "    b->items[b->n++] = v + (int)m;\n"
        "    return 1;\n"
        "}\n"
    )
    for used in ("#define LIMIT 8", "DOUBLE_LIMIT (LIMIT * 2)", "} bag_t;"):
        assert used in text
    for unused in ("UNUSED", "visit_fn", "struct other", "helper", "main", "File"):
        assert unused not in text


def test_sighted_pulls_a_macro_used_only_in_the_body() -> None:
    source = "#define K 3\n#define L 4\nint f(int x) { return x + K; }\n"
    assert _ctx("sighted", source, "f") == (
        "#define K 3\n\nint f(int x) { return x + K; }\n"
    )
    assert "#define K" not in _ctx("blind", source, "f")


def test_enumerator_use_selects_its_enum() -> None:
    source = "enum e { E_A, E_B };\nenum g { G_A };\nint f(void) { return E_B; }\n"
    text = _ctx("sighted", source, "f")
    assert "enum e" in text
    assert "enum g" not in text


def test_function_pointer_typedef_is_selected_by_its_name() -> None:
    source = "typedef int (*cb_t)(int);\nint f(cb_t g) { return g(1); }\n"
    assert "typedef int (*cb_t)(int);" in _ctx("blind", source, "f")


def test_adjacent_line_comments_form_one_doc_comment() -> None:
    source = "// one\n// two\nint f(void) { return 0; }\n"
    assert _ctx("blind", source, "f") == "// one\n// two\nint f(void);\n"


def test_a_comment_separated_by_code_is_not_the_doc_comment() -> None:
    source = "/* other */\nint g(void);\nint f(void) { return 0; }\n"
    assert _ctx("sighted", source, "f") == "int f(void) { return 0; }\n"


def test_a_header_comment_behind_a_blank_line_is_not_the_doc_comment() -> None:
    source = "/* BUG header */\n\nint f(int a) { return a; }\n"
    assert _ctx("blind", source, "f") == "int f(int a);\n"
    assert _ctx("sighted", source, "f") == "int f(int a) { return a; }\n"
    assert _ctx("baseline", source, "f") == "int f(int a) { return a; }\n"


def test_a_trailing_comment_on_the_previous_statement_is_not_the_doc_comment() -> None:
    source = "int x; // note\nint f(void) { return 0; }\n"
    assert _ctx("blind", source, "f") == "int f(void);\n"


def test_baseline_strips_every_leading_comment_line_but_keeps_the_doc() -> None:
    source = (
        "// line1 BUG\n// line2 leaks\n\n/* more */\n#include <x.h>\n\n"
        "// doc\nint f(void) { return 0; }\n"
    )
    assert _ctx("baseline", source, "f") == (
        "#include <x.h>\n\n// doc\nint f(void) { return 0; }\n"
    )


def test_every_declarator_of_a_typedef_is_a_name() -> None:
    source = "typedef struct n { int a; } n_t, *n_p;\nint f(n_t *p) { return p->a; }\n"
    assert "typedef struct n" in _ctx("blind", source, "f")
    source = "typedef unsigned a_t, b_t;\nint f(b_t x) { return x; }\n"
    assert "typedef unsigned a_t, b_t;" in _ctx("blind", source, "f")


def test_a_semicolonless_macro_invocation_is_not_part_of_the_next_function() -> None:
    source = "DECLARE_THING(x)\nint f(int a) { return a; }\n"
    assert _ctx("sighted", source, "f") == "int f(int a) { return a; }\n"


def test_an_initialiser_ending_in_a_paren_is_not_a_function() -> None:
    source = "static foo_t v = (foo_t){1};\nint f(int a) { return a; }\n"
    assert _ctx("blind", source, "f") == "int f(int a);\n"
    assert _ctx("sighted", source, "f") == "int f(int a) { return a; }\n"


def test_attributes_after_the_parameter_list_stay_with_the_function() -> None:
    source = "int f(int a) __attribute__((pure)) { return a; }\n"
    assert _ctx("sighted", source, "f") == source

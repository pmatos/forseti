"""One preparation supplies semantic C, its vacuity twin, and verdict scope."""

from __future__ import annotations

import pytest

from forseti.properties.harness import (
    PropertyHarness,
    SemanticSpec,
    _prepare_semantic_harness,
)
from forseti.properties.signature import (
    BufferParam,
    ScalarParam,
    StructParam,
    UnitSignature,
)


@pytest.mark.parametrize("returns_void", [False, True])
def test_preparation_renders_a_single_variant_or_the_complete_pair(
    returns_void: bool,
) -> None:
    ctype = "void" if returns_void else "int"
    source = f"{ctype} f(int x) {{ {'(void)x;' if returns_void else 'return x;'} }}"
    expression = "x >= 0" if returns_void else "result == x"
    prepared = _prepare_semantic_harness(
        unit_source=source,
        signature=UnitSignature("f", ctype, (ScalarParam("int", "x"),)),
        spec=SemanticSpec(expression, (" x >= 0 ", "")),
        includes=("limits.h",),
    )
    call = "    f(x);" if returns_void else "    int result = f(x);"
    assertion = f'    __ESBMC_assert(({expression}), "forseti:semantic");'
    expected = (
        "#include <limits.h>\n\n"
        f"{source}\n\n"
        "int nondet_int(void);\n\n"
        "int main(void) {\n"
        "    int x = nondet_int();\n"
        "    __ESBMC_assume((x >= 0));\n"
        f"{call}\n"
        f"{assertion}\n"
        "    return 0;\n"
        "}\n"
    )
    probe = expected.replace(assertion, '    __ESBMC_assert(0, "forseti:non-vacuity");')
    assert prepared.render() == expected
    assert prepared.render(probe=True) == probe
    assert prepared.build() == PropertyHarness(
        source_text=expected, probe_source_text=probe
    )


@pytest.mark.parametrize("floor", [1, 16])
def test_preparation_keeps_struct_domains_before_buffers_and_scope_with_caps(
    floor: int,
) -> None:
    signature = UnitSignature(
        "consume",
        "void",
        (
            StructParam("box_t", "box", "1"),
            BufferParam("int", "a", "n", const=True),
            ScalarParam("unsigned", "n"),
        ),
    )
    prepared = _prepare_semantic_harness(
        unit_source="void consume(box_t *box, const int *a, unsigned n) {}",
        signature=signature,
        spec=SemanticSpec(
            "box->count >= 0", ("box->count <= n", f"n >= {floor}", "a[n-1] >= 0")
        ),
        max_len=8,
    )
    built = prepared.build()
    assert built.length_bounds == ((("n", 8),) if floor <= 8 else ())
    assert built.assumption is not None
    assert "box (box_t) start as nondet struct values" in built.assumption
    assert built.probe_source_text is not None
    for text in (built.source_text, built.probe_source_text):
        assert text.index("box_t *box = &box__obj;") < text.index(
            "__ESBMC_assume((box->count <= n));"
        )
        allocation = text.index("int *a = malloc((n) * sizeof(int));")
        assert text.index(f"__ESBMC_assume((n >= {floor}));") < allocation
        cap = "__ESBMC_assume((n >= 0 && n <= 8));"
        if floor <= 8:
            assert text.index(cap) < allocation
        else:
            assert cap not in text
        assert allocation < text.index("__ESBMC_assume((a[n-1] >= 0));")
        assert text.index("consume(box, a, n);") < text.index("free(a);")
        assert "free(box)" not in text
    assert prepared.render(probe=True) == built.probe_source_text
    assert prepared.render() == built.source_text

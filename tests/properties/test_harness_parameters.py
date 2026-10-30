"""Parameter storage and its ordering through both property renderers."""

from __future__ import annotations

import pytest

from forseti.properties import (
    BufferParam,
    HarnessError,
    InvariantRole,
    ScalarParam,
    SemanticSpec,
    StructParam,
    UnitSignature,
    render_invariant_harness,
    render_semantic_harness,
)
from forseti.properties._harness_parameters import prepare_parameters

_SOURCE = (
    "typedef struct { int value; } box_t;\n"
    "void touch(box_t *box, const int *data, unsigned n, int *cell, int *out) "
    "{ *cell = 0; }"
)
_SIGNATURE = UnitSignature(
    "touch",
    "void",
    (
        StructParam("box_t", "box", "1"),
        BufferParam("int", "data", "n", const=True),
        ScalarParam("unsigned", "n"),
        BufferParam("int", "cell", "1", out=True),
        BufferParam("int", "out", "n", out=True),
    ),
)
_INCLUDES = ("stddef.h", "stdint.h")
_PROTOTYPES = (
    "box_t nondet_box_t(void);\n"
    "int nondet_int(void);\n"
    "unsigned nondet_unsigned(void);\n"
)
_BUFFER_SETUP = """\
    __ESBMC_assume((n) <= SIZE_MAX / sizeof(int));
    int *data = malloc((n) * sizeof(int));
    __ESBMC_assume(data != NULL);
    for (size_t _i = 0; _i < (n); _i++) data[_i] = nondet_int();
    int cell;
    __ESBMC_assume((n) <= SIZE_MAX / sizeof(int));
    int *out = malloc((n) * sizeof(int));
    __ESBMC_assume(out != NULL);
"""
_BOX_OBJECT = """\
    box_t box__obj = nondet_box_t();
    box_t *box = &box__obj;
"""
_CLEANUP = "    free(data);\n    free(out);\n"
_CAP = "    __ESBMC_assume((n >= 0 && n <= 3));\n"
_PROBE = '__ESBMC_assert(0, "forseti:non-vacuity");'


def _preamble(includes: tuple[str, ...]) -> str:
    headers = "".join(f"#include <{header}>\n" for header in includes)
    if "stdlib.h" not in includes:
        headers += "#include <stdlib.h>\n"
    return headers + "\n" + _SOURCE + "\n\n" + _PROTOTYPES


@pytest.mark.parametrize("probe", [False, True])
@pytest.mark.parametrize("includes", [_INCLUDES, (*_INCLUDES, "stdlib.h")])
def test_semantic_parameter_output_is_preserved(
    probe: bool, includes: tuple[str, ...]
) -> None:
    assertion = _PROBE if probe else '__ESBMC_assert((cell == 0), "forseti:semantic");'
    expected = (
        _preamble(includes)
        + "\nint main(void) {\n"
        + "    unsigned n = nondet_unsigned();\n"
        + _BOX_OBJECT
        + "    __ESBMC_assume((n > 0));\n"
        + _CAP
        + _BUFFER_SETUP
        + "    __ESBMC_assume((data[0] >= 0));\n"
        + "    touch(box, data, n, &cell, out);\n"
        + f"    {assertion}\n"
        + _CLEANUP
        + "    return 0;\n}\n"
    )
    assert (
        render_semantic_harness(
            unit_source=_SOURCE,
            signature=_SIGNATURE,
            spec=SemanticSpec("cell == 0", ("n > 0", "data[0] >= 0")),
            includes=includes,
            max_len=3,
            probe=probe,
        )
        == expected
    )


@pytest.mark.parametrize("role", list(InvariantRole))
@pytest.mark.parametrize("probe", [False, True])
@pytest.mark.parametrize("includes", [_INCLUDES, (*_INCLUDES, "stdlib.h")])
def test_invariant_parameter_output_is_preserved(
    role: InvariantRole, probe: bool, includes: tuple[str, ...]
) -> None:
    assertion = (
        _PROBE
        if probe
        else '__ESBMC_assert(__forseti_inv(&box), "forseti:rep-invariant");'
    )
    entry = (
        "    __ESBMC_assume(__forseti_inv(&box));\n"
        if role is InvariantRole.METHOD
        else ""
    )
    expected = (
        _preamble(includes)
        + "static int __forseti_inv(const box_t *self) {\n"
        + "    return (self->value >= 0);\n}\n\n"
        + f"/* forseti: representation invariant, role={role.value} */\n"
        + "int main(void) {\n"
        + "    unsigned n = nondet_unsigned();\n"
        + "    box_t box = nondet_box_t();\n"
        + entry
        + _CAP
        + _BUFFER_SETUP
        + "    touch(&box, data, n, &cell, out);\n"
        + f"    {assertion}\n"
        + _CLEANUP
        + "    return 0;\n}\n"
    )
    assert (
        render_invariant_harness(
            unit_source=_SOURCE,
            signature=_SIGNATURE,
            type_name="box_t",
            expression="self->value >= 0",
            role=role,
            max_len=3,
            probe=probe,
            includes=includes,
        )
        == expected
    )


@pytest.mark.parametrize("struct_type", [None, "box_t"])
def test_preparation_owns_mixed_parameter_storage(struct_type: str | None) -> None:
    prepared = prepare_parameters(
        _SIGNATURE.params, includes=_INCLUDES, struct_type=struct_type
    )
    assert prepared.headers == (
        "#include <stddef.h>",
        "#include <stdint.h>",
        "#include <stdlib.h>",
    )
    assert prepared.prototypes == tuple(_PROTOTYPES.splitlines())
    assert prepared.scalar_declarations == ("    unsigned n = nondet_unsigned();",)
    if struct_type is None:
        assert prepared.struct_declarations == (
            (_SIGNATURE.params[0], tuple(_BOX_OBJECT.splitlines())),
        )
        assert prepared.buffer_setup == tuple(_BUFFER_SETUP.splitlines())
        assert prepared.arguments == "box, data, n, &cell, out"
        assert prepared.cleanup == tuple(_CLEANUP.splitlines())
    else:
        assert prepared.struct_declarations == (
            (_SIGNATURE.params[0], ("    box_t box = nondet_box_t();",)),
        )
        assert prepared.buffer_setup == tuple(_BUFFER_SETUP.splitlines())
        assert prepared.arguments == "&box, data, n, &cell, out"
        assert prepared.cleanup == tuple(_CLEANUP.splitlines())


@pytest.mark.parametrize("includes", [(), ("stdlib.h", "stdint.h")])
def test_no_parameters_need_no_storage_or_extra_headers(
    includes: tuple[str, ...],
) -> None:
    prepared = prepare_parameters((), includes=includes)
    assert prepared.headers == tuple(f"#include <{header}>" for header in includes)
    assert prepared.prototypes == ()
    assert prepared.scalar_declarations == ()
    assert prepared.struct_declarations == ()
    assert prepared.buffer_setup == ()
    assert prepared.arguments == ""
    assert prepared.cleanup == ()


def test_scalar_outputs_have_no_generator_allocation_or_cleanup() -> None:
    prepared = prepare_parameters(
        (
            ScalarParam("int", "x"),
            ScalarParam("int", "y"),
            BufferParam("unsigned", "cell", " 1 ", out=True),
        ),
        includes=(),
    )
    assert prepared.headers == ()
    assert prepared.prototypes == ("int nondet_int(void);",)
    assert prepared.scalar_declarations == (
        "    int x = nondet_int();",
        "    int y = nondet_int();",
    )
    assert prepared.buffer_setup == ("    unsigned cell;",)
    assert prepared.arguments == "x, y, &cell"
    assert prepared.cleanup == ()


def test_existing_stdlib_is_preserved_without_duplication() -> None:
    prepared = prepare_parameters(
        (BufferParam("int", "out", "3", out=True),),
        includes=("stdlib.h", "stddef.h"),
    )
    assert prepared.headers == ("#include <stdlib.h>", "#include <stddef.h>")
    assert prepared.prototypes == ()
    assert prepared.arguments == "out"
    assert prepared.buffer_setup == (
        "    __ESBMC_assume((3) <= SIZE_MAX / sizeof(int));",
        "    int *out = malloc((3) * sizeof(int));",
        "    __ESBMC_assume(out != NULL);",
    )
    assert prepared.cleanup == ("    free(out);",)


def test_struct_objects_preserve_order_and_caller_selected_type() -> None:
    first = StructParam("struct box", "first", "1", const=True)
    second = StructParam("struct box", "second", "1")
    prepared = prepare_parameters(
        (first, ScalarParam("int", "n"), second),
        includes=(),
        struct_type="box_t",
    )
    # Prototype discovery retains signature types; object declarations retain
    # the invariant caller's type spelling, just as before the extraction.
    assert prepared.prototypes == (
        "struct box nondet_struct_box(void);",
        "int nondet_int(void);",
    )
    assert prepared.struct_declarations == (
        (first, ("    box_t first = nondet_box_t();",)),
        (second, ("    box_t second = nondet_box_t();",)),
    )
    assert prepared.scalar_declarations == ("    int n = nondet_int();",)
    assert prepared.arguments == "&first, n, &second"
    assert prepared.headers == prepared.buffer_setup == prepared.cleanup == ()


def test_unknown_parameter_fails_loudly() -> None:
    with pytest.raises(HarnessError, match="unknown Param subtype: object"):
        prepare_parameters(
            (object(),),  # ty: ignore[invalid-argument-type]
            includes=(),
        )

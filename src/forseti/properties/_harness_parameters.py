"""Prepare C parameter storage shared by semantic and invariant harnesses.

Pure text synthesis. The returned fragments keep property-specific assumptions
and assertions with each renderer while concentrating linked storage decisions:
nondet prototypes, declarations, allocation, call arguments and cleanup.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from .signature import BufferParam, HarnessError, Param, ScalarParam, StructParam


@dataclass(frozen=True)
class PreparedParameters:
    """Finished C fragments at the renderers' existing insertion points.

    Scalars precede struct objects and buffers. Each struct declaration keeps
    its parameter alongside it so a renderer can immediately append its entry
    assumption. Arguments retain signature order, and cleanup matches
    precisely the buffers allocated by `buffer_setup`.
    """

    headers: tuple[str, ...]
    prototypes: tuple[str, ...]
    scalar_declarations: tuple[str, ...]
    struct_declarations: tuple[tuple[StructParam, tuple[str, ...]], ...]
    buffer_setup: tuple[str, ...]
    arguments: str
    cleanup: tuple[str, ...]


def prepare_parameters(
    params: Sequence[Param],
    *,
    includes: Sequence[str],
    struct_type: str | None = None,
) -> PreparedParameters:
    """Materialise a signature's parameters without choosing property policy.

    A `StructParam` becomes one nondet object. Without `struct_type` (semantic
    harness) the object is ``T p__obj`` of the parameter's own type with ``T *p``
    bound to its address, and the call passes ``p``. With `struct_type` (invariant
    harness) the object is ``struct_type p`` itself and the call passes ``&p``.
    Header order, first-seen prototype order, allocation guards and fill loops
    are preserved. Output cells and struct objects pass by address without heap
    allocation. Unknown parameter subtypes raise `HarnessError`.
    """
    scalars: list[ScalarParam] = []
    structs: list[StructParam] = []
    buffers: list[BufferParam] = []
    for param in params:
        if isinstance(param, ScalarParam):
            scalars.append(param)
        elif isinstance(param, StructParam):
            structs.append(param)
        elif isinstance(param, BufferParam):
            buffers.append(param)
        else:
            raise HarnessError(f"unknown Param subtype: {type(param).__name__}")

    malloc_buffers = [buf for buf in buffers if not buf.scalar_backed]
    headers = [f"#include <{inc}>" for inc in includes]
    if malloc_buffers and "stdlib.h" not in includes:
        headers.append("#include <stdlib.h>")
    return PreparedParameters(
        headers=tuple(headers),
        prototypes=tuple(
            f"{ctype} {_nondet_slug(ctype)}(void);" for ctype in _nondet_ctypes(params)
        ),
        scalar_declarations=tuple(
            f"    {scalar.ctype} {scalar.name} = {_nondet_slug(scalar.ctype)}();"
            for scalar in scalars
        ),
        struct_declarations=tuple(
            (struct, _render_struct(struct, struct_type)) for struct in structs
        ),
        buffer_setup=tuple(line for buf in buffers for line in _render_buffer(buf)),
        arguments=", ".join(
            f"&{param.name}"
            if struct_type is not None and isinstance(param, StructParam)
            else _call_arg(param)
            for param in params
        ),
        cleanup=tuple(f"    free({buf.name});" for buf in malloc_buffers),
    )


def _nondet_slug(ctype: str) -> str:
    """Nondet generator name for a C type: ``int64_t`` -> ``nondet_int64_t``.

    ESBMC models any *undefined* function named ``nondet_*`` as returning a fresh
    unconstrained value of its return type, so the mapping is general -- no fixed
    type enum.
    """
    return "nondet_" + re.sub(r"[^A-Za-z0-9_]", "_", ctype.strip())


def _nondet_ctypes(params: Sequence[Param]) -> list[str]:
    """The distinct nondet-source ctypes, in first-seen order (one prototype each).

    Only nondet-*filled* inputs contribute: scalars always, and non-out buffers
    (their element type); an output buffer is allocated, never filled, so it
    needs no generator.
    """
    ordered: list[str] = []
    for param in params:
        if isinstance(param, ScalarParam):
            ctype = param.ctype.strip()
        elif isinstance(param, BufferParam) and not param.out:
            ctype = param.elem_ctype.strip()
        else:
            continue
        if ctype not in ordered:
            ordered.append(ctype)
    return ordered


def _render_buffer(buf: BufferParam) -> list[str]:
    """Decl (+ nondet fill for inputs) for one buffer param.

    A single-element output is a plain scalar (passed by address at the call
    site); everything else is heap-allocated via `malloc` and sized to the
    *logical* length, so a read past it is a genuine out-of-bounds rather than a
    read into slack. `malloc` here, not a stack VLA: a VLA's size must be > 0
    under ESBMC's model, so an unconstrained nondet `length` (no domain entry
    excludes 0) made *every* property over such a buffer spuriously VIOLATE at
    `length == 0`, regardless of what the property actually asserted (#297).
    `malloc(0)` is well-defined C, so the length-can-be-zero case is now sound.
    `prepare_parameters` pairs every such buffer with a `free` after the
    call, for the same reason: an unfreed one reports VIOLATED ("forgotten
    memory") under `--memory-leak-check`, independent of the checked property.

    A leading `__ESBMC_assume` guards the allocation's own size arithmetic:
    `length * sizeof(elem_ctype)`, computed in `length`'s type, can wrap when
    that type is as wide as `size_t` (nothing here restricts against a
    `size_t`/`unsigned long` length param) -- the same failure class relocated
    into the size computation, confirmed with `esbmc`. `precond/synth.py`
    sidesteps this by bounding every length to a small `max_len` before it ever
    reaches `malloc` (`_length_bound`), and the check path does the same by
    default (`plan_length_bounds`); but this renderer must still support a
    genuinely unconstrained length (`max_len=None`, the point of #297), so it
    guards the multiplication itself as well, the same shape `precond/synth.py`'s
    `obligation_expr` uses to guard its own count-based allocation. Input buffers
    are nondet-filled element by element; output buffers are left for the unit to
    write.
    """
    if buf.scalar_backed:
        return [f"    {buf.elem_ctype} {buf.name};"]
    lines = [
        f"    __ESBMC_assume(({buf.length}) <= SIZE_MAX / sizeof({buf.elem_ctype}));",
        f"    {buf.elem_ctype} *{buf.name} = "
        f"malloc(({buf.length}) * sizeof({buf.elem_ctype}));",
        f"    __ESBMC_assume({buf.name} != NULL);",
    ]
    if not buf.out:
        lines.append(
            f"    for (size_t _i = 0; _i < ({buf.length}); _i++) "
            f"{buf.name}[_i] = {_nondet_slug(buf.elem_ctype)}();"
        )
    return lines


def _render_struct(struct: StructParam, struct_type: str | None) -> tuple[str, ...]:
    """One nondet object for a struct parameter."""
    if struct_type is not None:
        return (f"    {struct_type} {struct.name} = {_nondet_slug(struct_type)}();",)
    obj = f"{struct.name}__obj"
    return (
        f"    {struct.elem_ctype} {obj} = {_nondet_slug(struct.elem_ctype)}();",
        f"    {struct.elem_ctype} *{struct.name} = &{obj};",
    )


def _call_arg(param: Param) -> str:
    """The call-site argument for a param: name, or ``&name`` for a scalar-backed
    output buffer (every other buffer is already a `malloc`-allocated pointer)."""
    if isinstance(param, ScalarParam):
        return param.name
    if isinstance(param, BufferParam):
        return f"&{param.name}" if param.scalar_backed else param.name
    raise HarnessError(f"unknown Param subtype: {type(param).__name__}")

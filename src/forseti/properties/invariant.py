"""Representation invariants: a predicate over one struct type (RFC-0007 kind 1, #387).

A function that takes ``T *`` is otherwise checked against an *arbitrary* ``T``, so
the built-in safety checks report states no caller can build. A representation
invariant is a C predicate over ``T``, written over the reserved identifier
``self`` (a ``const T *``), e.g. ``self->count <= CAP && self->tail < CAP``. It is
stored per type, shared by every function that takes ``T *``, and used here to
render one self-contained ESBMC harness per function:

* a method (the default): nondet ``T``, ``__ESBMC_assume(inv)`` at entry, the call,
  ``__ESBMC_assert(inv)`` at exit for every non-const ``T *`` parameter;
* an initialiser: nondet ``T``, the call, ``__ESBMC_assert(inv)`` -- no entry
  assumption, the function is what establishes the invariant.

The invariant is *assumed* at entry, never discharged at the callers that build a
``T``: a VERIFIED (up to k) reading is conditional on it, and an over-strong
invariant can hide a bug. Pure: returns source *text* only, like `harness.py`, whose
probe twin and nondet/buffer helpers this reuses.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from enum import Enum

from .cexpr import references, unsafe_reason
from .harness import (
    DEFAULT_INCLUDES,
    PropertyHarness,
    SemanticSpec,
    _call_arg,
    _defines_main,
    _is_scalar_backed,
    _nondet_ctypes,
    _nondet_slug,
    _render_buffer,
    non_vacuity_probe,
    plan_length_bounds,
)
from .model import Property, PropertyKind, PropertyStatus, Provenance, make_property_id
from .signature import (
    BufferParam,
    HarnessError,
    ScalarParam,
    StructParam,
    UnitSignature,
    extract_signature,
)

INVARIANT_SELF = "self"
REP_INVARIANT_LABEL = "forseti:rep-invariant"
_INVARIANT_FN = "__forseti_inv"

INVARIANT_ASSUMPTION_NOTE = (
    "the representation invariant is assumed at entry, not discharged at the "
    "callers that construct the type; a verdict is conditional on it, and an "
    "over-strong invariant can hide a bug"
)

_TYPE_NAME = re.compile(r"struct\s+[A-Za-z_]\w*|(?!struct\b)[A-Za-z_]\w*")
_INITIALISER_NAME = re.compile(r"(?:^|_)(?:init|initialise|initialize)(?:_|$)", re.I)


class InvariantRole(Enum):
    """What the checked function does with the invariant."""

    METHOD = "method"  # assumed at entry, re-established at exit
    INITIALISER = "initialiser"  # establishes it from an arbitrary state


# A representation invariant has no grading slot, so it skips GRADED: a human (or a
# harness acting for one) accepts or rejects a candidate directly, and both are
# terminal, like every other property's settled states.
_INVARIANT_ALLOWED: dict[PropertyStatus, frozenset[PropertyStatus]] = {
    PropertyStatus.CANDIDATE: frozenset(
        {PropertyStatus.ACCEPTED, PropertyStatus.REJECTED}
    ),
    PropertyStatus.GRADED: frozenset(),
    PropertyStatus.ACCEPTED: frozenset(),
    PropertyStatus.REJECTED: frozenset(),
}

# What an inductive check reads. Unlike `CHECKABLE_STATUSES` this keeps ACCEPTED:
# an accepted invariant is the one every `T *` function is assumed under, so a new
# or edited mutator must still be checked against it.
INVARIANT_CHECKABLE_STATUSES = frozenset(
    {PropertyStatus.CANDIDATE, PropertyStatus.ACCEPTED}
)


def is_valid_invariant_transition(
    current: PropertyStatus, requested: PropertyStatus
) -> bool:
    """True iff a stored invariant may move from `current` to `requested`.

    ``CANDIDATE -> ACCEPTED | REJECTED`` only; both are terminal and GRADED is not
    a state an invariant reaches (`is_valid_transition` is the semantic lifecycle's).
    """
    return requested in _INVARIANT_ALLOWED[current]


class InvariantNotApplicable(HarnessError):
    """There is nothing invariant-specific to check (not a render failure).

    A method that takes only ``const T *`` cannot break the invariant, so the check
    driver reports it ``skipped`` with this reason rather than ``error``, and never
    ``held``.
    """


def infer_role(symbol: str) -> InvariantRole:
    """`INITIALISER` for a name with an ``init``/``initialise``/``initialize`` part.

    Deliberately narrow: a wrong guess drops the entry assumption and reads as a
    spurious violation, so ``create``/``new``/``setup`` (``ring_new_slot``) are not
    guessed -- name those with an explicit `role`.
    """
    if _INITIALISER_NAME.search(symbol):
        return InvariantRole.INITIALISER
    return InvariantRole.METHOD


def normalised_type_name(type_name: str) -> str:
    """`type_name` with whitespace runs collapsed: ``struct  node`` -> ``struct node``.

    The store key and the signature match must agree on one spelling.
    """
    return " ".join(type_name.split())


def valid_type_name(type_name: str) -> bool:
    """A typedef name (``ring_t``) or a tagged struct (``struct node``)."""
    return _TYPE_NAME.fullmatch(type_name) is not None


def invariant_reason(expression: str) -> str | None:
    """None if `expression` is an acceptable invariant text, else why not.

    Static and shape-only: a pure C boolean expression (`unsafe_reason`, the same
    gate every property expression passes -- it is spliced into the harness) that
    names ``self``. Identifiers are not resolved here: an unknown field or macro
    fails to compile, which the check driver reports as an ``error``, never a pass.
    """
    if not expression.strip():
        return "empty invariant expression"
    unsafe = unsafe_reason(expression)
    if unsafe is not None:
        return f"unsafe expression: {unsafe}"
    if not references(expression, INVARIANT_SELF):
        return (
            f"vacuous invariant: does not reference {INVARIANT_SELF!r} "
            "(write it over the instance, e.g. self->count <= CAP)"
        )
    return None


def make_invariant(
    *,
    type_key: str,
    expression: str,
    provenance: Provenance,
    description: str | None = None,
) -> Property:
    """A ``CANDIDATE`` representation invariant for the type keyed `type_key`."""
    return Property(
        property_id=make_property_id(type_key, PropertyKind.REP_INVARIANT, expression),
        unit_id=type_key,
        kind=PropertyKind.REP_INVARIANT,
        expression=expression,
        status=PropertyStatus.CANDIDATE,
        provenance=provenance,
        description=description,
    )


def render_invariant_harness(
    *,
    unit_source: str,
    signature: UnitSignature,
    type_name: str,
    expression: str,
    role: InvariantRole = InvariantRole.METHOD,
    max_len: int | None = None,
    probe: bool = False,
    includes: Sequence[str] = DEFAULT_INCLUDES,
) -> str:
    """Return a compilable ESBMC harness (C text) checking `expression` on one function.

    `signature` must have been parsed with ``struct_types=(type_name,)`` so the
    ``T *`` parameters are `StructParam`s. Raises `HarnessError` on a unit that
    defines its own ``main``, an empty or non-type name, no ``T *`` parameter, or an
    initialiser with no non-const one; `InvariantNotApplicable` for a method that
    takes only ``const T *`` (it cannot mutate, so there is no exit obligation).

    `probe=True` renders the vacuity twin: byte-identical up to and including the
    call, with the exit assertion replaced by the labelled ``assert(0)``
    (`non_vacuity_probe`). A probe that fires shows the assumed invariant admits a
    state from which the call returns; a VERIFIED probe shows it never did (the
    invariant is unsatisfiable, e.g. ``0``) -- the same reading `check_properties`
    applies to a semantic property.
    """
    if _defines_main(unit_source):
        raise HarnessError(
            "unit_source defines main; pass the kernel slice, not the example harness"
        )
    if not valid_type_name(type_name):
        raise HarnessError(f"{type_name!r} is not a struct type name")
    if not expression.strip():
        raise HarnessError("representation invariant has an empty expression")

    structs = [p for p in signature.params if isinstance(p, StructParam)]
    if not structs:
        raise HarnessError(
            f"{signature.symbol!r} takes no {type_name!r} pointer parameter"
        )
    mutated = [p for p in structs if not p.const]
    if not mutated:
        if role is InvariantRole.INITIALISER:
            raise HarnessError(
                f"initialiser {signature.symbol!r} takes only const {type_name} *"
            )
        raise InvariantNotApplicable(
            f"{signature.symbol!r} takes only const {type_name} *, so it cannot "
            "break the invariant, so there is no exit obligation to check"
        )

    buffers = [
        p
        for p in signature.params
        if isinstance(p, BufferParam) and not isinstance(p, StructParam)
    ]
    malloc_buffers = [b for b in buffers if not _is_scalar_backed(b)]

    lines = [f"#include <{inc}>" for inc in includes]
    if malloc_buffers and "stdlib.h" not in includes:
        lines.append("#include <stdlib.h>")
    lines += ["", unit_source.strip("\n"), ""]
    for ctype in _nondet_ctypes(signature.params):
        lines.append(f"{ctype} {_nondet_slug(ctype)}(void);")
    lines += [
        f"static int {_INVARIANT_FN}(const {type_name} *{INVARIANT_SELF}) {{",
        f"    return ({expression.strip()});",
        "}",
        "",
        f"/* forseti: representation invariant, role={role.value} */",
        "int main(void) {",
    ]
    for scalar in (p for p in signature.params if isinstance(p, ScalarParam)):
        lines.append(
            f"    {scalar.ctype} {scalar.name} = {_nondet_slug(scalar.ctype)}();"
        )
    for struct in structs:
        lines.append(f"    {type_name} {struct.name} = {_nondet_slug(type_name)}();")
        if role is InvariantRole.METHOD:
            lines.append(f"    __ESBMC_assume({_INVARIANT_FN}(&{struct.name}));")
    for name, bound in plan_length_bounds(signature, SemanticSpec("1"), max_len):
        lines.append(f"    __ESBMC_assume(({name} >= 0 && {name} <= {bound}));")
    for buf in buffers:
        lines += _render_buffer(buf)
    args = ", ".join(
        f"&{p.name}" if isinstance(p, StructParam) else _call_arg(p)
        for p in signature.params
    )
    lines.append(f"    {signature.symbol}({args});")
    if probe:
        lines.append(f"    {non_vacuity_probe()}")
    else:
        for struct in mutated:
            lines.append(
                f"    __ESBMC_assert({_INVARIANT_FN}(&{struct.name}), "
                f'"{REP_INVARIANT_LABEL}");'
            )
    lines += [f"    free({buf.name});" for buf in malloc_buffers]
    lines += ["    return 0;", "}"]
    return "\n".join(lines) + "\n"


def build_invariant_harness(
    *,
    unit_source: str,
    symbol: str,
    type_name: str,
    expression: str,
    role: InvariantRole | None = None,
    max_len: int | None = None,
) -> PropertyHarness:
    """`render_invariant_harness` from source: the harness, its probe twin and bounds.

    Parses `symbol`'s signature with `type_name` as a struct type; `role` defaults to
    `infer_role(symbol)`. `HarnessError` from any leg propagates unchanged.
    """
    signature = extract_signature(unit_source, symbol, struct_types=(type_name,))
    chosen = role if role is not None else infer_role(symbol)

    def render(probe: bool) -> str:
        return render_invariant_harness(
            unit_source=unit_source,
            signature=signature,
            type_name=type_name,
            expression=expression,
            role=chosen,
            max_len=max_len,
            probe=probe,
        )

    return PropertyHarness(
        source_text=render(False),
        length_bounds=plan_length_bounds(signature, SemanticSpec("1"), max_len),
        probe_source_text=render(True),
    )

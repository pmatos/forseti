"""Signature-driven memory-precondition synthesis (RFC-0003 S2, L0 mechanical).

The v0 gate verifies a pointer-taking function at the *function level*, where
ESBMC passes each pointer an unconstrained value ranging over the whole object
universe — including the invalid object — so the first ``*p`` is (soundly)
VIOLATED under the empty precondition. That is a *missing input precondition*,
not a bug: the honest fix is to **materialize a valid backing object** and verify
against that.

This module reads the precondition off the *type signature* alone — no LLM, no
functional-property machinery (RFC-0003 D1). For each pointer parameter it picks
one of a few mechanical shapes and renders a **sidecar** C translation unit: it
``#include``\\ s the source verbatim (the user's file stays pristine), allocates a
valid object per pointer, constrains any symbolic length, and calls the unit.

The shapes (L0):

- **scalar ``T *p``** (a single complete-typed pointee, no length) → one fresh
  ``T`` object (``malloc(sizeof(*p))``).
- **``T *p`` adjacent to a length integer** — a *byte length* (``len``/``size``/
  ``nbytes``) → ``malloc(len)``; an *element count* (``n``/``count``/``nmemb``) →
  ``malloc(count * sizeof(*p))`` (equal only when ``sizeof(*p) == 1``). The
  length is a **symbolic** ``nondet`` bounded by ``max_len`` — exact sizing, so an
  off-by-one ``p[len]`` is out of bounds (a constant ``buf[MAX]`` would hide it).
- **fixed array ``T p[N]``** → ``malloc(N * sizeof(*p))``, ``N`` from the
  signature (`Param.array_extent`, recovered by `list_units`).

Anything else — ``void *`` (no pointee size), ``T **`` / pointer-to-array /
function pointer — is **UNRESOLVED**: L0 cannot justify a precondition, so the
unit is reported ``NEEDS_CONTRACT`` (loud, non-blocking) rather than materialized
wrongly. Rendering is pure (returns C text, no ESBMC, no disk); the verify
driver owns the effects and the honest labeling.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

from forseti.esbmc.units import Param, Unit

# The default symbolic-length ceiling. A pointer sized by a symbolic length is
# `malloc(len)` with `len <= max_len`; the loop that consumes it then needs an
# unwind bound above `max_len`, which the verify driver's k-ladder supplies. A
# bounded verdict is "assumed-verified up to max_len", never "for all lengths".
DEFAULT_MAX_LEN = 8

# System headers the sidecar needs beyond what the included source already pulls
# in. `malloc`/`size_t` come from stdlib; the source supplies its own stdint etc.
DEFAULT_INCLUDES: tuple[str, ...] = ("stdlib.h",)

# The property label the non-vacuity probe (`__ESBMC_assert(0, ...)`) carries, so
# a reachable call site is a recognisable FAILED rather than an anonymous one.
NON_VACUITY_LABEL = "forseti:non-vacuity"

# Parameter-name heuristics for the (pointer, length) idiom. A *byte length*
# sizes the object in bytes; an *element count* sizes it in `sizeof(*p)` units.
# Deliberately small (RFC-0003's stated sets plus a couple of obvious synonyms):
# a wrong pairing is worse than an unresolved one, which degrades to a fresh
# single object rather than a silent mis-size.
_BYTE_LEN_NAMES = frozenset({"len", "length", "size", "nbytes", "n_bytes", "buflen"})
_ELEM_COUNT_NAMES = frozenset({"n", "count", "nmemb", "num", "nelem"})

# Canonical-type tokens that mark an integer (a length must be integral, so a
# `double len` is not mistaken for a size). Pointers are excluded before this.
_INT_TOKENS = ("int", "long", "short", "char", "size_t", "unsigned", "signed")


class SynthError(ValueError):
    """The unit cannot be synthesised as-is (unresolved pointer, or no plan)."""


class ParamRole(Enum):
    """How one parameter is materialised in the sidecar harness."""

    SCALAR = "scalar"  # a plain nondet scalar
    LENGTH = "length"  # a nondet scalar consumed as a pointer's symbolic size
    SCALAR_PTR = "scalar_ptr"  # one fresh object (`malloc(sizeof(*p))`)
    PTR_BYTE_LEN = "ptr_byte_len"  # `malloc(len)` — byte-sized
    PTR_ELEM_COUNT = "ptr_elem_count"  # `malloc(count * sizeof(*p))`
    FIXED_ARRAY = "fixed_array"  # `malloc(N * sizeof(*p))`
    UNRESOLVED = "unresolved"  # L0 cannot justify a precondition → NEEDS_CONTRACT


@dataclass(frozen=True)
class ParamPlan:
    """The materialisation plan for one parameter."""

    param: Param
    var: str  # the harness variable name (the param's, or `argN` when unnamed)
    role: ParamRole
    length_var: str | None = None  # for PTR_*_LEN: the length variable to size by
    extent: int | None = None  # for FIXED_ARRAY: N


@dataclass(frozen=True)
class UnitPlan:
    """The whole-unit plan: an ordered `ParamPlan` per parameter."""

    unit: Unit
    params: tuple[ParamPlan, ...]

    @property
    def resolvable(self) -> bool:
        """True iff every pointer parameter got a materialisable plan (no L2)."""
        return all(p.role is not ParamRole.UNRESOLVED for p in self.params)

    @property
    def unresolved_params(self) -> tuple[str, ...]:
        """Names (or ``argN``) of the parameters L0 could not resolve."""
        return tuple(p.var for p in self.params if p.role is ParamRole.UNRESOLVED)


def _var_name(index: int, param: Param) -> str:
    """The harness variable for a parameter: its own name, or ``argN`` if unnamed."""
    return param.name if param.name else f"arg{index}"


def _is_integer_type(type_str: str) -> bool:
    return any(tok in type_str for tok in _INT_TOKENS)


def _is_unsigned(type_str: str) -> bool:
    # Canonical types resolve `size_t` to `unsigned long`, so an unsigned length
    # is detectable by the `unsigned` token; a signed length also gets a `>= 0`.
    return "unsigned" in type_str


def _length_kind(param: Param) -> ParamRole | None:
    """`PTR_BYTE_LEN` / `PTR_ELEM_COUNT` if `param` reads as a length, else None."""
    if param.is_pointer or not _is_integer_type(param.type):
        return None
    name = param.name.lower()
    if name in _BYTE_LEN_NAMES:
        return ParamRole.PTR_BYTE_LEN
    if name in _ELEM_COUNT_NAMES:
        return ParamRole.PTR_ELEM_COUNT
    return None


def _is_pointee_materialisable(type_str: str) -> bool:
    """True for a single-level pointer to a complete, sizeable object type.

    Rejects the shapes L0 cannot back with a valid object: a function pointer or
    pointer-to-array (``(*)`` in the canonical type), a multi-level pointer
    (``T **`` — one fresh ``T *`` would still dangle), and ``void *`` (no pointee
    size). Everything else — ``T *``, ``struct S *``, ``const uint8_t *`` — is a
    single object of ``sizeof(*p)``.
    """
    stripped = type_str.strip()
    if "(*" in stripped:  # function pointer / pointer-to-array
        return False
    if stripped.count("*") != 1:  # only single-level pointers
        return False
    # Scrub the cv/`restrict` qualifiers clang keeps on the canonical type
    # (`void *restrict`, `const void *restrict`) so a `void` pointee is detected
    # whatever qualifies it — otherwise the qualifier survives and a `void *`
    # would be mis-sized `malloc(sizeof(void))` instead of falling to UNRESOLVED.
    without_ptr = re.sub(
        r"\bconst\b|\bvolatile\b|\b__restrict__\b|\b__restrict\b|\brestrict\b|\*|\s",
        "",
        stripped,
    )
    return without_ptr != "void" and without_ptr != ""


def plan_unit(unit: Unit) -> UnitPlan:
    """Classify each parameter into its L0 materialisation plan (pure).

    Pointers are classified first (a fixed extent wins over length-pairing, which
    wins over a lone fresh object); a following integer consumed as a pointer's
    length is then marked ``LENGTH``; every remaining non-pointer is a plain
    ``SCALAR``. The pairing looks only at the *next* parameter — the dominant
    ``(ptr, len)`` idiom (RFC-0003 OQ2 flags richer pairing as L1).
    """
    n = len(unit.params)
    roles: list[ParamRole | None] = [None] * n
    length_var: list[str | None] = [None] * n
    extents: list[int | None] = [None] * n
    consumed_as_length: set[int] = set()

    for i, param in enumerate(unit.params):
        if not param.is_pointer:
            continue
        if not _is_pointee_materialisable(param.type):
            roles[i] = ParamRole.UNRESOLVED
            continue
        if param.array_extent is not None:
            roles[i] = ParamRole.FIXED_ARRAY
            extents[i] = param.array_extent
            continue
        if i + 1 < n and (i + 1) not in consumed_as_length:
            kind = _length_kind(unit.params[i + 1])
            if kind is not None:
                roles[i] = kind
                length_var[i] = _var_name(i + 1, unit.params[i + 1])
                consumed_as_length.add(i + 1)
                continue
        roles[i] = ParamRole.SCALAR_PTR

    # Every remaining unclassified slot is a non-pointer: a length consumed by a
    # pointer, else a plain scalar. This fills the list so no `None` survives.
    final_roles: list[ParamRole] = [
        role
        if role is not None
        else (ParamRole.LENGTH if i in consumed_as_length else ParamRole.SCALAR)
        for i, role in enumerate(roles)
    ]

    plans = tuple(
        ParamPlan(
            param=param,
            var=_var_name(i, param),
            role=final_roles[i],
            length_var=length_var[i],
            extent=extents[i],
        )
        for i, param in enumerate(unit.params)
    )
    return UnitPlan(unit, plans)


def _nondet_slug(type_str: str) -> str:
    """A `nondet_*` helper name for a scalar type (ESBMC models it as nondet)."""
    slug = re.sub(r"[^A-Za-z0-9]+", "_", type_str.strip()).strip("_")
    return f"nondet_{slug}"


def _length_bound(plan: ParamPlan, max_len: int) -> str:
    """The `__ESBMC_assume` bounding a length variable to `[0, max_len]`."""
    lo = "" if _is_unsigned(plan.param.type) else f"{plan.var} >= 0 && "
    return f"__ESBMC_assume({lo}{plan.var} <= {max_len});"


def _pointer_alloc(plan: ParamPlan) -> str:
    """The `malloc(...)` size expression for a pointer/array plan."""
    if plan.role is ParamRole.SCALAR_PTR:
        return f"sizeof(*{plan.var})"
    if plan.role is ParamRole.PTR_BYTE_LEN:
        return f"(size_t){plan.length_var}"
    if plan.role is ParamRole.PTR_ELEM_COUNT:
        return f"(size_t){plan.length_var} * sizeof(*{plan.var})"
    if plan.role is ParamRole.FIXED_ARRAY:
        return f"(size_t){plan.extent} * sizeof(*{plan.var})"
    raise SynthError(f"not a pointer plan: {plan.role}")  # pragma: no cover


_POINTER_ROLES = frozenset(
    {
        ParamRole.SCALAR_PTR,
        ParamRole.PTR_BYTE_LEN,
        ParamRole.PTR_ELEM_COUNT,
        ParamRole.FIXED_ARRAY,
    }
)


def render_sidecar(
    plan: UnitPlan,
    source_include: str,
    *,
    max_len: int = DEFAULT_MAX_LEN,
    non_vacuity: bool = False,
    includes: Sequence[str] = DEFAULT_INCLUDES,
) -> str:
    """Render the sidecar C translation unit for `plan` (pure).

    Emits, in order: ``#include "<source_include>"`` (the source verbatim, so the
    user's file stays pristine), the system `includes`, one `nondet_*` prototype
    per distinct scalar/length type, then an ``int main`` that declares the
    scalars/lengths first (a length bounded to ``max_len``), allocates each
    pointer object with **exact** size, calls the unit, and returns. With
    `non_vacuity` a ``__ESBMC_assert(0)`` is emitted after the call: a reachable
    call site makes it FAIL (the harness is non-vacuous); an unreachable one lets
    it pass (the synthesised precondition is contradictory — a vacuous "proof").

    Raises `SynthError` if `plan` has any unresolved parameter — the driver must
    report ``NEEDS_CONTRACT`` instead of rendering a wrong object.
    """
    if not plan.resolvable:
        raise SynthError(
            f"{plan.unit.name}: unresolved parameters {plan.unresolved_params}"
        )

    scalars = [p for p in plan.params if p.role in (ParamRole.SCALAR, ParamRole.LENGTH)]
    pointers = [p for p in plan.params if p.role in _POINTER_ROLES]

    # One prototype per distinct scalar type; ESBMC treats an undefined `nondet_*`
    # as an unconstrained value of its return type.
    nondet_types: list[str] = []
    for p in scalars:
        if p.param.type not in nondet_types:
            nondet_types.append(p.param.type)

    lines: list[str] = [f'#include "{source_include}"']
    lines += [f"#include <{header}>" for header in includes]
    lines.append("")
    lines += [f"extern {t} {_nondet_slug(t)}(void);" for t in nondet_types]
    lines.append("")
    lines.append("int main(void) {")

    for p in scalars:
        lines.append(f"    {p.param.type} {p.var} = {_nondet_slug(p.param.type)}();")
        if p.role is ParamRole.LENGTH:
            lines.append(f"    {_length_bound(p, max_len)}")

    for p in pointers:
        lines.append(f"    {p.param.type} {p.var} = malloc({_pointer_alloc(p)});")

    args = ", ".join(p.var for p in plan.params)
    lines.append(f"    {plan.unit.name}({args});")
    if non_vacuity:
        lines.append(f'    __ESBMC_assert(0, "{NON_VACUITY_LABEL}");')
    lines.append("    return 0;")
    lines.append("}")
    return "\n".join(lines) + "\n"

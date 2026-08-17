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
- **fixed array ``T p[N]`` with no accompanying length** → ``malloc(N *
  sizeof(*p))``, ``N`` from the signature (`Param.array_extent`, recovered by
  `list_units`). A companion length pairs instead and sizes the object by
  itself — a conventional ``T p[N]`` binds nobody (C adjusts the parameter to
  ``T *``), so the length is the better authority (issue #147).
- **``T p[static N]`` next to a length** → ``malloc(max(length, N))``. C99's
  ``static N`` is a *minimum* the caller must supply, not the object's size, so a
  valid caller satisfies both it and the length convention.

Anything else — ``void *`` (no pointee size), ``T **`` / pointer-to-array /
function pointer, or a fixed array whose written extent is not readable from the
source (``T p[SHA_DIGEST_LENGTH]``, which needs the preprocessor) and has no
accompanying length — is **UNRESOLVED**: L0 cannot justify a precondition, so the
unit is reported ``NEEDS_CONTRACT`` (loud, non-blocking) rather than materialized
wrongly. Rendering is pure (returns C text, no ESBMC, no disk); the verify
driver owns the effects and the honest labeling.

The same plan renders the *other half* of the story (S3, `inject_obligations`):
the precondition the sidecar **assumes** by allocating, written into a generated
copy of the translation unit as a **checked** obligation on every caller. One
renderer, so the check can never be weaker or stronger than the assumption.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

from forseti.esbmc.units import Param, Unit, find_definition_brace

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

# Prefix of the label each injected *caller obligation* carries (RFC-0003 S3), so
# a FAILED that is the caller breaking the precondition is told apart from one
# that is a bug inside the callee. Followed by `<function>:<parameter>`.
OBLIGATION_LABEL_PREFIX = "forseti:obligation:"

# Prefix of the label the obligation *site* probe carries. It marks the callee's
# entry with `__ESBMC_assert(0, ...)`: a caller that reaches the call makes it
# FAIL, so a VERIFIED obligation run is credited only when the site was actually
# reached — a dead call site would otherwise discharge vacuously.
OBLIGATION_SITE_LABEL_PREFIX = "forseti:obligation-site:"

# Parameter-name heuristics for the (pointer, length) idiom. A *byte length*
# sizes the object in bytes; an *element count* sizes it in `sizeof(*p)` units.
# Deliberately small (RFC-0003's stated sets plus a couple of obvious synonyms):
# a wrong pairing is worse than an unresolved one, which degrades to a fresh
# single object rather than a silent mis-size.
_BYTE_LEN_NAMES = frozenset({"len", "length", "size", "nbytes", "n_bytes", "buflen"})
_ELEM_COUNT_NAMES = frozenset({"n", "count", "nmemb", "num", "nelem"})

# C11's atomic types keep a functional spelling in clang's canonical type:
# `void *_Atomic p` prints as `_Atomic(void *)`, `_Atomic int *p` as
# `_Atomic(int) *`. Unwrapping it leaves the type it qualifies, so the pointer
# shape below is read off `void *` / `int *` rather than off a token soup in
# which a `void` pointee hides. Innermost-first (no nested parens), so an atomic
# function pointer keeps its `(*` and is rejected as one.
_ATOMIC_WRAPPER_RE = re.compile(r"\b_Atomic\s*\(([^()]*)\)")

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


_POINTER_ROLES = frozenset(
    {
        ParamRole.SCALAR_PTR,
        ParamRole.PTR_BYTE_LEN,
        ParamRole.PTR_ELEM_COUNT,
        ParamRole.FIXED_ARRAY,
    }
)


@dataclass(frozen=True)
class ParamPlan:
    """The materialisation plan for one parameter."""

    param: Param
    var: str  # the harness variable name (the param's, or `argN` when unnamed)
    role: ParamRole
    length_var: str | None = None  # for PTR_*_LEN: the length variable to size by
    extent: int | None = None  # for FIXED_ARRAY: N
    # For a length-sized pointer also written `T p[static N]`: the element floor
    # the *caller* is bound to supply, so the allocation is never smaller than it.
    static_min_extent: int | None = None


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

    @property
    def pointer_params(self) -> tuple[ParamPlan, ...]:
        """The parameters materialised as objects — the ones a precondition binds.

        A unit with none of these has an *empty* memory precondition: nothing was
        assumed about a caller, so there is nothing to discharge.
        """
        return tuple(p for p in self.params if p.role in _POINTER_ROLES)


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
    stripped = _ATOMIC_WRAPPER_RE.sub(r"\1", type_str.strip())
    if "(*" in stripped:  # function pointer / pointer-to-array
        return False
    if stripped.count("*") != 1:  # only single-level pointers
        return False
    # Scrub the cv/`restrict`/`_Atomic` qualifiers clang keeps on the canonical
    # type (`void *restrict`, `const void *restrict`) so a `void` pointee is
    # detected whatever qualifies it — otherwise the qualifier survives and a
    # `void *` would be mis-sized `malloc(sizeof(void))` instead of falling to
    # UNRESOLVED.
    without_ptr = re.sub(
        r"\bconst\b|\bvolatile\b|\b__restrict__\b|\b__restrict\b|\brestrict\b"
        r"|\b_Atomic\b|\*|\s",
        "",
        stripped,
    )
    return without_ptr != "void" and without_ptr != ""


def plan_unit(unit: Unit) -> UnitPlan:
    """Classify each parameter into its L0 materialisation plan (pure).

    Pointers are classified first (length-pairing wins over a fixed extent,
    which wins over a lone fresh object); a following integer consumed as a
    pointer's length is then marked ``LENGTH``; every remaining non-pointer is a
    plain ``SCALAR``. The pairing looks only at the *next* parameter — the
    dominant ``(ptr, len)`` idiom (RFC-0003 OQ2 flags richer pairing as L1).

    Neither a conventional ``T p[N]`` nor C99's ``T p[static N]`` wins outright
    over an accompanying length — the written extent is documentation the
    signature carries, not an allocation the callee itself demands:

    - **conventional ``T p[N]``** binds nobody (C adjusts the parameter to
      ``T *``), so a companion length is simply the better authority: the
      object is sized by the length alone, with ``N`` dropped entirely (issue
      #147). A body that reads all ``N`` elements regardless of what a smaller
      ``length`` says is the accepted trade-off — this is documentation, not a
      caller obligation, so there is nothing to floor against.
    - **``T p[static N]``** *is* a caller obligation: the argument must give
      access to at least ``N`` elements no matter what the length says, so it
      pairs with the length and becomes that plan's `static_min_extent` floor
      instead (``max(length, N)``, issue #137).

    Both fall back to `FIXED_ARRAY` only when there is no length to pair with.
    """
    n = len(unit.params)
    roles: list[ParamRole | None] = [None] * n
    length_var: list[str | None] = [None] * n
    extents: list[int | None] = [None] * n
    static_mins: list[int | None] = [None] * n
    consumed_as_length: set[int] = set()

    for i, param in enumerate(unit.params):
        if not param.is_pointer:
            continue
        if not _is_pointee_materialisable(param.type):
            roles[i] = ParamRole.UNRESOLVED
            continue
        # `T p[static <macro>]`: the caller *must* give access to the declared
        # extent, so the function may touch all of it however small a companion
        # length is — and with `N` unreadable there is no floor to raise that
        # length to, so L0 declines rather than under-allocating.
        if param.array_extent_unresolved and param.array_static_min:
            roles[i] = ParamRole.UNRESOLVED
            continue
        if i + 1 < n and (i + 1) not in consumed_as_length:
            kind = _length_kind(unit.params[i + 1])
            if kind is not None:
                roles[i] = kind
                length_var[i] = _var_name(i + 1, unit.params[i + 1])
                static_mins[i] = param.array_extent if param.array_static_min else None
                consumed_as_length.add(i + 1)
                continue
        # A readable extent with no length to pair with: `T p[N]` states the only
        # size the signature carries, and the weakest valid caller for `T p[static
        # N]` supplies exactly `N` — either way, the fixed-array shape.
        if param.array_extent is not None:
            roles[i] = ParamRole.FIXED_ARRAY
            extents[i] = param.array_extent
            continue
        # Written `T p[<macro or expression>]`: the declared extent needs the
        # preprocessor, so the one-element fallback below would under-size the
        # object and phantom-VIOLATE a unit that reads the full extent. Checked
        # *after* length-pairing on purpose — an accompanying length sizes the
        # object exactly, which is better than declining (issue #137).
        if param.array_extent_unresolved:
            roles[i] = ParamRole.UNRESOLVED
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
            static_min_extent=static_mins[i],
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


def _at_least(size: str, floor: str) -> str:
    """`size` raised to `floor` — C has no `max`, so a conditional expression."""
    return f"({size} > {floor} ? {size} : {floor})"


def _element_count(plan: ParamPlan) -> str | None:
    """The count `_pointer_alloc` multiplies by `sizeof(*p)`, or ``None``.

    ``None`` for every role but `PTR_ELEM_COUNT` — the only one where this
    multiplicand is caller-controlled rather than fixed at compile time
    (`FIXED_ARRAY`'s ``extent`` comes from the signature itself) or already
    byte-sized with no multiplication at all (`PTR_BYTE_LEN`). Exposed so
    `obligation_expr` can guard the *same* value `_pointer_alloc` multiplies,
    rather than re-deriving an expression that could silently drift from it.
    """
    if plan.role is not ParamRole.PTR_ELEM_COUNT:
        return None
    count = f"(size_t){plan.length_var}"
    if plan.static_min_extent is not None:
        count = _at_least(count, f"(size_t){plan.static_min_extent}")
    return count


def _pointer_alloc(plan: ParamPlan) -> str:
    """The `malloc(...)` size expression for a pointer/array plan.

    A length-sized pointer that also carries a `[static N]` floor is allocated
    ``max(length, N)``: a valid caller has to satisfy *both* the pointer/length
    convention and the C99 obligation, so the weakest one supplies whichever is
    larger. Sizing by the length alone would phantom-VIOLATE a body that reads all
    ``N``; sizing by ``N`` alone would phantom-VIOLATE one that reads ``length``
    elements when ``length > N`` (issue #137).
    """
    if plan.role is ParamRole.SCALAR_PTR:
        return f"sizeof(*{plan.var})"
    if plan.role is ParamRole.PTR_BYTE_LEN:
        nbytes = f"(size_t){plan.length_var}"
        if plan.static_min_extent is None:
            return nbytes
        floor = f"(size_t){plan.static_min_extent} * sizeof(*{plan.var})"
        return _at_least(nbytes, floor)
    if plan.role is ParamRole.PTR_ELEM_COUNT:
        count = _element_count(plan)
        assert count is not None  # role is PTR_ELEM_COUNT, so `_element_count` gave one
        return f"{count} * sizeof(*{plan.var})"
    if plan.role is ParamRole.FIXED_ARRAY:
        return f"(size_t){plan.extent} * sizeof(*{plan.var})"
    raise SynthError(f"not a pointer plan: {plan.role}")  # pragma: no cover


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
    pointers = plan.pointer_params

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


def obligation_expr(plan: ParamPlan) -> str:
    """The C predicate a caller must satisfy for one pointer parameter (pure).

    The *same* size expression the sidecar allocates, turned from an assumption
    into a check: where S2 writes ``malloc(len)`` to materialise a valid object,
    S3 demands the caller supplied one that big. Reusing `_pointer_alloc` is
    load-bearing — an obligation weaker than the assumption would discharge
    nothing, and one stronger would reject valid callers.

    ``__ESBMC_r_ok`` rather than ``__ESBMC_is_fresh`` because it is what esbmc
    8.3.0 can actually *check*, and because it is the weaker, more honest
    obligation: it admits an interior pointer into a larger live object, which a
    caller may legitimately pass. (`is_fresh` is unusable here for a second
    reason — the contract machinery cannot transplant an intrinsic call's
    temporary into the checking context; RFC-0003 OQ3, pinned by
    `tests/esbmc/test_contract_vehicle.py`.)

    The expression **rebases to the object start** rather than calling
    ``r_ok(p, n)`` directly, which is not a stylistic choice: on our pinned build
    ``r_ok`` answers *true* for a pointer whose offset already lies past its
    object's end (``r_ok(malloc(1) + 2, 4)`` passes), so the direct form would
    silently discharge exactly the caller bug that matters — a run-off interior
    pointer. Asking instead for ``offset + n`` bytes *from offset zero*, where
    ``r_ok`` is exact, restores the intended meaning. The three guards in front
    are equally load-bearing: a negative offset is not a rebasable pointer,
    ``offset + n`` wrapping around ``size_t`` (a caller's underflowed length,
    the classic ``len - HEADER`` bug) would otherwise ask for a *tiny* span and
    pass, and — for `PTR_ELEM_COUNT`, whose `_pointer_alloc` multiplies a
    caller-controlled count by ``sizeof(*p)`` — that multiplication itself can
    wrap first: a caller passing a count near ``SIZE_MAX / sizeof(*p)`` shrinks
    ``n`` before either the offset or the addition is ever checked, so the count
    is bounded against overflowing *its own* multiplication before `_pointer_alloc`
    is trusted at all.
    """
    ptr = f"(const void *){plan.var}"
    offset = f"__ESBMC_POINTER_OFFSET({ptr})"
    size = f"(__SIZE_TYPE__)({_pointer_alloc(plan)})"
    span = f"((__SIZE_TYPE__){offset} + {size})"
    base = f"(void *)((const char *){plan.var} - {offset})"
    count = _element_count(plan)
    count_guard = (
        ""
        if count is None
        else f"({count}) <= (__SIZE_TYPE__)-1 / sizeof(*{plan.var}) && "
    )
    return (
        f"({count_guard}{offset} >= 0 && {span} >= {size} && "
        f"__ESBMC_r_ok({base}, {span}))"
    )


def _obligation_targets(plan: UnitPlan) -> tuple[ParamPlan, ...]:
    """The pointer parameters an injected obligation can name, or raise.

    Injection writes C *inside the callee's body*, so every identifier it uses
    must be a real parameter name. A pointer the signature left unnamed is
    spelled ``argN`` by the sidecar — fine there, since the sidecar declares it,
    but unwritable here. Rather than inject a reference to a name that does not
    exist, decline: the driver reports ``NEEDS_CONTRACT``. Only the pointers need
    checking; a length is paired by *name*, so an unnamed parameter is never one.
    """
    if not plan.resolvable:
        raise SynthError(
            f"{plan.unit.name}: unresolved parameters {plan.unresolved_params}"
        )
    pointers = plan.pointer_params
    unnameable = tuple(p.var for p in pointers if not p.param.name)
    if unnameable:
        raise SynthError(
            f"{plan.unit.name}: cannot inject an obligation naming unnamed "
            f"parameter(s) {unnameable}"
        )
    return pointers


def _line_directive(source_path: str) -> str:
    """A ``#line`` directive that reports `source_path` from line 1 onward.

    ``#line N "file"`` tells the preprocessor that the *next* physical line is
    line ``N`` of ``file`` — so inserting it as an extra physical line renumbers
    everything after it without shifting where anything actually sits, which is
    what keeps `inject_obligations`'s "every line number matches the original"
    promise even with this line prepended. Quotes and backslashes are escaped;
    the string is otherwise opaque to the preprocessor.
    """
    escaped = source_path.replace("\\", "\\\\").replace('"', '\\"')
    return f'#line 1 "{escaped}"\n'


def inject_obligations(
    source_text: str,
    plan: UnitPlan,
    *,
    site_probe: bool = False,
    source_path: str | None = None,
) -> str:
    """`source_text` with `plan`'s memory precondition injected as caller checks.

    The *transparent contract injection* of RFC-0003 OQ1: the returned text is a
    **generated copy** of the translation unit, so the user's file stays pristine
    (S2's promise) while the callee carries a checkable obligation. One labelled
    ``__ESBMC_assert`` per pointer parameter is inserted immediately after the
    definition's ``{``, on that same line — so every line number in the copy
    matches the original and a counterexample reads against the user's source.

    Verified from a *caller's* entry point, each assert is evaluated with that
    caller's actual arguments: an invalid or too-small pointer FAILS the callee's
    obligation rather than producing a dereference failure attributable to the
    callee. With `site_probe` the obligations are replaced by a single
    ``__ESBMC_assert(0, ...)`` marking the entry, which a caller that reaches the
    call makes FAIL — the reachability discharge that keeps a dead call site from
    passing vacuously.

    The copy is written to disk under its own path, not the source's — so
    without `source_path`, ``__FILE__`` inside it would report *that* path
    instead, and a caller whose behaviour or object sizing depends on
    ``__FILE__`` would be checked against a program that is not quite the one
    being verified. Passing `source_path` prepends a ``#line`` directive that
    restores it, without disturbing any other line's reported number.
    ``None`` when the copy is never written under a different name (a pure or
    testing call, where there is no path to restore).

    Raises `SynthError` when the plan has an unresolved or unnameable parameter,
    or when no definition of the unit is isolable in `source_text`.
    """
    pointers = _obligation_targets(plan)
    function = plan.unit.name
    # Anchor on the compiled definition's line (issue #145): an inactive `#if 0`
    # body of the same name ahead of it must not capture the injection point, or
    # the obligation lands in dead code cpp deletes and goes unchecked.
    brace = find_definition_brace(source_text, function, plan.unit.def_line)
    if brace is None:
        raise SynthError(f"no definition of {function}() found in the source text")
    if site_probe:
        checks = [f'__ESBMC_assert(0, "{OBLIGATION_SITE_LABEL_PREFIX}{function}");']
    else:
        checks = [
            f"__ESBMC_assert({obligation_expr(p)}, "
            f'"{OBLIGATION_LABEL_PREFIX}{function}:{p.var}");'
            for p in pointers
        ]
    injected = "".join(f" {check}" for check in checks)
    copy = source_text[: brace + 1] + injected + source_text[brace + 1 :]
    if source_path is None:
        return copy
    return _line_directive(source_path) + copy

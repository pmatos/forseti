"""List a C source's function definitions and their parameter types via ESBMC.

The verify-gate needs to know, for each function in an edited file, its name and
whether it takes a pointer/array parameter — today a brittle regex does this in
the Claude Code adapter (issue #131), which misreads comments, typedefs, and C's
adjusted function-type parameters. This module gets the answer from **ESBMC's own
clang frontend** instead: `esbmc <file> --parse-tree-only` dumps the clang AST
(no `main` needed, unlike the symbol-table/goto dumps), and the AST carries the
*canonical, typedef-resolved* type of every parameter. So `typedef void
(*cb_t)(void); void f(cb_t cb)` is correctly seen as pointer-taking, which no
purely syntactic method (regex or a syntactic parser) can do.

The cost is parsing clang's *textual* AST, whose format is not a stable API — it
is coupled to the pinned ESBMC/clang build. `parse_units` is kept pure and
separately tested against captured fixtures so a format drift surfaces as a test
failure rather than a silent misread. A future move to libclang (a real API, at
the cost of a dependency + include-path coupling) can swap in behind
`list_units` without changing its shape.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from forseti.esbmc.preprocessor import (
    _guard_macro_names,
    _line_breakpoints,
    _presumed_line,
    _stripped_for_scan,
)

# One AST node line: leading tree art, then `-Kind`, then the rest. The art is
# 2 columns per depth (`| ` / `  `, closed by `|-` / `` `- ``), so `len(art)//2`
# is a monotonic depth — only the relative order (child deeper than parent) is
# used, never an absolute value.
_NODE_RE = re.compile(r"^([ |`]*)-(\w+)\b(.*)$")

# The first source location in a node line: `<START, ...>` or `<START>`. START is
# `PATH:line:col`, `line:line:col`, `col:col`, or `<built-in>:...`.
_LOC_RE = re.compile(r"<([^,>]+)")

# One location token: `<built-in>:line:col`, `PATH:line:col`/`line:line:col`, or a
# same-line abbreviation `col:col`. `<built-in>` is spelled out (rather than
# `[^,<>]+?`) because it contains its own literal `<`/`>`, which would otherwise
# be mistaken for the enclosing range's brackets.
#
# `PATH` is matched lazily up to its `:line:col` suffix rather than as a
# whitespace-free run: clang echoes the input path verbatim, spaces included
# (`</tmp/path space/test.c:4:1, col:17>`), so excluding `\s` from the path
# class would leave a spaced path unmatched entirely — `_loc_line` would then
# silently keep its caller's line instead of this node's own (PR #156 follow-up
# to issue #145). The lazy `+?` still stops at the first `:digit:digit` it
# finds, so it does not overrun into a following `,`/`>`-delimited token — and
# `col:\d+` is tried *before* it, so a same-line abbreviation is never at risk
# of being swallowed as a (nonexistent) path prefix of some later token.
_LOC_TOKEN_RE = r"<built-in>:\d+:\d+|col:\d+|[^,<>]+?:\d+:\d+"

# A node's full location: `<start[, end]> point`. `point` is a `Decl`'s own
# identifier location, distinct from `start` — the range's beginning — whenever
# they fall on different lines (see `_loc_line`).
_LOC_CHAIN_RE = re.compile(
    rf"<({_LOC_TOKEN_RE})(?:,\s*({_LOC_TOKEN_RE}))?>\s*({_LOC_TOKEN_RE})?"
)

# The identifier a Decl names: the last word immediately before its `'type'`.
_NAME_RE = re.compile(r"\s(\w+)\s+'")

# Every quoted type on a line; a typedef'd param prints `'written':'canonical'`,
# so the *last* match is the canonical (typedef-resolved) type.
_TYPE_RE = re.compile(r"'([^']*)'")

# A `DeclRefExpr` naming a function: clang prints `Function 0x<addr> '<name>'`.
# Matches a direct call's callee and an address-of, which is why `Unit.calls` is
# documented as *referenced*, not *called* — an over-approximation.
_CALLEE_RE = re.compile(r"\bFunction 0x[0-9a-fA-F]+ '(\w+)'")

# Nodes C allows *between* a `CallExpr` and the `DeclRefExpr` naming its callee:
# clang's own `FunctionToPointerDecay` cast, and the parentheses/dereferences the
# grammar permits — `(f)(x)` adds a `ParenExpr`, `(*f)(x)` a `UnaryOperator` and a
# second cast. Each is transparent only as its parent's *first* child, which is
# what separates a callee from an argument: `apply(f, q)` wraps `f` in the same
# decay cast, at index 1.
_CALLEE_WRAPPERS = frozenset({"ImplicitCastExpr", "ParenExpr", "UnaryOperator"})

# The site name reported for an escape with no named enclosing declaration.
_FILE_SCOPE = "<file scope>"

# The symbol an attribute names, as clang prints it: `AliasAttr <loc> "target"`.
# The quoted text is a linker symbol, not a C identifier — GNU `__asm__` labels
# may carry punctuation a C name cannot (`__asm__("impl.sym")` prints unchanged
# as `AsmLabelAttr <loc> "impl.sym"`), so everything but the closing quote is
# captured, not just `\w`.
_TARGET_NAME_RE = re.compile(r'"([^"]+)"')

# The storage class clang prints *after* a declaration's quoted type — `static`,
# `extern`, and modifiers like `inline`. Read from the tail so a source path that
# happens to contain a `static` directory (printed before the type) cannot be
# misread as internal linkage.
_STATIC_STORAGE = "static"

# A named parameter's array declarator: the name followed by *all* its consecutive
# `[...]` groups, captured together (whitespace between them included) so a
# multi-dimensional `p[2][3]` — or a spaced `p[2] [3]` — is recognised as such
# rather than half-read as its first extent. A plain pointer has no bracket at all
# and simply does not match. Formatted with an `re.escape`d name.
_ARRAY_DECL_TEMPLATE = r"\b{name}((?:\s*\[[^\]]*\])+)"

# The type qualifiers C allows inside a *function parameter*'s array bracket
# (C99 adds them there; C11 adds `_Atomic`). They qualify the adjusted pointer —
# `int p[const]` is `int *const p` — and say nothing about an extent.
_BRACKET_QUALIFIER = r"const|volatile|restrict|_Atomic|__restrict(?:__)?"

# The contents of a fixed-array parameter's bracket, when they state a literal
# extent. C99's `static` and any qualifier may precede the extent, in either
# order (`int p[static const 20]`), so they are skipped; anything else in the
# bracket is not a literal we can read.
_EXTENT_RE = re.compile(rf"^\s*(?:(?:static|{_BRACKET_QUALIFIER})\s+)*(\d+)\s*$")

# A bracket holding qualifiers and nothing else (`int p[const]`, `int p[restrict]`):
# valid C that declares no extent at all. `static` is excluded on purpose — C's
# grammar requires a size expression after it, so a bracket that has `static` but
# no readable extent stays unresolved rather than passing as unsized.
_QUALIFIER_ONLY_RE = re.compile(
    rf"^\s*(?:{_BRACKET_QUALIFIER})(?:\s+(?:{_BRACKET_QUALIFIER}))*\s*$"
)

# C99's `[static N]`, whose `N` binds the *caller*: the argument must give access to
# at least N elements. Read separately from the extent because it stays meaningful
# even when N itself is not (`[static SHA_DIGEST_LENGTH]`).
_STATIC_MIN_RE = re.compile(r"\bstatic\b")

# A GNU suffix attribute's opening: ``__attribute__`` then its own ``(``. A
# definition may carry one or more of these between its declarator and body —
# ``static void f(int *p) __attribute__((noinline)) { ... }`` — which clang
# (and so ESBMC) accepts even though gcc requires attributes *before* the
# declarator in a definition (issue #163). Matched separately from the ``(``
# it opens so `_skip_suffix_attributes` can balance that parenthesis on its
# own — its argument list can itself nest parens (``__attribute__((aligned(8)))``).
_ATTRIBUTE_RE = re.compile(r"__attribute__\s*\(")


@dataclass(frozen=True)
class Param:
    """One parameter: its name, canonical type, and fixed-array extent (if any).

    `array_extent` is the ``N`` of a parameter *written* as a fixed array
    ``T p[N]`` — information clang's canonical type has already thrown away
    (``T p[20]`` is *adjusted* to ``T *``, so the size is unrecoverable from the
    type alone). It is harvested from the source declarator by `list_units`
    (`annotate_array_extents`) and is ``None`` for a plain pointer, an unsized
    ``T p[]``, a scalar, or when the source could not be read. The memory-
    precondition synthesizer (RFC-0003 S2) uses it to size ``T p[N]`` objects.

    `array_extent_unresolved` marks the case that ``array_extent = None`` alone
    cannot express: the parameter *is* written as a fixed array, but its extent is
    not readable from the source — a macro or expression (``T p[SHA_DIGEST_LENGTH]``,
    which needs the preprocessor) or a multi-dimensional declarator. Sizing such a
    parameter as a single object would under-allocate it, so the synthesizer reports
    ``NEEDS_CONTRACT`` instead of a phantom violation (issue #137).

    `array_static_min` records that the declarator used C99's ``T p[static N]``,
    where ``N`` is a *caller obligation*: the argument must give access to at least
    ``N`` elements, so the function may touch all of them no matter what any other
    parameter says. That makes it distinct from the merely conventional ``T p[N]``,
    and it is set whether or not ``N`` itself was readable.
    """

    name: str
    type: str
    array_extent: int | None = None
    array_extent_unresolved: bool = False
    array_static_min: bool = False

    @property
    def is_pointer(self) -> bool:
        """True if the canonical type is a pointer or array.

        Clang adjusts array and function parameter types to pointers, so a
        pointer parameter's canonical type always renders with ``*`` (``T *``,
        ``ret (*)(...)``); ``[`` is kept as belt-and-suspenders for a
        pointer-to-array (``int (*)[10]``).
        """
        return "*" in self.type or "[" in self.type


@dataclass(frozen=True)
class Unit:
    """A function *definition* found in the source: its name and parameters.

    `calls` names the functions this definition's body *references* — every
    callee of a direct call, plus any function whose address is merely taken
    (both print the same ``Function 0x… 'name'`` reference in the AST). It is
    therefore an over-approximation, which is the safe direction for its user:
    compositional discharge (RFC-0003 S3) verifies every caller of a unit, so a
    spurious edge costs one extra verification while a missing one would let an
    undischarged obligation pass as discharged.

    What it is *not* is a call graph closed under function pointers: a body that
    calls through ``fp`` references the variable, not what it holds. That blind
    spot is reported separately by `parse_address_escapes`, not papered over here.

    `internal_linkage` is True when some declaration of this name in the
    translation unit is marked ``static``, i.e. the TU is the whole world for it.
    Compositional discharge (RFC-0003 S3) needs that to know whether "every caller
    in this TU" is every caller at all. It reads the marker and nothing else, so a
    definition whose ``static`` is not printed reads as external — which costs a
    withheld discharge, never a claimed one.

    `def_line` is the 1-based line of the definition clang actually compiled, as
    reported by its own AST location — set by `parse_definitions`, ``None`` for a
    hand-built `Unit` (tests). `annotate_array_extents` anchors its declarator
    search to this line so a `#if 0`/`#ifdef`-excluded alternative body with the
    same name cannot donate its array shape to the active definition (issue #145).

    `predefined_guards` says, for each conditional-guard name in the source,
    whether it was defined *before the translation unit's first line* — the one
    fact a textual scan of the file can never derive, because it is set by the
    build command (``-D``) and by the compiler's own builtins, not by the file
    (issue #226). `list_units` reads it back from the preprocessor itself
    (`probe_predefined_guards`), in the same call and under the same binary and
    flags as the parse that set `def_line`, so the two are measured together;
    every consumer of `def_line` (the extent anchor here, the
    obligation-injection anchor in `forseti.precond.synth`) passes it down so
    `_line_breakpoints` can decide guards this file leaves open. Being measured
    together is all this field promises: a consumer that re-reads the source
    after the listing carries a `def_line` from that same listing and is stale
    in exactly the same way and to exactly the same degree.

    It ranges over guard names only, never over every macro, and it says nothing
    about state *below* an ``#include``: a header may redefine anything, and
    `_INCLUDE_RE` still drops the whole seed there. Empty for a hand-built
    `Unit`, for a source with no guards, whenever the probe could not run, and
    — the common case — whenever `list_units` skipped the probe because no
    unit's name had a second definition-shaped occurrence for the seed to
    choose between (`_with_predefined_guards`). ``()`` therefore says "not
    measured", never "nothing was defined": all four fall back to the
    assumed-live default that predates this field.
    """

    name: str
    params: tuple[Param, ...]
    calls: tuple[str, ...] = ()
    internal_linkage: bool = False
    def_line: int | None = None
    predefined_guards: tuple[tuple[str, bool], ...] = ()

    @property
    def takes_pointer(self) -> bool:
        """True if any parameter is a pointer/array (the gate's NEEDS_CONTRACT test)."""
        return any(p.is_pointer for p in self.params)


class ListUnitsError(RuntimeError):
    """ESBMC could not be invoked (missing binary, etc.) — distinct from "no units"."""


def _loc_file(rest: str, current: str) -> str:
    """The file a node line refers to, tracking clang's abbreviated locations.

    Clang prints a full ``PATH:line:col`` only when the file changes, then
    abbreviates to ``line:``/``col:`` for following nodes in the same file. So a
    bare ``line``/``col`` inherits `current`; anything else names a new file
    (a path, a header, or ``<built-in>``).

    Reads the range's *start*, unlike `_loc_line` (which reads `point`): file
    identity does not move within a same-file macro expansion, and a macro whose
    own definition lives in a *different* file (a header) still drops the unit
    via `_is_target`, so the asymmetry is harmless for every case this module
    handles.
    """
    match = _LOC_RE.search(rest)
    if not match:
        return current
    head = match.group(1).split(":", 1)[0]
    if head in ("line", "col"):
        return current
    return head


def _token_line(token: str | None, inherited: int) -> int:
    """`token`'s line, or `inherited` if `token` is absent or a bare ``col:N``.

    Every other `_LOC_TOKEN_RE` alternative ends in a colon-separated digit run
    for line and column, so the second-to-last split component is always a
    decimal string here — no fallback needed.
    """
    if token is None or token.startswith("col:"):
        return inherited
    return int(token.split(":")[-2])


def _loc_line(rest: str, current: int) -> int:
    """The 1-based source line a node's own location — not its range's *start* —
    refers to.

    A node line reads ``<start[, end]> point ...``; `point` is a `Decl`'s own
    identifier location, printed right after the range and, like `end`, inherits
    its line from the location before it when abbreviated to a bare ``col:N``
    (mirroring `_loc_file`).

    Deliberately reads `point`, not `start`: they diverge when a `FunctionDecl`'s
    return type is a macro defined earlier in the file — clang's range then
    *starts* at the macro's spelling location, but `point` still names the
    function identifier's real line. Anchoring `Unit.def_line` on `start` would
    pick up the macro definition's line instead (issue #145 follow-up).
    """
    match = _LOC_CHAIN_RE.search(rest)
    if not match:
        return current
    start_line = _token_line(match.group(1), current)
    end_line = _token_line(match.group(2), start_line)
    return _token_line(match.group(3), end_line)


def _depth(art: str) -> int:
    return len(art) // 2


def _error_line(text: str) -> str:
    """The first ``ERROR:``-prefixed line, so a failed run is self-describing."""
    for line in text.splitlines():
        if line.startswith("ERROR:"):
            return line[len("ERROR:") :].strip()
    return ""


@dataclass
class _AstNode:
    """One node of the walked AST, with its position among its parent's children."""

    depth: int
    kind: str
    rest: str
    index: int
    children: int = 0


@dataclass
class _OpenFunction:
    """A ``FunctionDecl`` being assembled, and the nodes seen under it so far."""

    name: str | None
    depth: int
    file: str
    def_line: int | None = None
    params: list[Param] = field(default_factory=list)
    calls: dict[str, None] = field(default_factory=dict)  # an ordered set
    is_definition: bool = False


def _walk(ast_text: str) -> Iterator[tuple[list[_AstNode], str, str, str, int]]:
    """Every node of `ast_text`, with its ancestor stack, current file, and line.

    The single traversal every parser here shares: it yields the node's stack
    (root first, the node itself last, each entry knowing its position among its
    parent's children) so a parser can ask about *structure* — who encloses this,
    is it the first child — rather than guessing from a line in isolation.

    The current *presumed* line (`_loc_line`) is carried alongside the current
    file for the same reason the file is: a node's own location can be an
    abbreviated ``col:N`` that inherits both from whatever came before it. Read
    by `parse_definitions` to set `Unit.def_line` (issue #145).
    """
    stack: list[_AstNode] = []
    current_file = ""
    current_line = 0
    for line in ast_text.splitlines():
        node = _NODE_RE.match(line)
        if not node:
            continue
        art, kind, rest = node.group(1), node.group(2), node.group(3)
        depth = _depth(art)
        current_file = _loc_file(rest, current_file)
        current_line = _loc_line(rest, current_line)
        while stack and stack[-1].depth >= depth:
            stack.pop()
        index = 0
        if stack:
            index = stack[-1].children
            stack[-1].children += 1
        stack.append(_AstNode(depth, kind, rest, index))
        yield stack, kind, rest, current_file, current_line


def parse_definitions(ast_text: str) -> list[tuple[str, Unit]]:
    """Every function *definition* in an ``esbmc --parse-tree-only`` dump.

    Walks the textual AST tracking the current file and pairs each definition with
    the file it came from — the whole *translation unit*, headers included, not
    just the file that was passed to esbmc. For each ``FunctionDecl`` it collects
    the immediate ``ParmVarDecl`` children (with their canonical types) and every
    function the subtree references (`Unit.calls`), keeping it only if the subtree
    contains a ``CompoundStmt`` — i.e. it is a definition, not a prototype.

    Neither filtered nor deduped: `parse_units` narrows this to the file under
    test (what the gate can verify), while `parse_external_callers` needs exactly
    the part `parse_units` drops.

    ``static`` is harvested from *every* declaration of a name, not just the
    definition: clang prints the storage class on the declaration that carried it,
    so ``static void f(int *); void f(int *p) {}`` marks only the prototype.

    Also records `Unit.def_line` — the definition's own presumed line, from
    `_walk`'s `_loc_line` tracking — on every ``FunctionDecl`` (cheap to always
    capture; only definitions survive to `found` anyway). `annotate_array_extents`
    anchors its declarator search to it so a ``#if 0``/``#ifdef``-excluded
    alternative body with the same name cannot donate its array shape to the
    active definition (issue #145).

    Function declarations **nest** — C allows one at block scope
    (``void g(void) { extern void h(void); f(p); }``) and GNU C a whole nested
    definition — so the open declarations are a *stack*. Closing the enclosing
    ``g`` at the inner one, as a single slot would, silently drops every call
    written after it, and a dropped call edge is a caller the discharge never
    sees.
    """
    found: list[tuple[str, Unit]] = []
    internal: set[str] = set()
    open_fns: list[_OpenFunction] = []

    def close(down_to: int) -> None:
        """Finish every open declaration the node at `down_to` is not inside."""
        while open_fns and open_fns[-1].depth >= down_to:
            fn = open_fns.pop()
            if fn.name and fn.is_definition:
                found.append(
                    (
                        fn.file,
                        Unit(
                            fn.name,
                            tuple(fn.params),
                            tuple(fn.calls),
                            def_line=fn.def_line,
                        ),
                    )
                )

    for stack, kind, rest, file, line in _walk(ast_text):
        depth = stack[-1].depth
        close(depth)
        if kind == "FunctionDecl":
            name_match = _NAME_RE.search(rest)
            name = name_match.group(1) if name_match else None
            if name and _STATIC_STORAGE in rest.rsplit("'", 1)[-1].split():
                internal.add(name)
            open_fns.append(_OpenFunction(name, depth, file, def_line=line))
            continue
        if not open_fns:
            continue
        fn = open_fns[-1]
        if kind == "ParmVarDecl" and depth == fn.depth + 1:
            types = _TYPE_RE.findall(rest)
            name_match = _NAME_RE.search(rest)
            fn.params.append(
                Param(
                    name_match.group(1) if name_match else "",
                    types[-1] if types else "",
                )
            )
        elif kind == "CompoundStmt":
            fn.is_definition = True
        elif kind == "DeclRefExpr":
            callee = _CALLEE_RE.search(rest)
            if callee is not None:
                fn.calls[callee.group(1)] = None

    close(0)
    return [(f, replace(u, internal_linkage=u.name in internal)) for f, u in found]


def _is_target(file: str, source_norm: str) -> bool:
    """Whether a definition's clang location names `source`.

    A normalized *full-path* match (clang echoes the input path verbatim), so a
    definition from a same-basename ``#include``\\ d file in another directory is
    not misread as belonging to `source`. An empty location is never the target.
    """
    return bool(file) and os.path.normpath(file) == source_norm


def parse_units(ast_text: str, source: str | Path) -> list[Unit]:
    """Function definitions in `source` from an ``esbmc --parse-tree-only`` dump.

    `parse_definitions` narrowed to the file under test — a header of
    declarations yields nothing, matching what the gate can verify. Deduped by
    name, first definition wins.
    """
    source_norm = os.path.normpath(str(source))
    units: list[Unit] = []
    seen: set[str] = set()
    for file, unit in parse_definitions(ast_text):
        if not _is_target(file, source_norm) or unit.name in seen:
            continue
        seen.add(unit.name)
        units.append(unit)
    return units


def parse_external_callers(
    ast_text: str, source: str | Path, symbol: str
) -> tuple[str, ...]:
    """Definitions **outside** `source` that reference `symbol`, by name.

    The blind spot `parse_units` has by construction: a ``static inline`` in an
    included header is part of the same translation unit and can call `symbol`,
    but it is not a unit the gate enumerates. Compositional discharge (RFC-0003
    S3) has to know they exist — it cannot claim every caller in the TU was
    checked while some were never even listed — so it counts them and withholds
    the upgrade rather than quietly ignoring them.
    """
    source_norm = os.path.normpath(str(source))
    names: dict[str, None] = {}
    for file, unit in parse_definitions(ast_text):
        if _is_target(file, source_norm) or unit.name == symbol:
            continue
        if symbol in unit.calls:
            names[unit.name] = None
    return tuple(names)


def _is_call_callee(stack: list[_AstNode]) -> bool:
    """Whether the node on top of `stack` is the callee of a direct call.

    Climbs the first-child chain of `_CALLEE_WRAPPERS` above the reference and
    asks whether it lands on a ``CallExpr``'s first child. Anything else — an
    argument, an initialiser, an ``&f``, an AST shape we have not seen — is *not*
    a direct call, which is the conservative reading: an unfamiliar shape costs a
    withheld discharge, never a claimed one.
    """
    k = len(stack) - 1
    while k > 0 and stack[k].index == 0 and stack[k - 1].kind in _CALLEE_WRAPPERS:
        k -= 1
    return k > 0 and stack[k].index == 0 and stack[k - 1].kind == "CallExpr"


def _enclosing_name(stack: list[_AstNode]) -> str:
    """What to call the site of the node on top of `stack`.

    The nearest enclosing named declaration: the function a reference sits in,
    or — at file scope — the object whose initialiser holds it (``static cb_t fp
    = f``). Reporting only, so a shape with no named ancestor degrades to
    `_FILE_SCOPE` rather than dropping the escape.
    """
    for node in reversed(stack[:-1]):
        if node.kind in ("FunctionDecl", "VarDecl"):
            name = _NAME_RE.search(node.rest)
            return name.group(1) if name else _FILE_SCOPE
    return _FILE_SCOPE


def parse_address_escapes(ast_text: str, symbol: str) -> tuple[str, ...]:
    """Sites naming `symbol` **outside a direct call**, by enclosing declaration.

    A function whose address is stored in a pointer can be invoked through it
    from anywhere in the translation unit, and an indirect ``fp(...)`` names only
    the *variable* — so no `Unit.calls` edge leads back to `symbol` and the caller
    enumeration silently misses that path. This reports those sites so
    compositional discharge (RFC-0003 S3) can withhold its upgrade instead:
    ``static cb_t fp = f;`` at file scope yields ``fp``, an ``fp = f`` inside a
    body yields the enclosing function.

    The test is deliberately on *position*, not on node kind: clang prints the
    same ``Function 0x… 'name'`` reference wherever a function is named without
    being called, so ``char c __attribute__((cleanup(f)))`` — a call at scope exit
    that no expression in the AST spells — is caught by the same rule as an
    address-of, and so is any attribute of that shape we have not seen yet.

    What that rule cannot catch is an attribute on the callee's **own**
    declaration rather than a reference sitting elsewhere — a
    ``constructor``/``destructor`` attribute prints no ``Function 0x… 'name'``
    text at all, since it names no one; the loader invokes the callee directly.
    `parse_implicit_invocations` covers that shape.

    A *direct* call is not an escape, however it is written (`_is_call_callee`),
    including the recursive call in ``symbol``'s own body — otherwise every
    recursive unit would be permanently undischargeable.
    """
    sites: dict[str, None] = {}
    for stack, _kind, rest, _file, _line in _walk(ast_text):
        referenced = _CALLEE_RE.search(rest)
        if referenced is None or referenced.group(1) != symbol:
            continue
        if not _is_call_callee(stack):
            sites[_enclosing_name(stack)] = None
    return tuple(sites)


# `kind` -> the human word for the AST node clang emits directly on a function's
# own `FunctionDecl` when a `constructor`/`destructor` attribute marks it: the
# loader calls it at load/unload time, with none of the arguments any call site
# in this translation unit supplies.
_IMPLICIT_INVOCATION_ATTRS = {
    "ConstructorAttr": "constructor",
    "DestructorAttr": "destructor",
}


def parse_implicit_invocations(ast_text: str, symbol: str) -> tuple[str, ...]:
    """Constructor/destructor attributes on `symbol`'s **own** declaration.

    ``__attribute__((constructor))``/``destructor`` is unlike every other
    attribute-borne path this module follows: it names no one. It sits on the
    callee's own ``FunctionDecl`` as a bare ``ConstructorAttr``/``DestructorAttr``
    child, with no ``Function 0x… 'name'`` reference anywhere — so neither
    `parse_address_escapes` (which keys off that reference shape) nor
    `parse_symbol_aliases` (a second *name*, not a second *invoker*) can see it.
    The loader calls the function directly at load/unload time, supplying none
    of the arguments any explicit caller in this translation unit does, so an
    otherwise-clean sweep of explicit callers says nothing about that
    invocation. Compositional discharge (RFC-0003 S3) withholds the upgrade
    instead of claiming a caller set that omits it.
    """
    found: dict[str, None] = {}
    for stack, kind, _rest, _file, _line in _walk(ast_text):
        label = _IMPLICIT_INVOCATION_ATTRS.get(kind)
        if label is not None and _enclosing_name(stack) == symbol:
            found[label] = None
    return tuple(found)


def _declared_name(rest: str) -> str:
    """The identifier a declaration line names, or ``""`` when it names none."""
    found = _NAME_RE.search(rest)
    return found.group(1) if found else ""


def parse_symbol_aliases(ast_text: str, symbol: str) -> tuple[str, ...]:
    """Declarations that are **another name** for `symbol`, by name.

    GNU C's ``static void fa(int *) __attribute__((alias("f")))`` makes ``fa``
    and ``f`` the same function at link time, but a call to ``fa`` prints a
    reference to ``fa`` alone: nothing in the AST joins that call site to ``f``,
    so the caller enumeration cannot see it. Compositional discharge (RFC-0003 S3)
    therefore treats an alias as a caller path it cannot follow and withholds the
    upgrade, rather than claiming a set of callers that was complete only for one
    of the function's names.

    An **assembly label** is the same identity by a different route: what the
    linker joins is a declaration's *symbol* name, which is its ``__asm__`` label
    when it has one and its C name otherwise. So the comparison is between those
    effective names — which catches two declarations sharing an explicit label,
    and equally ``void fa(int *) __asm__("f")`` next to a plain ``f``, where only
    one side carries a label at all.

    The two mechanisms also combine: ``alias("impl.sym")`` names a *linker*
    symbol, so when `symbol` itself carries ``__asm__("impl.sym")`` the alias
    that actually links to it names that label, not `symbol`'s bare C name —
    matching only the literal name would miss it. `AliasAttr` targets are
    therefore compared against the same effective-symbol set (`symbol`'s own
    label, or `symbol` itself when it has none) the label loop below computes,
    not against `symbol` alone.

    What this does *not* cover is a function named by an attribute rather than
    aliased by one — a ``cleanup`` handler, say. That is `parse_address_escapes`'
    job: clang prints those as an ordinary ``Function 0x… 'name'`` reference, and
    a reference outside a direct call already opens the caller set. A
    ``constructor``/``destructor`` attribute is neither a second name nor a
    reference — it sits on `symbol`'s own declaration with the loader as the
    invoker; `parse_implicit_invocations` covers that.
    """
    aliases: dict[str, None] = {}
    labels: dict[str, set[str]] = {}
    alias_targets: dict[str, set[str]] = {}
    for stack, kind, rest, _file, _line in _walk(ast_text):
        if kind == "FunctionDecl":
            labels.setdefault(_declared_name(rest), set())
        elif kind == "AliasAttr":
            alias_targets.setdefault(_enclosing_name(stack), set()).update(
                _TARGET_NAME_RE.findall(rest)
            )
        elif kind == "AsmLabelAttr":
            labels.setdefault(_enclosing_name(stack), set()).update(
                _TARGET_NAME_RE.findall(rest)
            )
    target = labels.get(symbol) or {symbol}
    effective_target = target | {symbol}
    for name, targets in alias_targets.items():
        if targets & effective_target:
            aliases[name] = None
    for name, own in labels.items():
        if name != symbol and (own or {name}) & target:
            aliases[name] = None
    return tuple(aliases)


def parse_asm_statements(ast_text: str, symbol: str) -> tuple[str, ...]:
    """Sites in the translation unit that GNU inline assembly might route to `symbol`.

    A `GCCAsmStmt` can invoke any symbol in the translation unit by name
    (``asm("call f")``), with no ``DeclRefExpr`` and hence no `Unit.calls` edge
    and no `parse_address_escapes` site for it — exactly the gap this covers.
    But *which* symbol, if any, a given block invokes is unrecoverable from this
    format: verified against both esbmc's pinned clang and a current upstream
    clang, a `GCCAsmStmt` node's only children are its operand expressions
    (``result``, ``x`` for ``asm("..." : "=a"(result) : "D"(x))``); the asm
    string itself is never printed. There is therefore no substring to compare
    against `symbol`, so every block found is reported unconditionally, naming
    the declaration that encloses it — the only reading that does not silently
    drop a call path this scan cannot see at all.

    Swept over the *whole* translation unit, like `parse_address_escapes`,
    `parse_symbol_aliases` and `parse_implicit_invocations` — not narrowed to
    one file the way `parse_units` is. A function *defined in an included
    header* can contain ``asm("call f")`` exactly as one defined in the file
    under test can, and the block leaves no reference of any kind for anything
    else in this module to catch by name instead (issue #167 follow-up); a
    scan that only looked at the file under test would silently drop that
    call path, which is unsound, not merely incomplete. The cost is real: a
    translation unit that happens to pull in a header with unrelated inline
    asm (``sys/io.h``'s port-I/O wrappers, measured at 18 `GCCAsmStmt` nodes
    for that one header alone) now permanently withholds discharge for every
    function in it — the honest price of a format that never prints what a
    `GCCAsmStmt` actually invokes.

    A file-scope ``asm(...)`` (``FileScopeAsmDecl``) is narrower: unlike
    `GCCAsmStmt`, clang prints its full text as a ``StringLiteral`` child
    (verified with a live `esbmc --parse-tree-only` run — the text is not
    truncated even across the embedded newlines a multi-line block like
    ``asm("wrapper:\\n\\tcall f\\n\\tret")`` prints escaped, not literal, nor at
    an embedded ``\\"`` — though that one *would* truncate the match, since
    `_TARGET_NAME_RE` stops at the first quote it sees). A block that only
    renames or declares a symbol unrelated to `symbol` does not have to cost a
    withheld discharge, so this reports one only when `symbol` appears in it as
    a whole word — the reading a hand-written assembly function that itself
    calls ``f`` would need. It sits outside any function, so `_enclosing_name`
    reports it as `_FILE_SCOPE`.

    This narrowing is real code with real unit and fake-injected coverage, but
    it does not yet run against an actual translation unit: esbmc 8.3.0's
    frontend cannot *convert* a ``FileScopeAsmDecl`` at all (only dump it under
    ``--parse-tree-only``), so any TU containing one fails S2 verification
    outright before `parse_asm_statements` is ever called for real — see
    `discharge_precondition`'s module docstring and
    `test_file_scope_asm_fails_esbmc_conversion_before_discharge_runs`.
    """
    target = re.compile(r"\b" + re.escape(symbol) + r"\b")
    sites: dict[str, None] = {}
    for stack, kind, rest, _file, _line in _walk(ast_text):
        if kind == "GCCAsmStmt":
            sites[_enclosing_name(stack)] = None
        elif (
            kind == "StringLiteral"
            and len(stack) > 1
            and stack[-2].kind == "FileScopeAsmDecl"
        ):
            text = _TARGET_NAME_RE.search(rest)
            if text and target.search(text.group(1)):
                sites[_enclosing_name(stack)] = None
    return tuple(sites)


def _skip_suffix_attributes(text: str, pos: int) -> int:
    """`pos`, advanced past every literal ``__attribute__((...))`` starting there.

    `pos` must already be past any whitespace. Each attribute's own
    parentheses are balanced independently — its argument list can nest them
    (``__attribute__((aligned(8)))``) — and whitespace after it is skipped
    before looking for a next one, so ``f(int *p) __attribute__((noinline))
    __attribute__((used)) {`` is skipped in full. Stops, returning `pos`
    unchanged, at the first unbalanced attribute — the caller's own ``{``
    check then fails on it, the same fail-closed outcome as today.

    Textual and literal on purpose, like the rest of this scan: a macro that
    expands to an attribute (``#define NOINLINE __attribute__((noinline))``,
    then ``f(int *p) NOINLINE {``) is not recognised and cannot be by a scan
    with no preprocessor — that definition still misses, exactly as before
    this function existed.
    """
    while True:
        match = _ATTRIBUTE_RE.match(text, pos)
        if match is None:
            return pos
        depth = 0
        j = match.end() - 1  # index of the attribute's own '('
        while j < len(text):
            char = text[j]
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        else:
            return pos  # unbalanced — leave position at the attribute
        pos = j + 1
        while pos < len(text) and text[pos].isspace():
            pos += 1


@dataclass(frozen=True)
class _DefinitionMatch:
    """One definition-shaped occurrence of a name: its line, its ``{``, its params.

    ``line`` is the 1-based *physical* line of the ``fn_name (`` token, which
    `_select_definition` translates to a presumed coordinate before it anchors on
    `def_line`. ``brace`` indexes the body's ``{``; ``params`` is the declarator
    text between ``(`` and its matching ``)``. ``name_start`` indexes the ``fn_name``
    token itself (the regex match's own start -- not re-derived by a later,
    independent search, which is what let a string literal mentioning `fn_name`
    after the real token, e.g. an ``__attribute__((annotate("main")))`` suffix,
    get renamed instead of it). A body opener and a parameter list are the same
    occurrence seen two ways, so both entry points read them off this.
    """

    line: int
    brace: int
    params: str
    name_start: int


def _preceded_by_member_access(text: str, start: int) -> bool:
    """True if `text[start:]` (an identifier) is immediately preceded, past
    whitespace, by ``.`` or ``->``.

    C has no syntax that puts a name-qualifying operator directly before a
    call other than struct/union member access or a pointer-to-struct
    dereference — so ``s.main(`` or ``p->main(`` can only be a member named
    `fn_name`, never a reference to the free function of the same name, and
    must not be treated like one by `_paren_balanced_occurrences`'s callers.
    """
    i = start
    while i > 0 and text[i - 1].isspace():
        i -= 1
    if i > 0 and text[i - 1] == ".":
        return True
    return i > 1 and text[i - 2 : i] == "->"


def _paren_balanced_occurrences(
    source_no_comments: str, fn_name: str
) -> Iterator[tuple[re.Match[str], int, int]]:
    """Every ``fn_name (`` occurrence, balanced-paren scanned to its matching ``)``.

    Yields ``(match, close_idx, after_idx)``: `match` is the ``fn_name (`` regex
    match, `close_idx` the matching ``)``'s index, and `after_idx` the first
    index past it once whitespace and any suffix attributes are skipped. An
    occurrence with no matching close is skipped (unbalanced — not a usable
    signature), and so is one that is a struct/pointer member access
    (`_preceded_by_member_access`) — never a reference to `fn_name` itself.

    The single scanner behind both `_definition_candidates` (filters on a
    trailing ``{``) and `_declaration_name_starts` (filters on anything else,
    including a bare reference in expression position) — keeping one scan is
    what stops the #145 anchor from reaching one caller and not the other.
    """
    for match in re.finditer(rf"\b{re.escape(fn_name)}\s*\(", source_no_comments):
        if _preceded_by_member_access(source_no_comments, match.start()):
            continue
        depth = 0
        j = match.end() - 1  # index of the '('
        while j < len(source_no_comments):
            char = source_no_comments[j]
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        else:
            continue  # unbalanced — not a usable signature
        k = j + 1
        while k < len(source_no_comments) and source_no_comments[k].isspace():
            k += 1
        k = _skip_suffix_attributes(source_no_comments, k)
        yield match, j, k


def _definition_candidates(
    source_no_comments: str, fn_name: str
) -> list[_DefinitionMatch]:
    """Every definition-shaped occurrence of `fn_name`, in textual order.

    An occurrence whose ``)`` is followed (past whitespace and any suffix
    attributes) by ``{`` is *definition-shaped* — so a prototype (``);``) or a
    call site (``) ;``, ``))``) is skipped. Deliberately narrow: clang already
    told us the canonical types and which parameters are pointers, so this only
    has to isolate the declarator text to harvest an array extent from, and the
    ``{`` to inject an obligation after — never to classify a type.
    """
    matches: list[_DefinitionMatch] = []
    for match, j, k in _paren_balanced_occurrences(source_no_comments, fn_name):
        if k < len(source_no_comments) and source_no_comments[k] == "{":
            line = source_no_comments.count("\n", 0, match.start()) + 1
            matches.append(
                _DefinitionMatch(
                    line, k, source_no_comments[match.end() : j], match.start()
                )
            )
    return matches


def _select_definition(
    matches: list[_DefinitionMatch],
    source_no_comments: str,
    def_line: int | None,
    predefined: Sequence[tuple[str, bool]] = (),
) -> _DefinitionMatch | None:
    """The definition-shaped occurrence `def_line` anchors to, or the first.

    Textual order alone cannot tell two definition-shaped occurrences of the same
    name apart — e.g. an inactive ``#if 0`` body ahead of the one clang actually
    compiled (issue #145). When `def_line` is given (the compiled definition's
    line, from `Unit.def_line`), the occurrence at or nearest *after* that line is
    preferred over one before it — `def_line` points at the declaration's opening
    line, which the ``fn_name (`` text can follow by a line or two (a return type
    on its own line). Without `def_line` the first definition-shaped occurrence is
    used, as before.

    A sole candidate is returned without consulting the breakpoints at all: there
    is nothing for `def_line` or `predefined` to choose between, so neither can
    change the answer. Stated here rather than left to be re-derived, because
    `_with_predefined_guards`' cost gate is exactly this property read backwards —
    it skips the probe for a listing whose every name has one candidate.

    `def_line` is clang's *presumed* line — the coordinate a ``#line``/linemarker
    directive in the source can rewrite away from physical line count. Comparing
    it against a raw physical line count would silently anchor to the wrong
    occurrence whenever such a directive is present, so each candidate's physical
    line is first translated to the same presumed coordinate system before the
    comparison (issue #145 follow-up: a directive after an inactive ``#if 0`` body
    can make the active definition's presumed line collide with, or fall behind,
    the inactive one's physical line).

    Two candidates can still translate to the *same* presumed line even after
    `_line_breakpoints` excludes a ``#line`` sitting inside a literal ``#if 0``/
    ``#elif 0`` branch — e.g. two directives outside any conditional at all, each
    immediately ahead of a definition-shaped occurrence. `min`'s stability would
    otherwise silently keep whichever candidate is textually first. As a last
    resort, ties are broken by physical line, preferring the later occurrence —
    consistent with this anchor's own default assumption (issue #145): an
    inactive alternative more often sits *before* the active one than after.

    A duplicate directive inside a ``#ifdef``/``#ifndef``/``#if defined(...)``
    is only sometimes this case. `_line_breakpoints` excludes it whenever the
    guard's state is proven — by this file's own ``#define``/``#undef``
    directives (issue #157) or by `predefined`, the definedness the command line
    and the compiler's builtins gave each guard name before the first line
    (issue #226). Passing `predefined` is what lets both compiles of the same
    ``-D``-selected source pick their own definition; without it (a caller with
    no build flags to measure under) such a guard reads opaque and the tiebreak
    below decides, which is right for only one of the two physical orderings.

    What stays a residual either way is a guard an ``#include``d header decides:
    `_line_breakpoints` drops every definedness fact at an ``#include``, seed
    included, because a header's effect depends on where it is included and no
    probe of a separate translation unit reproduces that.
    """
    if not matches:
        return None
    if def_line is None or len(matches) == 1:
        return matches[0]
    breakpoints = _line_breakpoints(source_no_comments, predefined)
    # Sort key: candidates at or after `def_line` (key[0] == False) all sort before
    # any that are only before it, then nearest wins within each group — "at or
    # nearest after", falling back to nearest-before only if none qualify. `-m.line`
    # (physical line) only breaks a tie left by the first two components — two
    # candidates translated to the same presumed distance from `def_line`.
    return min(
        matches,
        key=lambda m: (
            _presumed_line(m.line, breakpoints) < def_line,
            abs(_presumed_line(m.line, breakpoints) - def_line),
            -m.line,
        ),
    )


def find_definition_brace(
    source_text: str,
    fn_name: str,
    def_line: int | None = None,
    predefined: Sequence[tuple[str, bool]] = (),
) -> int | None:
    """Index of the ``{`` opening `fn_name`'s definition body in `source_text`.

    ``None`` when no definition of that name is isolable (a prototype-only
    declaration, a name that appears solely at call sites, an unbalanced
    signature). The index is into the **original** text — comments are masked
    length-preservingly first, so a ``{`` inside a comment cannot be mistaken for
    the body's while the offset stays usable for injection (RFC-0003 S3).

    `def_line` (the compiled definition's presumed line, `Unit.def_line`) anchors
    the choice when the same name has more than one definition-shaped occurrence:
    an inactive ``#if 0`` body ahead of the compiled one must not capture the
    injection point, or an obligation would land in dead code cpp deletes and go
    unchecked (issue #145; the anchor lives in `_select_definition`).

    `predefined` (`Unit.predefined_guards`) carries the one input that anchor
    cannot read off the text — which guards the build flags and the compiler's
    builtins had already defined (issue #226). Omitting it is safe but weaker:
    every such guard reads live, exactly as it did before that field existed.
    """
    masked = _stripped_for_scan(source_text)
    chosen = _select_definition(
        _definition_candidates(masked, fn_name), masked, def_line, predefined
    )
    return None if chosen is None else chosen.brace


def _declaration_name_starts(source_no_comments: str, fn_name: str) -> list[int]:
    """Every non-definition (prototype, bare call statement, or a reference in
    expression position) `fn_name`'s own name-span start.

    Shares `_paren_balanced_occurrences` with `_definition_candidates`, but
    applies the opposite filter: anything *other* than a `{` (not just a `;`)
    after the closing `)` and any suffix attributes — ``int fn_name(...);``,
    a standalone call ``fn_name(...);``, or a reference in expression position
    like ``return fn_name() + 1;`` or ``int x = fn_name();`` (issue #95
    review: the original `;`-only filter left an expression-position
    reference to a renamed definition dangling, which can silently collide
    with a harness's own injected same-named entry point instead of erroring
    loudly). A separate filter rather than widening `_definition_candidates`
    itself: that one anchors RFC-0003 S3's obligation injection and #145's
    `def_line` disambiguation, both of which want *only* a definition — a
    prototype or reference is exactly what they must keep excluding.

    Catching a plain call statement or expression-position reference too (not
    just a genuine prototype) is deliberate, not just tolerated: renaming a
    same-file reference alongside every definition of the same name keeps
    both consistent (a reference renamed without its definition, or vice
    versa, would be the real bug) — `rename_all_declarations_and_definitions`'s
    caller only ever runs this against a single translation unit, so any
    non-definition occurrence of `fn_name(...)` refers to the one declaration
    this scan is renaming away either way.
    """
    starts: list[int] = []
    for match, _j, k in _paren_balanced_occurrences(source_no_comments, fn_name):
        if k >= len(source_no_comments) or source_no_comments[k] != "{":
            starts.append(match.start())
    return starts


def rename_all_declarations_and_definitions(
    source_text: str, fn_name: str, new_name: str
) -> str:
    """`source_text` with **every** declaration or definition of `fn_name`
    renamed to `new_name`.

    `find_definition_brace`/`_select_definition` pick *one* occurrence (the one
    `def_line` anchors to, or the textually-first) — the right choice for
    injecting an obligation into the compiled definition. Renaming to avoid a
    name collision is a different problem, on two fronts (issue #95 review):

    - An inactive ``#if 0`` alternative ahead of the real definition is
      textually just as definition-shaped (this scan has no preprocessor), so
      renaming only the first occurrence can rename the dead one and leave
      the real, colliding definition untouched. There is no compiled
      `def_line` to anchor on for this caller's purpose anyway, so every
      definition-shaped occurrence is renamed — dead code included, which is
      harmless to touch.
    - A *declaration* (prototype) is not itself a collision the way a second
      definition is — a matching-signature ``int main(void);`` ahead of
      ``int main(void) { ... }`` is legal C — but this scan cannot verify the
      prototype's signature actually matches whatever the caller renames the
      definition to become (a mismatched-type prototype left behind, e.g.
      ``void main(void);`` ahead of an ``int main(void)`` definition, is a
      hard `conflicting types` parse error, verified against a live esbmc
      run). Renaming every declaration alongside every definition sidesteps
      the signature question entirely rather than trying to answer it.

    Each occurrence's own name span (`_DefinitionMatch.name_start` /
    `_declaration_name_starts`, from the same regex match the respective scan
    already isolated) is what gets replaced — never a fresh, independent
    search for `fn_name` in the surrounding text, which a string literal or
    attribute mentioning the name after the real token could shadow (an
    ``__attribute__((annotate("main")))`` suffix). Comments *and* string/char
    literals are masked before scanning (`_stripped_for_scan`), so a literal
    that happens to spell `fn_name(` — e.g. a diagnostic like
    ``printf("call main();\n")`` — is never mistaken for a reference and
    rewritten in place (issue #95 review). Occurrences are rewritten
    last-to-first so each replacement's offset stays valid for the ones still
    to come.
    """
    masked = _stripped_for_scan(source_text)
    starts = {m.name_start for m in _definition_candidates(masked, fn_name)}
    starts.update(_declaration_name_starts(masked, fn_name))
    result = source_text
    for start in sorted(starts, reverse=True):
        end = start + len(fn_name)
        result = result[:start] + new_name + result[end:]
    return result


@dataclass(frozen=True)
class _ArrayShape:
    """What a parameter's source declarator says about its extent."""

    extent: int | None = None
    unresolved: bool = False
    static_min: bool = False


def _array_shape(param_list: str, name: str) -> _ArrayShape:
    """What ``name``'s declarator in `param_list` says about its array extent.

    Yields `extent` when a single-dimension literal is recovered from ``name[N]``;
    `unresolved` when ``name`` *is* written as a fixed array whose extent cannot be
    read from the source — a macro or expression (``name[SHA_DIGEST_LENGTH]``, which
    needs the preprocessor) or a multi-dimensional ``name[N][M]`` (a
    pointer-to-array, not an L0 shape); and neither when there is no extent to
    recover in the first place: a plain pointer, an unsized ``name[]`` or
    qualifier-only ``name[const]`` (both exactly as informative as ``T *name``),
    or an unnamed parameter. `static_min` is set independently, for C99's
    ``name[static N]``.

    The unresolved flag is what keeps an unreadable extent out of the one-element
    fallback, which would under-size the object. It says nothing about a parameter
    list `_param_list_text` could not isolate at all — that residual case yields a
    default shape, i.e. one indistinguishable from a plain pointer.
    """
    if not name:
        return _ArrayShape()
    match = re.search(_ARRAY_DECL_TEMPLATE.format(name=re.escape(name)), param_list)
    if not match:
        return _ArrayShape()
    brackets = re.findall(r"\[([^\]]*)\]", match.group(1))
    static_min = any(_STATIC_MIN_RE.search(b) for b in brackets)
    if len(brackets) != 1:
        return _ArrayShape(unresolved=True, static_min=static_min)
    inner = brackets[0]
    if not inner.strip() or _QUALIFIER_ONLY_RE.match(inner):
        return _ArrayShape(static_min=static_min)
    extent = _EXTENT_RE.match(inner)
    if extent is None:
        return _ArrayShape(unresolved=True, static_min=static_min)
    return _ArrayShape(extent=int(extent.group(1)), static_min=static_min)


def _annotated_param(param: Param, param_list: str) -> Param:
    """`param` with its written array shape attached, read off `param_list`."""
    shape = _array_shape(param_list, param.name)
    return replace(
        param,
        array_extent=shape.extent,
        array_extent_unresolved=shape.unresolved,
        array_static_min=shape.static_min,
    )


def _param_list_text(
    matches: list[_DefinitionMatch],
    source_no_comments: str,
    unit: Unit,
) -> str | None:
    """The parameter-list text of `unit`'s *definition*, or ``None``.

    The declarator text between ``(`` and its matching ``)`` for the
    definition-shaped occurrence `unit.def_line` anchors to — see
    `_definition_candidates` for what counts as definition-shaped (and produces
    `matches`) and `_select_definition` for how `def_line` (issue #145) and
    `predefined_guards` (issue #226) pick the compiled definition over an
    inactive ``#if 0``/``#ifdef`` alternative.

    Takes `unit` rather than its three fields unpacked, and `matches` rather than
    the name to rescan for: the two anchors are only meaningful together (see
    `Unit.predefined_guards`), and the candidate list is already computed once
    per listing (`list_units`).
    """
    chosen = _select_definition(
        matches, source_no_comments, unit.def_line, unit.predefined_guards
    )
    return None if chosen is None else chosen.params


def annotate_array_extents(units: list[Unit], source_text: str) -> list[Unit]:
    """Attach each pointer parameter's written fixed-array extent, from `source_text`.

    Pure post-pass over `parse_units`' output: for every unit, isolate its
    definition's parameter list (comment-stripped) and set `Param.array_extent`
    for pointer parameters written as ``T p[N]`` — or `Param.array_extent_unresolved`
    when the declarator states an extent we cannot read. Non-pointer parameters and
    units whose parameter list cannot be isolated are returned unchanged. Kept
    separate from the AST walk (and independently tested) because the extent
    comes from the *source declarator*, not the clang type — which has adjusted
    ``T p[N]`` to ``T *`` and discarded ``N``.

    The declarator search is anchored to `Unit.def_line` (`_param_list_text`), so
    a same-named definition excluded by `#if 0`/`#ifdef` cannot donate its extent
    to the one clang actually compiled (issue #145), and to each unit's own
    `Unit.predefined_guards` so that a ``#ifdef`` only the build flags or a
    compiler builtin decide is excluded too (issue #226). String/char literals are
    stripped alongside comments (`_stripped_for_scan`) for the same reason
    `rename_all_declarations_and_definitions` does: a literal spelling
    `name(...)` is not a parameter list to isolate.
    """
    stripped = _stripped_for_scan(source_text)
    candidates = _candidates_by_name(units, stripped)
    return _annotate_array_extents(units, stripped, candidates)


def _candidates_by_name(
    units: list[Unit], stripped: str
) -> dict[str, list[_DefinitionMatch]]:
    """Each unit name's definition-shaped occurrences in `stripped`, scanned once.

    Keyed by name because that is all `_definition_candidates` reads, so two
    units sharing a name share the one scan.
    """
    return {unit.name: _definition_candidates(stripped, unit.name) for unit in units}


def _annotate_array_extents(
    units: list[Unit],
    stripped: str,
    candidates: dict[str, list[_DefinitionMatch]],
) -> list[Unit]:
    """`annotate_array_extents` on text `_stripped_for_scan` has already masked,
    and candidates `_candidates_by_name` has already scanned for.

    Split out so `list_units` pays for both once per listing rather than twice:
    it needs the same masked text and the same candidate lists for
    `_with_predefined_guards`' cost gate, which would otherwise scan every unit
    name here a second time — on every listing, including the common one where
    the gate declines to probe at all.
    """
    annotated: list[Unit] = []
    for unit in units:
        param_list = _param_list_text(candidates[unit.name], stripped, unit)
        if param_list is None:
            annotated.append(unit)
            continue
        params = tuple(
            _annotated_param(p, param_list) if p.is_pointer else p for p in unit.params
        )
        annotated.append(replace(unit, params=params))
    return annotated


def _parse_tree(
    source: Path, esbmc_bin: str, timeout_s: float, extra_flags: Sequence[str]
) -> str:
    """The clang AST dump for `source`, or raise `ListUnitsError`.

    Raises when esbmc cannot be invoked (missing/unrunnable binary) **or** when
    the parse run fails (missing source, bad include path, C parse error — esbmc
    exits nonzero), so a failed parse is never indistinguishable from a file that
    happens to define nothing. ESBMC prints the dump to stderr; both streams are
    combined so the parser is robust to which one a given build uses.
    """
    argv = (esbmc_bin, str(source), "--parse-tree-only", *extra_flags)
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ListUnitsError(
            f"esbmc --parse-tree-only failed: {esbmc_bin}: {exc}"
        ) from exc
    if proc.returncode != 0:
        detail = (
            _error_line(proc.stderr)
            or _error_line(proc.stdout)
            or f"exit {proc.returncode}"
        )
        raise ListUnitsError(f"esbmc --parse-tree-only failed ({esbmc_bin}): {detail}")
    return proc.stdout + "\n" + proc.stderr


# The probe translation unit `probe_predefined_guards` compiles: a leading
# `#undef` of every identifier it is about to declare, then one
# `int <prefix><i>;` per guard name, emitted only inside `#ifdef NAME`, so the
# declarations that survive into the dump name exactly the guards the
# preprocessor considered defined. The `#undef`s are what make that reading
# true under any `extra_flags`: the declarations name themselves, so without
# them a `-D` of one of these identifiers rewrites its declaration before the
# AST and the guard reads `False`. `sentinel` is unconditional and so must
# always survive — its absence means the dump is not a dump of this probe (a
# flag that broke the run, an ESBMC that printed something else) and the whole
# reading is discarded rather than read as "nothing was defined".
_PROBE_STEM = "forseti_guard_probe"
_PROBE_PREFIX = f"{_PROBE_STEM}_"
_PROBE_SENTINEL = "sentinel"
# Anchored on the trailing `_` of `_PROBE_PREFIX`, which is what keeps the probe
# *file* name out of the reading: the dump quotes `forseti_guard_probe.c` (or
# `.cpp`) in every location it prints, and a `.` is not a `_`.
_PROBE_DECL_RE = re.compile(rf"\b{_PROBE_PREFIX}(\d+|{_PROBE_SENTINEL})\b")

# The probe's share of a listing's time budget. `_parse_tree` applies its
# timeout per `subprocess.run`, so handing the probe the caller's whole
# `timeout_s` would double `list_units`' in-process worst case — past the
# margin the Claude Code gate sizes its own `subprocess.run` with
# (`LIST_UNITS_TIMEOUT_S + 15s`, written for one esbmc run), turning an
# optional, fail-closed measurement into a blocking `UnitsUnavailable` on
# every edit of a slow-parsing file. A probe TU is a few declarations and no
# `#include`s — ~11ms locally — so a small cap costs nothing real, and a probe
# that overruns it degrades to `()` like any other probe failure.
_PROBE_TIMEOUT_CAP_S = 10.0


def probe_predefined_guards(
    source_no_comments: str,
    *,
    esbmc_bin: str = "esbmc",
    # Not `list_units`' 30.0: every budget at or above the cap behaves
    # identically, so mirroring that default would only invite the reader to
    # think the probe gets 30s. The production caller passes its own anyway.
    timeout_s: float = _PROBE_TIMEOUT_CAP_S,
    extra_flags: Sequence[str] = (),
    suffix: str = ".c",
) -> tuple[tuple[str, bool], ...]:
    """Which of `source_no_comments`' conditional guards are defined before its
    first line, measured against the real preprocessor under `extra_flags`.

    The one question `_line_breakpoints`' textual scan structurally cannot
    answer (issue #226): a ``#ifdef WIDGET`` whose ``WIDGET`` comes from a
    command-line ``-D`` or is a compiler builtin leaves no trace in the file, so
    the scan has to assume the branch live and can then count a ``#line`` cpp
    never reached. This asks instead — it compiles a generated probe TU that
    declares one uniquely named variable per guard name inside that name's own
    ``#ifdef``, with the *same* `esbmc_bin`, `extra_flags` and `suffix` the real
    parse used, and reports which declarations survived.

    Those three are the complete list of inputs that decide the predefined set,
    and every one of them has to be reproduced or the probe answers a different
    question than the parse it annotates. The binary picks the builtins, the
    flags add ``-D``\\ s — and the *file extension* picks the language: esbmc
    parses ``x.cpp`` as C++ and defines ``__cplusplus``, ``x.c`` as C and does
    not, so probing a C++ source through a ``.c`` file reports ``__cplusplus``
    undefined and proves the arm cpp actually took *dead*. Nothing else is in
    play: the process cwd is shared with the real run (so a relative ``-I``
    resolves the same), and the probe's own directory is irrelevant because it
    ``#include``\\ s nothing. An extension esbmc will not take (``""`` among
    them — ``failed to figure out type of file``) needs no special case: it
    fails the probe run, which fails closed like every other probe failure, and
    a source carrying one could not have been parsed in the first place.

    What it measures is definedness at the *start* of a translation unit and
    nothing else. The probe deliberately ``#include``s nothing: a header's
    effects depend on where it is included, which a separate TU cannot
    reproduce, so a guard sitting below an ``#include`` stays this issue's
    residual and `_line_breakpoints` still drops the seed there.

    A guard name inside the probe's own ``forseti_guard_probe_`` namespace is
    dropped rather than answered — the leading ``#undef``\\ s would undefine the
    very name its ``#ifdef`` tests — so it comes back absent, which
    `_line_breakpoints` reads as opaque and assumed-live.

    Fails closed to ``()`` — an unrunnable or failing esbmc, an unwritable or
    unencodable temporary file, a probe overrunning `_PROBE_TIMEOUT_CAP_S`, a
    dump missing the unconditional sentinel. ``()`` is not a degraded answer but
    the *previous* answer: every guard reads opaque and assumed-live again,
    exactly as before this existed. The caller has already parsed the source
    successfully by this point, so a probe failure must not turn a good listing
    into a raised error.
    """
    # A guard name inside the probe's own namespace cannot be measured by this
    # scheme at all: the `#undef` block below would undefine the very name its
    # `#ifdef` tests. Drop it rather than answer, so it comes back absent —
    # which `_line_breakpoints` reads as opaque and assumed-live, the safe
    # direction — instead of a silent `False`. Filtered on the whole prefix, not
    # on the identifiers actually emitted: the emitted set is a function of how
    # many names survive this filter, so a rule that depended on the index
    # arithmetic would shift its own boundary.
    names = tuple(
        name
        for name in _guard_macro_names(source_no_comments)
        if not name.startswith(_PROBE_PREFIX)
    )
    if not names:
        return ()
    probe_timeout_s = min(timeout_s, _PROBE_TIMEOUT_CAP_S)
    declared = [f"{_PROBE_PREFIX}{index}" for index in range(len(names))]
    declared.append(f"{_PROBE_PREFIX}{_PROBE_SENTINEL}")
    # The declarations name themselves, so a build that predefines one of these
    # identifiers rewrites the declaration before it reaches the AST: under
    # `-Dforseti_guard_probe_0=WIDGET` the probe emits `int WIDGET;`, index `0`
    # is absent from the dump, and a guard that *is* defined is recorded
    # `False`. The sentinel still survives, so nothing fails closed — it is a
    # silent inversion in the wrong *dead* direction. `#undef` of a macro that
    # was never defined is a well-defined no-op in C and C++, so undefining
    # every emitted identifier up front costs nothing in the normal case and
    # makes the reading independent of `-D`s and preincluded headers alike
    # (review feedback on PR #231).
    probe_text = "".join(f"#undef {ident}\n" for ident in declared)
    probe_text += "".join(
        f"#ifdef {name}\nint {_PROBE_PREFIX}{index};\n#endif\n"
        for index, name in enumerate(names)
    )
    probe_text += f"int {_PROBE_PREFIX}{_PROBE_SENTINEL};\n"
    try:
        with tempfile.TemporaryDirectory(prefix="forseti-guard-probe-") as tmp:
            probe_path = Path(tmp) / f"{_PROBE_STEM}{suffix}"
            # Explicit UTF-8, never the locale's encoding: `_MACRO_NAME`'s
            # continuation class is `[\w$]`, which Python's `re` reads as
            # Unicode, so `#ifdef Wé` is a name this probe really can be asked
            # about. Under a C/latin-1 locale the default would either raise
            # `UnicodeEncodeError` — a `ValueError`, which the handler below
            # does *not* catch, so it would escape and fail a listing that had
            # already parsed — or write mis-encoded bytes, making the probe test
            # a *different* name and answer `False` for a guard that is defined.
            # That is the wrong *dead* direction, the one this module is built
            # to never take. Pinning the encoding closes both, which is why the
            # handler can stay narrow.
            probe_path.write_text(probe_text, encoding="utf-8")
            dump = _parse_tree(probe_path, esbmc_bin, probe_timeout_s, extra_flags)
    except (OSError, ListUnitsError):
        return ()
    survived = {match.group(1) for match in _PROBE_DECL_RE.finditer(dump)}
    if _PROBE_SENTINEL not in survived:
        return ()
    return tuple((name, str(index) in survived) for index, name in enumerate(names))


@dataclass(frozen=True)
class CallerOpenings:
    """The five ways `list_units`'s caller enumeration can under-report `symbol`.

    Each field is the result of one caller-completeness pass over a *single*
    ``esbmc --parse-tree-only`` dump, produced by `list_caller_openings`:

    - `foreign`   — definitions outside `source` naming `symbol`
      (`parse_external_callers`)
    - `escaped`   — sites naming `symbol` outside a direct call
      (`parse_address_escapes`)
    - `aliased`   — declarations that are another name for `symbol`
      (`parse_symbol_aliases`)
    - `implicit`  — load-time attributes on `symbol`'s own declaration
      (`parse_implicit_invocations`)
    - `asm_sites` — sites GNU inline asm might route to `symbol`
      (`parse_asm_statements`)

    The questions stay distinct — one field, one `parse_*` pass each — they only
    share the dump.
    """

    foreign: tuple[str, ...]
    escaped: tuple[str, ...]
    aliased: tuple[str, ...]
    implicit: tuple[str, ...]
    asm_sites: tuple[str, ...]


def list_caller_openings(
    source: Path,
    symbol: str,
    *,
    esbmc_bin: str = "esbmc",
    timeout_s: float = 30.0,
    extra_flags: Sequence[str] = (),
) -> CallerOpenings:
    """All five caller-completeness listings for `symbol`, from one shared dump.

    Parses `source` once with ``esbmc --parse-tree-only`` and runs all five
    `parse_*` passes over that single dump — the seam `find_open_callers`
    crosses. Each pass keeps its own semantics (e.g. `parse_external_callers`
    still takes `source`, to exclude in-file definitions). Raises `ListUnitsError`
    on the same conditions as `list_units`, so an unparseable TU never reads as
    "no hidden callers".
    """
    ast_text = _parse_tree(source, esbmc_bin, timeout_s, extra_flags)
    return CallerOpenings(
        foreign=parse_external_callers(ast_text, source, symbol),
        escaped=parse_address_escapes(ast_text, symbol),
        aliased=parse_symbol_aliases(ast_text, symbol),
        implicit=parse_implicit_invocations(ast_text, symbol),
        asm_sites=parse_asm_statements(ast_text, symbol),
    )


def _with_predefined_guards(
    units: list[Unit],
    masked: str,
    candidates: dict[str, list[_DefinitionMatch]],
    *,
    esbmc_bin: str,
    timeout_s: float,
    extra_flags: Sequence[str],
    suffix: str,
) -> list[Unit]:
    """`units`, each carrying `probe_predefined_guards`' reading of `masked`.

    `masked` is `_stripped_for_scan` output and `candidates` is
    `_candidates_by_name` over it — the same text and the same scan the extent
    pass uses, taken as arguments rather than re-derived so a listing pays for
    each once.

    Returned unchanged unless some unit's name has more than one
    definition-shaped occurrence. That is the exact precondition for the guard
    seed to change any answer: `_select_definition` returns a sole candidate
    without consulting the breakpoints at all, so probing a single-definition
    file — the common case, and one that would otherwise pay a whole extra
    ``esbmc --parse-tree-only`` run per listing — buys nothing.

    The probe runs at most once per listing, under the same binary, flags and
    file extension as the parse it annotates, and contributes ``()`` when it
    cannot (see `probe_predefined_guards`).
    """
    if not any(len(matches) > 1 for matches in candidates.values()):
        return units
    guards = probe_predefined_guards(
        masked,
        esbmc_bin=esbmc_bin,
        timeout_s=timeout_s,
        extra_flags=extra_flags,
        suffix=suffix,
    )
    if not guards:
        return units
    return [replace(unit, predefined_guards=guards) for unit in units]


def list_units(
    source: Path,
    *,
    esbmc_bin: str = "esbmc",
    timeout_s: float = 30.0,
    extra_flags: Sequence[str] = (),
) -> list[Unit]:
    """Run ``esbmc --parse-tree-only`` on `source` and parse its function units.

    Raises `ListUnitsError` when esbmc cannot be invoked (missing/unrunnable
    binary) **or** when the parse run fails (missing source, bad include path, C
    parse error — esbmc exits nonzero). Only a *successful* run that defines no
    functions returns ``[]`` (an empty or declaration-only file). Never treats a
    failed parse as an empty file, which would be indistinguishable from a valid
    one and could let the gate silently skip a unit.
    """
    units = parse_units(_parse_tree(source, esbmc_bin, timeout_s, extra_flags), source)
    # Enrich with fixed-array extents read from the source declarators (the clang
    # type has adjusted `T p[N]` to `T *`). Best-effort: a successful parse means
    # esbmc read the file, so a read failure here is unexpected — degrade to the
    # un-annotated units rather than fail a listing that already succeeded.
    try:
        source_text = Path(source).read_text()
    except OSError:
        return units
    masked = _stripped_for_scan(source_text)
    candidates = _candidates_by_name(units, masked)
    return _annotate_array_extents(
        _with_predefined_guards(
            units,
            masked,
            candidates,
            esbmc_bin=esbmc_bin,
            timeout_s=timeout_s,
            extra_flags=extra_flags,
            # The extension is a parse input like the binary and the flags: it
            # picks the language, and so the predefined set the probe reads
            # (`probe_predefined_guards`).
            suffix=Path(source).suffix,
        ),
        masked,
        candidates,
    )

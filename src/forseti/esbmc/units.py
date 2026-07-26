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
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path

# One AST node line: leading tree art, then `-Kind`, then the rest. The art is
# 2 columns per depth (`| ` / `  `, closed by `|-` / `` `- ``), so `len(art)//2`
# is a monotonic depth — only the relative order (child deeper than parent) is
# used, never an absolute value.
_NODE_RE = re.compile(r"^([ |`]*)-(\w+)\b(.*)$")

# The first source location in a node line: `<START, ...>` or `<START>`. START is
# `PATH:line:col`, `line:line:col`, `col:col`, or `<built-in>:...`.
_LOC_RE = re.compile(r"<([^,>]+)")

# The identifier a Decl names: the last word immediately before its `'type'`.
_NAME_RE = re.compile(r"\s(\w+)\s+'")

# Every quoted type on a line; a typedef'd param prints `'written':'canonical'`,
# so the *last* match is the canonical (typedef-resolved) type.
_TYPE_RE = re.compile(r"'([^']*)'")

# Line (`// ...`) and block (`/* ... */`) comments, blanked before harvesting an
# array extent so a `[N]` inside a comment can never be misread as a declarator.
_COMMENT_RE = re.compile(r"//[^\n]*|/\*.*?\*/", re.DOTALL)

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
    """

    name: str
    params: tuple[Param, ...]
    calls: tuple[str, ...] = ()
    internal_linkage: bool = False

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
    """
    match = _LOC_RE.search(rest)
    if not match:
        return current
    head = match.group(1).split(":", 1)[0]
    if head in ("line", "col"):
        return current
    return head


def _depth(art: str) -> int:
    return len(art) // 2


def _error_line(text: str) -> str:
    """The first ``ERROR:``-prefixed line, so a failed run is self-describing."""
    for line in text.splitlines():
        if line.startswith("ERROR:"):
            return line[len("ERROR:") :].strip()
    return ""


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
    """
    found: list[tuple[str, Unit]] = []
    internal: set[str] = set()
    current_file = ""

    # State for the FunctionDecl currently being assembled.
    fn_name: str | None = None
    fn_depth = -1
    fn_file = ""
    params: list[Param] = []
    calls: dict[str, None] = {}  # an ordered set of referenced function names
    is_definition = False

    def flush() -> None:
        nonlocal fn_name
        if fn_name and is_definition:
            found.append((fn_file, Unit(fn_name, tuple(params), tuple(calls))))
        fn_name = None

    for line in ast_text.splitlines():
        node = _NODE_RE.match(line)
        if not node:
            continue
        art, kind, rest = node.group(1), node.group(2), node.group(3)
        depth = _depth(art)
        current_file = _loc_file(rest, current_file)

        # A node at or above the open FunctionDecl's depth ends its subtree.
        if fn_name is not None and depth <= fn_depth:
            flush()

        if kind == "FunctionDecl":
            flush()  # close a same-depth previous function first
            name_match = _NAME_RE.search(rest)
            fn_name = name_match.group(1) if name_match else None
            if fn_name and _STATIC_STORAGE in rest.rsplit("'", 1)[-1].split():
                internal.add(fn_name)
            fn_depth = depth
            fn_file = current_file
            params = []
            calls = {}
            is_definition = False
        elif fn_name is not None and depth > fn_depth:
            if kind == "ParmVarDecl" and depth == fn_depth + 1:
                types = _TYPE_RE.findall(rest)
                name_match = _NAME_RE.search(rest)
                params.append(
                    Param(
                        name_match.group(1) if name_match else "",
                        types[-1] if types else "",
                    )
                )
            elif kind == "CompoundStmt":
                is_definition = True
            elif kind == "DeclRefExpr":
                callee = _CALLEE_RE.search(rest)
                if callee is not None:
                    calls[callee.group(1)] = None

    flush()
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


@dataclass
class _AstNode:
    """One node of the walked AST, with its position among its parent's children."""

    depth: int
    kind: str
    rest: str
    index: int
    children: int = 0


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
    """Sites where `symbol`'s **address** is taken rather than called, by name.

    A function whose address is stored in a pointer can be invoked through it
    from anywhere in the translation unit, and an indirect ``fp(...)`` names only
    the *variable* — so no `Unit.calls` edge leads back to `symbol` and the caller
    enumeration silently misses that path. This reports the escapes so
    compositional discharge (RFC-0003 S3) can withhold its upgrade instead:
    ``static cb_t fp = f;`` at file scope yields ``fp``, an ``fp = f`` inside a
    body yields the enclosing function.

    A *direct* call is not an escape, however it is written (`_is_call_callee`),
    including the recursive call in ``symbol``'s own body — otherwise every
    recursive unit would be permanently undischargeable.
    """
    sites: dict[str, None] = {}
    stack: list[_AstNode] = []
    for line in ast_text.splitlines():
        node = _NODE_RE.match(line)
        if not node:
            continue
        art, kind, rest = node.group(1), node.group(2), node.group(3)
        depth = _depth(art)
        while stack and stack[-1].depth >= depth:
            stack.pop()
        index = 0
        if stack:
            index = stack[-1].children
            stack[-1].children += 1
        stack.append(_AstNode(depth, kind, rest, index))
        if kind != "DeclRefExpr":
            continue
        referenced = _CALLEE_RE.search(rest)
        if referenced is None or referenced.group(1) != symbol:
            continue
        if not _is_call_callee(stack):
            sites[_enclosing_name(stack)] = None
    return tuple(sites)


def mask_comments(source_text: str) -> str:
    """`source_text` with every comment blanked out, **preserving every offset**.

    Each comment character becomes a space (newlines kept, so line numbering is
    untouched), rather than the whole comment collapsing to one space. Length
    preservation is what lets an index found in the masked text be used verbatim
    against the original — which `find_definition_brace` relies on to inject
    text at an exact position in the user's source.
    """

    def blank(match: re.Match[str]) -> str:
        return "".join("\n" if ch == "\n" else " " for ch in match.group(0))

    return _COMMENT_RE.sub(blank, source_text)


def _definition_signature(
    source_no_comments: str, fn_name: str
) -> tuple[str, int] | None:
    """`fn_name`'s definition: its parameter-list text and its ``{`` index.

    Scans for ``fn_name (`` and balances parentheses to the matching ``)``; the
    occurrence whose ``)`` is followed (past whitespace) by ``{`` is the
    definition — so a prototype (``);``) or a call site (``) ;``, ``))``) is
    skipped. Deliberately narrow: clang already told us the canonical types and
    which parameters are pointers, so this only has to isolate the declarator
    text to harvest an array extent from — never to classify a type.
    """
    for match in re.finditer(rf"\b{re.escape(fn_name)}\s*\(", source_no_comments):
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
        if k < len(source_no_comments) and source_no_comments[k] == "{":
            return source_no_comments[match.end() : j], k
    return None


def find_definition_brace(source_text: str, fn_name: str) -> int | None:
    """Index of the ``{`` opening `fn_name`'s definition body in `source_text`.

    ``None`` when no definition of that name is isolable (a prototype-only
    declaration, a name that appears solely at call sites, an unbalanced
    signature). The index is into the **original** text — comments are masked
    length-preservingly first, so a ``{`` inside a comment cannot be mistaken for
    the body's while the offset stays usable for injection (RFC-0003 S3).
    """
    found = _definition_signature(mask_comments(source_text), fn_name)
    return None if found is None else found[1]


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
    """
    stripped = mask_comments(source_text)
    annotated: list[Unit] = []
    for unit in units:
        found = _definition_signature(stripped, unit.name)
        if found is None:
            annotated.append(unit)
            continue
        param_list = found[0]
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


def list_external_callers(
    source: Path,
    symbol: str,
    *,
    esbmc_bin: str = "esbmc",
    timeout_s: float = 30.0,
    extra_flags: Sequence[str] = (),
) -> tuple[str, ...]:
    """Names of definitions *outside* `source` that reference `symbol`.

    The complement of `list_units` over the same translation unit — see
    `parse_external_callers` for why compositional discharge needs it. Raises
    `ListUnitsError` on the same conditions as `list_units`.
    """
    ast_text = _parse_tree(source, esbmc_bin, timeout_s, extra_flags)
    return parse_external_callers(ast_text, source, symbol)


def list_address_escapes(
    source: Path,
    symbol: str,
    *,
    esbmc_bin: str = "esbmc",
    timeout_s: float = 30.0,
    extra_flags: Sequence[str] = (),
) -> tuple[str, ...]:
    """Sites in `source`'s translation unit that take `symbol`'s address.

    The other way the caller enumeration can be incomplete — see
    `parse_address_escapes`. Raises `ListUnitsError` on the same conditions as
    `list_units`.
    """
    ast_text = _parse_tree(source, esbmc_bin, timeout_s, extra_flags)
    return parse_address_escapes(ast_text, symbol)


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
    return annotate_array_extents(units, source_text)

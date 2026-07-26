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

# Line (`// ...`) and block (`/* ... */`) comments, stripped before harvesting an
# array extent so a `[N]` inside a comment can never be misread as a declarator.
_COMMENT_RE = re.compile(r"//[^\n]*|/\*.*?\*/", re.DOTALL)

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

    `def_line` is the 1-based line of the definition clang actually compiled, as
    reported by its own AST location — set by `parse_units`, ``None`` for a
    hand-built `Unit` (tests). `annotate_array_extents` anchors its declarator
    search to this line so a `#if 0`/`#ifdef`-excluded alternative body with the
    same name cannot donate its array shape to the active definition (issue #145).
    """

    name: str
    params: tuple[Param, ...]
    def_line: int | None = None

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


def _loc_line(rest: str, current: int) -> int:
    """The 1-based source line a node line refers to.

    Mirrors `_loc_file`: a bare ``col:N`` inherits `current` (same line as the
    enclosing location); ``line:N:...`` or a full ``PATH:N:...`` gives the new line.
    """
    match = _LOC_RE.search(rest)
    if not match:
        return current
    parts = match.group(1).split(":")
    if len(parts) >= 2 and parts[0] != "col" and parts[-2].isdecimal():
        return int(parts[-2])
    return current


def _depth(art: str) -> int:
    return len(art) // 2


def _error_line(text: str) -> str:
    """The first ``ERROR:``-prefixed line, so a failed run is self-describing."""
    for line in text.splitlines():
        if line.startswith("ERROR:"):
            return line[len("ERROR:") :].strip()
    return ""


def parse_units(ast_text: str, source: str | Path) -> list[Unit]:
    """Function definitions in `source` from an ``esbmc --parse-tree-only`` dump.

    Walks the textual AST tracking the current file; for each ``FunctionDecl`` in
    `source` it collects the immediate ``ParmVarDecl`` children (with their
    canonical types) and keeps the unit only if the subtree contains a
    ``CompoundStmt`` — i.e. it is a *definition*, not a prototype (so a header of
    declarations yields nothing, matching what the gate can verify). Deduped by
    name, first definition wins.
    """
    # Attribute a function to `source` by a normalized full-path match against
    # clang's location (clang echoes the input path verbatim), so a definition
    # from a same-basename `#include`d file in another directory is not misread
    # as belonging to `source`.
    source_norm = os.path.normpath(str(source))
    units: list[Unit] = []
    seen: set[str] = set()
    current_file = ""
    current_line = 0

    # State for the FunctionDecl currently being assembled.
    fn_name: str | None = None
    fn_depth = -1
    fn_in_target = False
    fn_def_line: int | None = None
    params: list[Param] = []
    is_definition = False

    def flush() -> None:
        nonlocal fn_name
        if fn_name and fn_in_target and is_definition and fn_name not in seen:
            seen.add(fn_name)
            units.append(Unit(fn_name, tuple(params), fn_def_line))
        fn_name = None

    for line in ast_text.splitlines():
        node = _NODE_RE.match(line)
        if not node:
            continue
        art, kind, rest = node.group(1), node.group(2), node.group(3)
        depth = _depth(art)
        current_file = _loc_file(rest, current_file)
        current_line = _loc_line(rest, current_line)

        # A node at or above the open FunctionDecl's depth ends its subtree.
        if fn_name is not None and depth <= fn_depth:
            flush()

        if kind == "FunctionDecl":
            flush()  # close a same-depth previous function first
            name_match = _NAME_RE.search(rest)
            fn_name = name_match.group(1) if name_match else None
            fn_depth = depth
            fn_in_target = bool(current_file) and (
                os.path.normpath(current_file) == source_norm
            )
            fn_def_line = current_line
            params = []
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

    flush()
    return units


def _param_list_text(
    source_no_comments: str, fn_name: str, def_line: int | None = None
) -> str | None:
    """The parameter-list text of `fn_name`'s *definition*, or ``None``.

    Scans for ``fn_name (`` and balances parentheses to the matching ``)``; an
    occurrence whose ``)`` is followed (past whitespace) by ``{`` is *definition-
    shaped* — so a prototype (``);``) or a call site (``) ;``, ``))``) is skipped.
    Deliberately narrow: clang already told us the canonical types and which
    parameters are pointers, so this only has to isolate the declarator text to
    harvest an array extent from — never to classify a type.

    Textual order alone cannot tell two definition-shaped occurrences of the same
    name apart — e.g. an inactive ``#if 0`` body ahead of the one clang actually
    compiled (issue #145). When `def_line` is given (the compiled definition's
    line, from `Unit.def_line`), the occurrence at or nearest *after* that line is
    preferred over one before it — `def_line` points at the declaration's opening
    line, which the ``fn_name (`` text can follow by a line or two (a return type
    on its own line). Without `def_line` the first definition-shaped occurrence is
    used, as before.
    """
    candidates: list[tuple[int, str]] = []
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
            line = source_no_comments.count("\n", 0, match.start()) + 1
            candidates.append((line, source_no_comments[match.end() : j]))
    if not candidates:
        return None
    if def_line is None:
        return candidates[0][1]
    # Sort key: candidates at or after `def_line` (key[0] == False) all sort before
    # any that are only before it, then nearest wins within each group — "at or
    # nearest after", falling back to nearest-before only if none qualify.
    return min(candidates, key=lambda c: (c[0] < def_line, abs(c[0] - def_line)))[1]


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


def _blank_comment(match: re.Match[str]) -> str:
    """A comment's replacement text: its newlines, so line numbers stay aligned.

    A single-line comment (`//...` or a same-line `/*...*/`) becomes one space,
    same as before. A multi-line block comment becomes that many bare newlines
    instead — dropping its inline text (fine; a comment holds no declarator) while
    keeping every following line's number identical to `source_text`'s, which
    `_param_list_text` relies on to match a `Unit.def_line` from clang.
    """
    newlines = match.group(0).count("\n")
    return "\n" * newlines if newlines else " "


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
    to the one clang actually compiled (issue #145).
    """
    stripped = _COMMENT_RE.sub(_blank_comment, source_text)
    annotated: list[Unit] = []
    for unit in units:
        param_list = _param_list_text(stripped, unit.name, unit.def_line)
        if param_list is None:
            annotated.append(unit)
            continue
        params = tuple(
            _annotated_param(p, param_list) if p.is_pointer else p for p in unit.params
        )
        annotated.append(Unit(unit.name, params, unit.def_line))
    return annotated


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
    # ESBMC prints the AST dump to stderr; combine both streams so the parser is
    # robust to which stream a given build uses.
    units = parse_units(proc.stdout + "\n" + proc.stderr, source)
    # Enrich with fixed-array extents read from the source declarators (the clang
    # type has adjusted `T p[N]` to `T *`). Best-effort: a successful parse means
    # esbmc read the file, so a read failure here is unexpected — degrade to the
    # un-annotated units rather than fail a listing that already succeeded.
    try:
        source_text = Path(source).read_text()
    except OSError:
        return units
    return annotate_array_extents(units, source_text)

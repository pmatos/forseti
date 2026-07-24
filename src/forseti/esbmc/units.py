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
    """

    name: str
    type: str
    array_extent: int | None = None

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
    """A function *definition* found in the source: its name and parameters."""

    name: str
    params: tuple[Param, ...]

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

    # State for the FunctionDecl currently being assembled.
    fn_name: str | None = None
    fn_depth = -1
    fn_in_target = False
    params: list[Param] = []
    is_definition = False

    def flush() -> None:
        nonlocal fn_name
        if fn_name and fn_in_target and is_definition and fn_name not in seen:
            seen.add(fn_name)
            units.append(Unit(fn_name, tuple(params)))
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
            fn_depth = depth
            fn_in_target = bool(current_file) and (
                os.path.normpath(current_file) == source_norm
            )
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


def _param_list_text(source_no_comments: str, fn_name: str) -> str | None:
    """The parameter-list text of `fn_name`'s *definition*, or ``None``.

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
            return source_no_comments[match.end() : j]
    return None


def _array_extent(param_list: str, name: str) -> int | None:
    """The ``N`` of ``name[N]`` in `param_list`, or ``None``.

    Harvests a *single-dimension* fixed extent for the named parameter; a
    multi-dimensional ``name[N][M]`` (a pointer-to-array, not an L0 shape) is
    left as ``None`` so the synthesizer treats it as unresolved rather than
    mis-sizing it.
    """
    if not name:
        return None
    match = re.search(rf"\b{re.escape(name)}\s*\[\s*(\d+)\s*\]\s*(?!\[)", param_list)
    return int(match.group(1)) if match else None


def annotate_array_extents(units: list[Unit], source_text: str) -> list[Unit]:
    """Attach each pointer parameter's written fixed-array extent, from `source_text`.

    Pure post-pass over `parse_units`' output: for every unit, isolate its
    definition's parameter list (comment-stripped) and set `Param.array_extent`
    for pointer parameters written as ``T p[N]``. Non-pointer parameters and
    units whose parameter list cannot be isolated are returned unchanged. Kept
    separate from the AST walk (and independently tested) because the extent
    comes from the *source declarator*, not the clang type — which has adjusted
    ``T p[N]`` to ``T *`` and discarded ``N``.
    """
    stripped = _COMMENT_RE.sub(" ", source_text)
    annotated: list[Unit] = []
    for unit in units:
        param_list = _param_list_text(stripped, unit.name)
        if param_list is None:
            annotated.append(unit)
            continue
        params = tuple(
            replace(p, array_extent=_array_extent(param_list, p.name))
            if p.is_pointer
            else p
            for p in unit.params
        )
        annotated.append(Unit(unit.name, params))
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

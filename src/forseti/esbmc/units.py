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

# Line (`// ...`) and block (`/* ... */`) comments, stripped before harvesting an
# array extent so a `[N]` inside a comment can never be misread as a declarator.
# A `//` comment absorbs a trailing backslash-newline as part of itself: cpp
# splices lines (translation phase 2) before it ever recognizes a comment
# (phase 3), so `// text \` continued onto a `#line`/`#if`-looking next line is,
# to cpp, one comment spanning both physical lines — not a directive (PR #156
# follow-up to issue #145: `_line_breakpoints`' own continuation exclusion runs
# on this already-blanked text, so a backslash swallowed by an *unspliced* `//`
# match here would already be gone by the time that exclusion could see it).
_COMMENT_RE = re.compile(r"//(?:\\\r?\n|[^\n])*|/\*.*?\*/", re.DOTALL)

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

# A `#line N` (ISO C) or GNU linemarker `# N "file" flags...` directive, in its
# literal-digit-sequence form (a macro-valued `#line` is not handled — vanishingly
# rare outside raw preprocessor output, which this module never sees). Matches
# right after `#`, so `#define`/`#if`/... (which start with a non-digit, non-
# "line" word) never do. The captured digit run also tolerates an embedded
# backslash-continuation (PR #156 follow-up to issue #145: cpp splices `#line
# \` + newline + `11` into one logical `#line 11` before ever reading it, so a
# breakpoint recorded here must be too — the same splicing hazard `_IF_RE`/
# `_ELIF_RE` already guard against for a conditional's own text). The
# continuation is stripped from the captured group — via `_CONTINUATION_RE` —
# before the digits are read as an `int`, in `_line_breakpoints`.
_LINE_DIRECTIVE_RE = re.compile(
    r"^[ \t]*#[ \t]*(?:line[ \t]+)?((?:\\\r?\n)*\d(?:\\\r?\n|\d)*)\b", re.MULTILINE
)

# A literal `#if 0`/`#elif 0` or `#if 1`/`#elif 1` (the complete condition is
# exactly the digit `0` or `1`, nothing else) are the only conditionals this
# scan can resolve without a preprocessor: cpp always skips the former and
# always takes the latter, so a `#line` inside the skipped arm never reaches
# the compiler — and once a literal-`1` arm has been taken, neither does one
# inside any later `#elif`/`#else` in the same chain (PR #156 follow-up to
# issue #145: `#if 1` was previously read as opaque, so its `#else` was always
# assumed live even though cpp never takes it). The condition is captured
# (rather than prefix-matched) so a longer expression that merely *starts*
# with `0`/`1` — `#if 0 || FEATURE` — is not misread as known either way: such
# a branch genuinely might go either way, and disagreeing with cpp's own
# evaluation would corrupt the presumed-line translation for the real code
# after it, the same failure mode this whole exclusion exists to avoid. The
# captured group also consumes an embedded backslash-continuation (PR #156
# follow-up to issue #145: cpp splices a condition written as `#if \` +
# newline + `0` into one logical `#if 0` before ever evaluating it, so
# `_cond_events` strips any spliced newline out of the captured text — via
# `_CONTINUATION_RE` — before comparing it to the literal `"0"`/`"1"`).
#
# `#ifdef`/`#ifndef` and any other `#if`/`#elif <expr>` stay opaque — their
# condition needs macro state this scan does not have — so they are tracked
# only to balance nesting and to know whether an *earlier* arm in their own
# chain already decided it (matching `_param_list_text`'s own stance on such
# bodies: it leaves picking the *right* one to `def_line`, not to evaluating
# the condition).
_IF_RE = re.compile(r"^[ \t]*#[ \t]*if[ \t]+((?:\\\r?\n|[^\n])*)$", re.MULTILINE)
_IFDEF_RE = re.compile(r"^[ \t]*#[ \t]*(?:ifdef|ifndef)\b", re.MULTILINE)

# `#elif`'s condition is captured separately from `#if`'s: a literal `#elif 0`
# does not reactivate a dead branch the way `#else` does — its own condition is
# still false, so the branch it introduces stays dead regardless of what came
# before it (PR #156 follow-up to issue #145: treating every `#elif` as an
# unconditional `#else` let a still-dead `#elif 0` branch's `#line` count). A
# literal `#elif 1` is its mirror: it is live only if no earlier arm in the
# chain was already known-taken — otherwise cpp never reaches it regardless of
# its own condition being `1`. Like `_IF_RE`, the captured group consumes an
# embedded backslash-continuation so a split condition still classifies
# correctly.
_ELIF_RE = re.compile(r"^[ \t]*#[ \t]*elif[ \t]+((?:\\\r?\n|[^\n])*)$", re.MULTILINE)

# A bare `#else` has no condition of its own to read: it is taken whenever cpp
# reaches it, which happens whenever nothing before it was. This scan can
# prove that to be false — the else is dead — only once an earlier arm in the
# same chain was a literal `1`; absent that proof it must assume the else
# might be live (the same "assumed live" bias as an opaque `#if`), exactly as
# it must assume a still-unproven earlier arm might not be taken at all (PR
# #156 follow-up to issue #145: previously `#else` was unconditionally marked
# live even after a known-taken `#if 1`).
_ELSE_RE = re.compile(r"^[ \t]*#[ \t]*else\b", re.MULTILINE)
_ENDIF_RE = re.compile(r"^[ \t]*#[ \t]*endif\b", re.MULTILINE)

# A backslash immediately before a newline splices the following physical line
# onto this one (translation phase 2) — cpp decides whether a logical line is
# a directive *after* splicing, so a physical line that only looks like a
# directive because it follows a `\`-continued line (e.g. a multi-line
# `#define`'s replacement text spilling a `#line`-shaped token onto its own
# line) is not a directive at all and must not feed any of the `^`-anchored
# regexes above (PR #156 follow-up to issue #145). `\r?` tolerates a CRLF
# source even though `list_units` itself reads via `Path.read_text()`
# (universal newlines), since `annotate_array_extents` is also callable
# directly on arbitrary text.
_CONTINUATION_RE = re.compile(r"\\\r?\n")


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

    Reads the range's *start*, unlike `_loc_line` (which reads `point`): file
    identity does not move within a same-file macro expansion, and a macro whose
    own definition lives in a *different* file (a header) still drops the unit
    via `fn_in_target`, so the asymmetry is harmless for every case this module
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


def _line_breakpoints(source_no_comments: str) -> list[tuple[int, int]]:
    """``(physical_line, presumed_line)`` pairs from `source_no_comments`'s
    ``#line``/linemarker directives, one per directive reachable by cpp, in
    physical order.

    Each pair says the *next* physical line reads as `presumed_line`, and every
    physical line after it reads one higher — until the next directive — mirroring
    how ``#line N`` (or GNU's ``# N "file"``) resets clang's own line counter.

    A directive inside a literal ``#if 0``/``#elif 0`` branch is excluded: cpp
    never processes it, so such a directive never resets clang's own counter
    either — including it would let a duplicate ``#line`` guarding an inactive
    body collide with the one guarding the active definition (PR #156
    follow-up to issue #145). A directive inside a literal ``#if 1``/``#elif
    1`` branch's *sibling* ``#elif``/``#else`` arms is excluded for the mirror
    reason: cpp never reaches them once the literal-``1`` arm was taken, so a
    ``#line`` there never resets the counter either (PR #156 follow-up to
    issue #145: previously such a sibling was always assumed live). Nesting is
    tracked (an ``#if``/``#ifdef``/``#ifndef`` inside a dead branch stays dead
    regardless of its own condition), but any branch whose own condition is
    not the complete literal ``0`` or ``1`` is opaque — its condition needs
    macro state this textual scan does not have, or (for ``#elif``/``#else``)
    needs knowing whether an earlier arm in its own chain was already
    known-taken — and is assumed live absent that proof, exactly as
    `_param_list_text` itself leaves picking the right ``#ifdef`` branch to
    `def_line`, not to evaluating the condition.

    A physical line that is itself the *continuation* of a backslash-
    terminated previous line is never treated as a directive: cpp splices such
    a line onto the one above (translation phase 2) before it ever looks for a
    leading ``#``, so a ``#line``/``#if``/...-shaped token spliced in from,
    say, a multi-line ``#define``'s replacement text is just macro-body text
    to cpp, not a directive this module should react to (PR #156 follow-up to
    issue #145). Splicing inside a comment is handled earlier, by
    `_COMMENT_RE`/`_blank_comment`, before this function ever sees the text —
    the exclusion here only concerns a whole *directive-shaped line* starting
    on a continuation. A *token within* a directive that is itself split
    across a splice — an ``#if``/``#elif`` condition (``#if \\`` + newline +
    ``0``), or a ``#line``'s own digit sequence (``#line \\`` + newline +
    ``11``) — is a separate case: each is captured with the splice tolerated
    inline (see `_IF_RE`/`_ELIF_RE`/`_LINE_DIRECTIVE_RE`) and stripped via
    `_CONTINUATION_RE` before the captured text is read, not handled by this
    exclusion.
    """
    continuation_starts = {
        m.end() for m in _CONTINUATION_RE.finditer(source_no_comments)
    }

    def _events(
        pattern: re.Pattern[str], kind: str, *, keep_match: bool = False
    ) -> list[tuple[int, str, re.Match[str] | None]]:
        return [
            (m.start(), kind, m if keep_match else None)
            for m in pattern.finditer(source_no_comments)
            if m.start() not in continuation_starts
        ]

    def _cond_events(
        pattern: re.Pattern[str], kinds_by_literal: dict[str, str], opaque_kind: str
    ) -> list[tuple[int, str, None]]:
        return [
            (
                m.start(),
                kinds_by_literal.get(
                    _CONTINUATION_RE.sub("", m.group(1)).strip(), opaque_kind
                ),
                None,
            )
            for m in pattern.finditer(source_no_comments)
            if m.start() not in continuation_starts
        ]

    events = sorted(
        _cond_events(_IF_RE, {"0": "if0", "1": "if1"}, "ifop")
        + _events(_IFDEF_RE, "ifop")
        + _cond_events(_ELIF_RE, {"0": "elif0", "1": "elif1"}, "elifop")
        + _events(_ELSE_RE, "else")
        + _events(_ENDIF_RE, "endif")
        + _events(_LINE_DIRECTIVE_RE, "line", keep_match=True),
        key=lambda event: event[0],
    )
    # One `(dead, decided)` pair per open conditional. `dead` is whether the
    # *current* arm is known-dead; `decided` is whether some earlier arm in
    # this same chain was already known-taken (a literal `1`) — once true, cpp
    # can never reach any later `#elif`/`#else` in the chain, however live
    # that arm looks in isolation (PR #156 follow-up to issue #145).
    stack: list[tuple[bool, bool]] = []
    breakpoints: list[tuple[int, int]] = []
    for pos, kind, match in events:
        if kind == "if0":
            stack.append((True, False))
        elif kind == "if1":
            stack.append((False, True))
        elif kind == "ifop":
            stack.append((False, False))
        elif kind == "elif0":
            # Own condition is false, independent of every earlier branch's
            # state — this specific branch is never taken.
            if stack:
                _, decided = stack[-1]
                stack[-1] = (True, decided)
        elif kind == "elif1":
            if stack:
                _, decided = stack[-1]
                # Dead if an earlier arm already won the chain; otherwise this
                # literal `1` is the winner from here on.
                stack[-1] = (decided, True)
        elif kind in ("elifop", "else"):
            if stack:
                _, decided = stack[-1]
                stack[-1] = (decided, decided)
        elif kind == "endif":
            if stack:
                stack.pop()
        elif match is not None and not any(dead for dead, _ in stack):
            physical = source_no_comments.count("\n", 0, pos) + 1
            presumed = int(_CONTINUATION_RE.sub("", match.group(1)))
            breakpoints.append((physical, presumed))
    return breakpoints


def _presumed_line(physical_line: int, breakpoints: list[tuple[int, int]]) -> int:
    """`physical_line`'s presumed line, per `breakpoints` (see `_line_breakpoints`).

    Absent any directive at or before `physical_line`, presumed and physical
    coincide.
    """
    presumed = physical_line
    for directive_line, presumed_at_next in breakpoints:
        if directive_line >= physical_line:
            break
        presumed = presumed_at_next + (physical_line - directive_line - 1)
    return presumed


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

    A duplicate directive inside an *opaque* conditional (``#ifdef``) is not this
    case: this textual scan cannot tell which branch cpp took, so nothing here —
    this tiebreak included — can pick the right candidate in both compiles of
    such an input; it is a residual, not a target.
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
    breakpoints = _line_breakpoints(source_no_comments)
    # Sort key: candidates at or after `def_line` (key[0] == False) all sort before
    # any that are only before it, then nearest wins within each group — "at or
    # nearest after", falling back to nearest-before only if none qualify. `-c[0]`
    # (physical line) only breaks a tie left by the first two components — two
    # candidates translated to the same presumed distance from `def_line`.
    return min(
        candidates,
        key=lambda c: (
            _presumed_line(c[0], breakpoints) < def_line,
            abs(_presumed_line(c[0], breakpoints) - def_line),
            -c[0],
        ),
    )[1]


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
    same as before. A multi-line comment — a block comment, or a `//` comment
    that absorbed a backslash-continuation (PR #156 follow-up to issue #145) —
    becomes that many bare newlines instead — dropping its inline text (fine; a
    comment holds no declarator) while keeping every following line's number
    identical to `source_text`'s, which `_param_list_text` relies on to match a
    `Unit.def_line` from clang.
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

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
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field, replace
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

# Line (`// ...`) and block (`/* ... */`) comments, blanked before harvesting an
# array extent so a `[N]` inside a comment can never be misread as a declarator.
# A `//` comment absorbs a trailing backslash-newline as part of itself: cpp
# splices lines (translation phase 2) before it ever recognizes a comment
# (phase 3), so `// text \` continued onto a `#line`/`#if`-looking next line is,
# to cpp, one comment spanning both physical lines — not a directive (PR #156
# follow-up to issue #145: `_line_breakpoints`' own continuation exclusion runs
# on this already-blanked text, so a backslash swallowed by an *unspliced* `//`
# match here would already be gone by the time that exclusion could see it).
_COMMENT_RE = re.compile(r"//(?:\\\r?\n|[^\n])*|/\*.*?\*/", re.DOTALL)

# String (`"..."`) and char (`'...'`) literals, blanked the same way and for the
# same reason as comments (below): a literal can spell `fn_name(` — a diagnostic
# like `printf("call main();\n")` — without that being a real occurrence of
# `fn_name`, and every `_paren_balanced_occurrences` consumer (`find_definition_brace`,
# `annotate_array_extents`, `rename_all_declarations_and_definitions`) must not
# mistake it for one (issue #95 review) — the last of which would otherwise
# silently rewrite the literal's own byte content instead of leaving it alone.
# `\\.` (not `[^\\]`) is what lets an escaped quote (`\"`) or backslash (`\\`)
# stay inside the literal instead of ending the match early; stopping at an
# unescaped newline is deliberate, like `_COMMENT_RE`'s `//` alternative — a
# literal genuinely never spans one without cpp's line-splicing, which this
# textual scan does not perform.
_STRING_RE = re.compile(r'"(?:\\.|[^"\\\n])*"' r"|'(?:\\.|[^'\\\n])*'")

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

# A `#line N` (ISO C) or GNU linemarker `# N "file" flags...` directive, in its
# literal-digit-sequence form. Matches right after `#`, so `#define`/`#if`/...
# (which start with a non-digit, non-"line" word) never do. The captured digit
# run also tolerates an embedded backslash-continuation (PR #156 follow-up to
# issue #145: cpp splices `#line \` + newline + `11` into one logical `#line
# 11` before ever reading it, so a breakpoint recorded here must be too — the
# same splicing hazard `_IF_RE`/`_ELIF_RE` already guard against for a
# conditional's own text). The continuation is stripped from the captured
# group — via `_CONTINUATION_RE` — before the digits are read as an `int`, in
# `_line_breakpoints`.
_LINE_DIRECTIVE_RE = re.compile(
    r"^[ \t]*#[ \t]*(?:line[ \t]+)?((?:\\\r?\n)*\d(?:\\\r?\n|\d)*)\b", re.MULTILINE
)

# A macro-valued `#line NAME` (issue #165 follow-up to #156/#157): GNU's
# linemarker form is always digits (never a macro), so only the ISO `line`
# spelling is matched here — the `\d` case above already owns the digit form,
# and requiring the first non-continuation character to be a letter/underscore
# keeps the two regexes mutually exclusive at the same start position. The
# named macro is resolved against `_line_breakpoints`' own tiny macro-value
# table (see `_DEFINE_RE`), which only ever tracks a plain integer literal — a
# `#line` naming anything else (an unknown identifier, a function-like macro,
# one bound to a non-literal expression) stays unresolved, the same graceful
# no-breakpoint fallback this scan already uses for every condition it cannot
# evaluate.
_LINE_DIRECTIVE_MACRO_RE = re.compile(
    r"^[ \t]*#[ \t]*line[ \t]+((?:\\\r?\n)*[A-Za-z_]\w*)\b", re.MULTILINE
)

# `#define NAME <rest of line>` and `#undef NAME`, tracked for exactly two
# lookups — a macro-valued `#line NAME` (`_LINE_DIRECTIVE_MACRO_RE`) and a
# `#ifdef`/`defined` test (`_IFDEF_RE`) — and deliberately not a general macro
# table. The two read different things off the same directives: the `#line`
# value table needs an integer-literal replacement, while definedness needs only
# that the `#define` happened, so an empty or function-like `#define` decides a
# `#ifdef` while leaving `#line NAME` unresolved. `_DEFINE_RE`'s second group
# is the *entire* remainder of the line, exactly like `_IF_RE`'s condition
# group: `_line_breakpoints` strips its continuation and whitespace, then
# accepts it only if what is left is a bare unsigned-integer literal (see
# `_INT_LITERAL_RE`) — a function-like macro's `(params) body`, an empty
# replacement (`#define DEBUG`), or any non-literal expression all fail that
# check and simply drop any existing binding for the name, exactly as an
# explicit `#undef` would. A binding is *never* established while a
# `#define`/`#undef` cannot itself be proven live or dead (`_line_breakpoints`'
# own `stack` is non-empty) — only ever unbound: this scan does not have the
# macro state to know which of an `#ifdef`'s two arms cpp actually takes, so
# recording whichever arm happened to run last would risk resolving `#line
# NAME` to a value cpp itself would never produce, the same failure mode
# `_IF_RE`'s own comment names as the reason `#ifdef`/non-literal `#if` stay
# opaque. A `#define`/`#undef` inside a branch already proven dead (a literal
# `#if 0`) is excluded exactly like a `#line` there — cpp never reaches it
# either, so it must not disturb an existing trustworthy binding. This is
# still only as trustworthy as the scan's usual blind spot: it reads one
# file's own text, so a `#define`/`#undef` of the same name arriving via an
# `#include` between this file's own binding and its `#line NAME` is invisible
# and can leave a stale value in the table — no worse than every other gap
# this module already has for `#include`d macro state, but worth naming here
# since this is the first place that state can produce a wrong *value*
# instead of just a missed opaque condition. The definedness half does *not*
# carry that residual: `_INCLUDE_RE` clears it at every `#include`, because
# there the fallback is a merely-opaque conditional rather than a dropped
# breakpoint (issue #157).
_DEFINE_RE = re.compile(
    r"^[ \t]*#[ \t]*define[ \t]+(\w+)((?:\\\r?\n|[^\n])*)$", re.MULTILINE
)
_UNDEF_RE = re.compile(r"^[ \t]*#[ \t]*undef[ \t]+(\w+)\b", re.MULTILINE)

# A bare unsigned-integer literal, nothing else — what `_DEFINE_RE`'s captured
# replacement text must reduce to (after continuation-stripping and
# whitespace-trimming) for `_line_breakpoints` to trust it as a `#line`-usable
# value. `fullmatch` so a stray trailing character can never slip through.
_INT_LITERAL_RE = re.compile(r"[0-9]+")

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
# `_CONTINUATION_RE` — before comparing it to the literal `"0"`/`"1"`). A
# condition wrapped in balanced parens spanning its *entire* text — `(0)`,
# `((1))` — is peeled down to the bare digit first, via
# `_strip_literal_parens` (issue #165 follow-up to #156: cpp always evaluates
# `#if (0)` as false, so leaving the parens in place and missing the exact
# `"0"`/`"1"` match would wrongly leave it opaque). A paren pair that does not
# span the whole condition — `(0 || FEATURE)`, `(0) || FEATURE` — is left
# alone by that same whole-span check and still falls through to opaque,
# exactly like the unparenthesized `0 || FEATURE` case above.
#
# A `#ifdef NAME`/`#ifndef NAME` — and the `#if defined NAME`/`#if
# !defined(NAME)` spelling of the same predicate — resolves whenever this file's
# own directives have already *proven* whether `NAME` is defined at that point
# (issue #157: every such conditional used to be opaque, so a `#line` guarding
# an inactive alternative definition still donated a phantom breakpoint and left
# `_select_definition` with a presumed-line tie only its `-m.line` physical-order
# guess could break). Proof runs in one direction only: a `#define NAME`/`#undef
# NAME` this scan can itself place (see `known_defined` in `_line_breakpoints`)
# establishes defined/undefined, and nothing else does. In particular the
# *absence* of any `#define NAME` proves nothing — a command-line `-D NAME`, a
# compiler builtin, or an `#include`d header can each define a name this file
# never mentions, and `list_units` really does forward `-D` flags.
#
# That one-directional rule is what the cost asymmetry demands, and the same
# asymmetry is what `_INCLUDE_RE` below rests on: a wrongly *live* verdict costs
# nothing, since an opaque conditional is already assumed live and so lands on
# exactly today's behaviour, whereas a wrongly *dead* verdict deletes a real
# breakpoint and corrupts the presumed-line translation of every live line after
# it. Only the dead verdict is new here, so only the dead verdict has to be
# proven — and it is proven from this file's text or not at all.
#
# Any other `#if`/`#elif <expr>` still stays opaque — `defined(A) && defined(B)`
# needs a real expression evaluator, not a lookup — so it is tracked only to
# balance nesting and to know whether an *earlier* arm in its own chain already
# decided it (matching `_param_list_text`'s own stance on such bodies: it leaves
# picking the *right* one to `def_line`, not to evaluating the condition).
_IF_RE = re.compile(r"^[ \t]*#[ \t]*if[ \t]+((?:\\\r?\n|[^\n])*)$", re.MULTILINE)

# `#ifdef`/`#ifndef`, exposing the same `negated`/`name` groups as `_DEFINED_RES`
# so `_line_breakpoints` reads both spellings of the predicate through one code
# path. `name` is deliberately *optional*: an operand-less `#ifdef` is malformed,
# but cpp still opens a nesting level a later `#endif` closes, and dropping the
# event would unbalance every conditional after it — a nameless match simply
# resolves to opaque, exactly like a name this scan knows nothing about. A
# leading backslash-continuation is tolerated before the name, mirroring
# `_LINE_DIRECTIVE_MACRO_RE`.
_IFDEF_RE = re.compile(
    r"^[ \t]*#[ \t]*if(?P<negated>n)?def\b[ \t]*(?:\\\r?\n)*(?P<name>[A-Za-z_]\w*)?",
    re.MULTILINE,
)

# `defined NAME` / `defined(NAME)`, optionally negated — matched against an
# `#if`/`#elif`'s *entire* condition (`fullmatch`, for the same reason
# `_INT_LITERAL_RE` is one: a trailing operand must never slip through as a
# decided condition). Two patterns rather than one alternation because Python's
# `re` cannot reuse the `name` group name across alternatives; the parenthesized
# form is tried first so `defined (X)` is read as its operand rather than as a
# bare `(X)`. `_strip_literal_parens` runs before both, so `#if (defined(X))`
# arrives already peeled — while `#if !(defined(X))`, whose parens wrap only
# `!`'s operand, does not and correctly stays opaque.
_DEFINED_RES = (
    re.compile(r"(?P<negated>!)?\s*defined\s*\(\s*(?P<name>\w+)\s*\)"),
    re.compile(r"(?P<negated>!)?\s*defined\s+(?P<name>\w+)"),
)

# `#include`/`#include_next`, tracked for one purpose: to make `_line_breakpoints`
# forget every definedness fact it had proven (see `_IFDEF_RE`). A header may
# `#define` or `#undef` any name, so nothing this file proved before the include
# still holds after it. The `#line NAME` *value* table (`_DEFINE_RE`) is
# deliberately left alone by the same asymmetry: forgetting a definedness fact
# only falls back to opaque — the assumed-live default that predates issue #157,
# which costs nothing — whereas forgetting a value binding would silently drop a
# real breakpoint this scan can otherwise resolve. `_DEFINE_RE`'s own comment
# names the residual that leaves.
_INCLUDE_RE = re.compile(r"^[ \t]*#[ \t]*include(?:_next)?\b", re.MULTILINE)

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

# A GNU suffix attribute's opening: ``__attribute__`` then its own ``(``. A
# definition may carry one or more of these between its declarator and body —
# ``static void f(int *p) __attribute__((noinline)) { ... }`` — which clang
# (and so ESBMC) accepts even though gcc requires attributes *before* the
# declarator in a definition (issue #163). Matched separately from the ``(``
# it opens so `_skip_suffix_attributes` can balance that parenthesis on its
# own — its argument list can itself nest parens (``__attribute__((aligned(8)))``).
_ATTRIBUTE_RE = re.compile(r"__attribute__\s*\(")


def _strip_literal_parens(text: str) -> str:
    """`text` with any balanced parens spanning its *entire* length peeled off,
    recursively — `"(0)"` and `"((1))"` both reduce to their bare digit.

    A pair is only peeled when its own open paren's matching close is the
    text's last character: `"(0 || FEATURE)"` reduces once (to `"0 ||
    FEATURE"`, then stops, since that no longer starts with `(`), but
    `"(0) || FEATURE"` is never touched at all — its leading `(` closes at
    index 2, well before the end, so the pair wraps only a subexpression, not
    the whole condition. Both are deliberately left as the non-literal text
    they are — `_cond_events` still calls this before its exact `"0"`/`"1"`
    lookup, so anything this leaves unpeeled falls through to opaque exactly
    like an unparenthesized compound condition already does.
    """
    text = text.strip()
    while text.startswith("(") and text.endswith(")"):
        depth = 0
        wraps_whole = False
        for i, ch in enumerate(text):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    wraps_whole = i == len(text) - 1
                    break
        if not wraps_whole:
            break
        text = text[1:-1].strip()
    return text


def _macro_test(condition: str) -> re.Match[str] | None:
    """`condition` read as a whole-text ``defined``/``!defined`` test, or ``None``.

    ``None`` means "not that shape at all" — a bare `0`/`1` (already handled by
    the caller), a compound expression, or anything else `_DEFINED_RES` does not
    match end to end — which the caller turns into an opaque conditional.
    """
    for pattern in _DEFINED_RES:
        match = pattern.fullmatch(condition)
        if match is not None:
            return match
    return None


def _macro_verdict(match: re.Match[str], known_defined: dict[str, bool]) -> str:
    """``"1"`` (live), ``"0"`` (dead), or ``"op"`` (opaque) for `match`'s macro test.

    `match` is a `_IFDEF_RE` or `_DEFINED_RES` match — both spell the predicate
    with the same ``negated``/``name`` groups. The verdict is ``"op"`` whenever
    the operand is missing (an operand-less, malformed ``#ifdef``) or its
    definedness is not in `known_defined`; see `_IFDEF_RE` for why a name simply
    never ``#define``d in this file lands there rather than reading as undefined.
    """
    name = match.group("name")
    if name is None:
        return "op"
    defined = known_defined.get(name)
    if defined is None:
        return "op"
    return "1" if defined != (match.group("negated") is not None) else "0"


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
    """

    name: str
    params: tuple[Param, ...]
    calls: tuple[str, ...] = ()
    internal_linkage: bool = False
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


def _blank_preserving_newlines(match: re.Match[str]) -> str:
    """`match`'s text with every character replaced by a space, newlines kept.

    Shared by `mask_comments` and `mask_string_literals`: both blank a matched
    span to the *same length* (so an offset found in the masked text stays
    usable against the original) and keep any embedded newline (so line
    numbering stays untouched).
    """
    return "".join("\n" if ch == "\n" else " " for ch in match.group(0))


def mask_comments(source_text: str) -> str:
    """`source_text` with every comment blanked out, **preserving every offset**.

    Each comment character becomes a space (newlines kept, so line numbering is
    untouched), rather than the whole comment collapsing to one space. Length
    preservation is what lets an index found in the masked text be used verbatim
    against the original — which `find_definition_brace` relies on to inject
    text at an exact position in the user's source.
    """
    return _COMMENT_RE.sub(_blank_preserving_newlines, source_text)


def mask_string_literals(source_text: str) -> str:
    """`source_text` with every string/char literal blanked out, same contract
    as `mask_comments` — see `_STRING_RE` for why this exists."""
    return _STRING_RE.sub(_blank_preserving_newlines, source_text)


def _stripped_for_scan(source_text: str) -> str:
    """`source_text` with comments and string/char literals both blanked out,
    offsets preserved — the masked view every `_paren_balanced_occurrences`
    consumer scans against, so an `fn_name(`-shaped occurrence *inside* a
    comment or a literal cannot be mistaken for a real one (issue #95 review).
    Order doesn't matter: each pass only ever blanks to spaces, so neither can
    uncover or hide a span the other would have matched.
    """
    return mask_string_literals(mask_comments(source_text))


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
    matches: list[_DefinitionMatch], source_no_comments: str, def_line: int | None
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
    is only sometimes this case. `_line_breakpoints` now excludes it whenever
    this file's own ``#define``/``#undef`` directives prove the guard's state
    (issue #157), so the common single-build-flag shape no longer reaches the
    tiebreak at all. What stays a residual is a guard this file never decides —
    one defined on the command line with ``-D``, by a compiler builtin, or in an
    ``#include``d header: there the scan cannot tell which branch cpp took, so
    nothing here, this tiebreak included, can pick the right candidate in both
    compiles of such an input.
    """
    if not matches:
        return None
    if def_line is None:
        return matches[0]
    breakpoints = _line_breakpoints(source_no_comments)
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
    source_text: str, fn_name: str, def_line: int | None = None
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
    """
    masked = _stripped_for_scan(source_text)
    chosen = _select_definition(
        _definition_candidates(masked, fn_name), masked, def_line
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
    regardless of its own condition).

    A ``#ifdef NAME``/``#ifndef NAME`` — or the ``#if defined NAME``/``#if
    !defined(NAME)`` spelling of the same predicate — is decided too, but only
    when this file's own ``#define``/``#undef`` directives already proved
    whether ``NAME`` is defined at that point (issue #157, tracked in
    `known_defined`). The proof is one-directional on purpose: a name this file
    never mentions stays *unknown*, never "undefined", because a command-line
    ``-D``, a compiler builtin, or an ``#include``d header can define it — and
    an ``#include`` anywhere drops every fact proven above it. `_IFDEF_RE`
    carries the full rationale; the short version is that a wrong *live* verdict
    only reproduces the opaque default, while a wrong *dead* verdict deletes a
    real breakpoint.

    Every other branch whose own condition is not the complete literal ``0`` or
    ``1`` and not a decidable ``defined`` test stays opaque — its condition needs
    macro state this textual scan does not have, or (for ``#elif``/``#else``)
    needs knowing whether an earlier arm in its own chain was already
    known-taken — and is assumed live absent that proof, exactly as
    `_param_list_text` itself leaves picking the right branch to `def_line`,
    not to evaluating the condition.

    A physical line that is itself the *continuation* of a backslash-
    terminated previous line is never treated as a directive: cpp splices such
    a line onto the one above (translation phase 2) before it ever looks for a
    leading ``#``, so a ``#line``/``#if``/...-shaped token spliced in from,
    say, a multi-line ``#define``'s replacement text is just macro-body text
    to cpp, not a directive this module should react to (PR #156 follow-up to
    issue #145). Splicing inside a comment is handled earlier, by
    `_COMMENT_RE`/`mask_comments`, before this function ever sees the text —
    the exclusion here only concerns a whole *directive-shaped line* starting
    on a continuation. A *token within* a directive that is itself split
    across a splice — an ``#if``/``#elif`` condition (``#if \\`` + newline +
    ``0``), or a ``#line``'s own digit sequence (``#line \\`` + newline +
    ``11``) — is a separate case: each is captured with the splice tolerated
    inline (see `_IF_RE`/`_ELIF_RE`/`_LINE_DIRECTIVE_RE`) and stripped via
    `_CONTINUATION_RE` before the captured text is read, not handled by this
    exclusion.

    A ``#line`` naming a macro (``#line NAME``) resolves against a tiny
    ``#define``/``#undef``-tracking table this scan builds as it walks
    forward (issue #165 follow-up to #156/#157) — see `_DEFINE_RE`'s comment
    for exactly which bindings are trusted. A literal ``#if (0)``/``#elif
    (0)``/... — parens spanning the whole condition, however deeply nested —
    is normalized to the bare digit before the ``"0"``/``"1"`` comparison (see
    `_strip_literal_parens`), the same issue's second follow-up.
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
        pattern: re.Pattern[str], prefix: str
    ) -> list[tuple[int, str, re.Match[str] | None]]:
        """`pattern`'s conditionals as ``<prefix>0``/``1``/``def``/``op`` events.

        A literal `0`/`1` classifies here and for good; a ``defined``-shaped
        condition classifies as ``<prefix>def`` and carries its match, because
        its verdict depends on macro state the forward walk has not built yet.
        """
        found: list[tuple[int, str, re.Match[str] | None]] = []
        for m in pattern.finditer(source_no_comments):
            if m.start() in continuation_starts:
                continue
            condition = _strip_literal_parens(_CONTINUATION_RE.sub("", m.group(1)))
            if condition in ("0", "1"):
                found.append((m.start(), prefix + condition, None))
                continue
            test = _macro_test(condition)
            if test is None:
                found.append((m.start(), prefix + "op", None))
            else:
                found.append((m.start(), prefix + "def", test))
        return found

    events = sorted(
        _cond_events(_IF_RE, "if")
        + _events(_IFDEF_RE, "ifdef", keep_match=True)
        + _cond_events(_ELIF_RE, "elif")
        + _events(_ELSE_RE, "else")
        + _events(_ENDIF_RE, "endif")
        + _events(_INCLUDE_RE, "include")
        + _events(_LINE_DIRECTIVE_RE, "line", keep_match=True)
        + _events(_LINE_DIRECTIVE_MACRO_RE, "line_macro", keep_match=True)
        + _events(_DEFINE_RE, "define", keep_match=True)
        + _events(_UNDEF_RE, "undef", keep_match=True),
        key=lambda event: event[0],
    )
    # One `(dead, decided)` pair per open conditional. `dead` is whether the
    # *current* arm is known-dead; `decided` is whether some earlier arm in
    # this same chain was already known-taken (a literal `1`) — once true, cpp
    # can never reach any later `#elif`/`#else` in the chain, however live
    # that arm looks in isolation (PR #156 follow-up to issue #145).
    stack: list[tuple[bool, bool]] = []
    # `#line NAME`'s tiny macro-value table (issue #165 follow-up to #156),
    # populated by `#define`/emptied by `#undef` as the scan walks forward —
    # see `_DEFINE_RE`.
    macros: dict[str, int] = {}
    # What this file's own directives have *proven* about each name's
    # definedness at the current position: `True` defined, `False` undefined,
    # absent unknown. Populated under the same fail-closed rule as `macros` —
    # only a `#define`/`#undef` the walk can place (top level, and not inside a
    # branch it cannot prove taken) ever establishes an entry; anything less
    # only removes one. See `_IFDEF_RE` for why absence is never read as
    # "undefined" (issue #157).
    known_defined: dict[str, bool] = {}
    breakpoints: list[tuple[int, int]] = []
    for pos, kind, match in events:
        dead = any(is_dead for is_dead, _ in stack)
        if kind in ("ifdef", "elifdef") and match is not None:
            # Resolved here, not when the event was built: a `#ifdef`'s verdict
            # is a function of the macro state at *its* position in the walk.
            kind = kind.removesuffix("def") + _macro_verdict(match, known_defined)
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
        elif kind == "include":
            # A header can `#define`/`#undef` anything, so every definedness
            # fact proven above it stops holding below it (`_INCLUDE_RE`).
            if not dead:
                known_defined.clear()
        elif kind == "define":
            if match is not None and not dead:
                name = match.group(1)
                if stack:
                    # Some enclosing branch is not provably dead but also not
                    # provably the chain's only reachable arm — this scan
                    # cannot tell whether cpp ever executes this `#define`, so
                    # it must not *establish* a binding, only drop a
                    # previously-trustworthy one for the same name (same
                    # fail-closed stance `_IF_RE`'s own comment gives for
                    # `#ifdef`).
                    macros.pop(name, None)
                    known_defined.pop(name, None)
                else:
                    # Definedness and value are established on different terms:
                    # *any* top-level `#define` proves the name defined, while
                    # only an integer-literal replacement is usable as a `#line`
                    # value — so an empty `#define DEBUG` or a function-like
                    # `#define L(x) (x)` still decides a `#ifdef` even though it
                    # leaves `macros` unbound (issue #157).
                    known_defined[name] = True
                    value_text = _CONTINUATION_RE.sub("", match.group(2)).strip()
                    if _INT_LITERAL_RE.fullmatch(value_text):
                        macros[name] = int(value_text)
                    else:
                        macros.pop(name, None)
        elif kind == "undef":
            if match is not None and not dead:
                name = match.group(1)
                macros.pop(name, None)
                if stack:
                    known_defined.pop(name, None)
                else:
                    known_defined[name] = False
        elif kind == "line_macro":
            if match is not None and not dead:
                name = _CONTINUATION_RE.sub("", match.group(1))
                if name in macros:
                    physical = source_no_comments.count("\n", 0, pos) + 1
                    breakpoints.append((physical, macros[name]))
        elif match is not None and not dead:
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

    The declarator text between ``(`` and its matching ``)`` for the
    definition-shaped occurrence `def_line` anchors to — see `_definition_candidates`
    for what counts as definition-shaped and `_select_definition` for how
    `def_line` (issue #145) picks the compiled definition over an inactive
    ``#if 0`` alternative.
    """
    chosen = _select_definition(
        _definition_candidates(source_no_comments, fn_name),
        source_no_comments,
        def_line,
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
    to the one clang actually compiled (issue #145). String/char literals are
    stripped alongside comments (`_stripped_for_scan`) for the same reason
    `rename_all_declarations_and_definitions` does: a literal spelling
    `name(...)` is not a parameter list to isolate.
    """
    stripped = _stripped_for_scan(source_text)
    annotated: list[Unit] = []
    for unit in units:
        param_list = _param_list_text(stripped, unit.name, unit.def_line)
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

"""C preprocessor lexical scanning for `forseti.esbmc.units`.

This is the comment/string-literal *masking* layer and the ``#line``/``#if``
*directive scanner* that `list_units` relies on to map a physical source line to
the presumed (``#line``-adjusted) line ESBMC's clang frontend counts from, and to
tell which ``#if``/``#ifdef`` arm cpp actually compiles. It was carved out of
`units.py` (which held ~800 lines of it inline, comments included) so the clang-AST
parsing concern and this preprocessor-emulation concern each read on their own.

The split is one-directional by construction: `units` imports from here, never the
reverse (see `test_preprocessor.py`'s acyclicity check). Everything here is a pure
function of source *text* — no ESBMC, no I/O — which is what let the whole cluster
move without touching a single caller's behaviour. The dense per-regex comments
carry the cpp-conformance rationale (issues #95, #145, #156, #157, #163, #165,
#226 and the PR #225 review); they travel with the code they explain.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

# Line (`// ...`) and block (`/* ... */`) comments, blanked before harvesting an
# array extent so a `[N]` inside a comment can never be misread as a declarator.
# A `//` comment absorbs a trailing backslash-newline as part of itself: cpp
# splices lines (translation phase 2) before it ever recognizes a comment
# (phase 3), so `// text \` continued onto a `#line`/`#if`-looking next line is,
# to cpp, one comment spanning both physical lines — not a directive (PR #156
# follow-up to issue #145). Swallowing the backslash *here* is what keeps
# `_splice_continuations` from later joining that next line onto the comment's
# own: this pass blanks the backslash, so by the time the splicer runs there is
# no continuation left to act on.
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
# masking pass does not perform. `_line_breakpoints` splices its own copy
# afterwards (`_splice_continuations`), which is what keeps the unmasked tail of
# a *spliced* literal from reading as a line-leading directive there.
_STRING_RE = re.compile(r'"(?:\\.|[^"\\\n])*"' r"|'(?:\\.|[^'\\\n])*'")


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


# Every directive pattern below is built from these four shared fragments, each
# written once because a widening applied to one spelling and not another is
# silent. cpp accepts the same prologue and the same identifier grammar in
# *every* directive, so a pattern that sees `#ifdef` but not `%:ifdef` leaves a
# conditional unbalanced — its `#endif` then pops somebody else's frame, and a
# `#define` inside it looks top-level — while a pattern that reads `#define W$`
# as defining `W` proves a definedness fact cpp never established. Both end in
# the same place: a guard decided *dead* on an arm cpp really takes, which
# deletes a real `#line` breakpoint (the asymmetry `_IF_RE` spells out).
#
# Every claim below was checked against gcc 16.2.1, clang 22.1.8 *and* `esbmc
# --parse-tree-only` (8.3.0), the last because esbmc's own frontend is the only
# line numbering this scan has to agree with (PR #225 review):
#
#   `_PP_SPACE`   The whitespace cpp allows before `#` and between `#` and the
#                 directive keyword. Vertical tab and form feed are
#                 preprocessing whitespace exactly as space and tab are — all
#                 three tools take `#\vif 0` and `#\fdefine FF 1`. Newline is
#                 excluded: it ends the directive.
#   `_DIRECTIVE`  That whitespace plus `#` — or `%:`, C's digraph spelling of
#                 it. `%:define W 1` and `%:if 0` are ordinary directives to all
#                 three. Trigraphs (`??=define`) are deliberately *not*
#                 accepted: esbmc does not honour them at all (it fails to parse
#                 `??=define W 1`), and gcc/clang honour them only under a
#                 strict `-std=c*`, never the `gnu*` default. Reading one as a
#                 directive would disagree with esbmc in the costly direction.
#   `_MACRO_NAME` A *complete* preprocessing identifier: `$` included (all three
#                 accept it by default, so `#define W$ 1` defines `W$` and
#                 leaves `W` undefined), and `\uXXXX`/`\UXXXXXXXX` universal
#                 character names too (`#define W\u00E9 1` likewise defines
#                 `Wé`, not `W`). A literal UTF-8 spelling needs no rule of its
#                 own — Python's `\w` is Unicode-aware. Capturing only the
#                 leading `\w` run would record a definition of `W` and decide a
#                 later `#ifndef W` dead on the arm cpp actually takes.
#   `_NAME_END`   The end of such an identifier — no further identifier
#                 character, and no backslash that could open a UCN. Greedy
#                 repetition already reaches that point, so this mostly restates
#                 it; stating it explicitly is what stops a later edit that
#                 appends anything after a name capture from silently
#                 reintroducing the truncation above. Applying it to directive
#                 *keywords* as well is deliberate: `#endif\u00E9` is not
#                 `#endif` — clang rejects it as an invalid directive outright.
#
# None of them mention the backslash-continuation cpp splices out in translation
# phase 2: `_line_breakpoints` runs every pattern over already-spliced text (see
# `_splice_continuations`), so each directive is matched in the single logical
# form cpp itself reads.
_PP_SPACE = r"[ \t\v\f]"
_DIRECTIVE = rf"^{_PP_SPACE}*(?:#|%:){_PP_SPACE}*"
_UCN = r"\\u[0-9A-Fa-f]{4}|\\U[0-9A-Fa-f]{8}"
_MACRO_NAME = rf"(?:[A-Za-z_$]|{_UCN})(?:[\w$]|{_UCN})*"
_NAME_END = r"(?![\w$\\])"

# A `#line N` (ISO C) or GNU linemarker `# N "file" flags...` directive, in its
# literal-digit-sequence form. Matches right after the prologue, so
# `#define`/`#if`/... (which start with a non-digit, non-"line" word) never do.
_LINE_DIRECTIVE_RE = re.compile(
    rf"{_DIRECTIVE}(?:line{_PP_SPACE}+)?(\d+){_NAME_END}", re.MULTILINE
)

# A macro-valued `#line NAME` (issue #165 follow-up to #156/#157): GNU's
# linemarker form is always digits (never a macro), so only the ISO `line`
# spelling is matched here — the `\d` case above already owns the digit form,
# and requiring the first character to be a letter/underscore/`$` keeps the two
# regexes mutually exclusive at the same start position. The named macro is
# resolved against `_line_breakpoints`' own tiny macro-value
# table (see `_DEFINE_RE`), which only ever tracks a plain integer literal — a
# `#line` naming anything else (an unknown identifier, a function-like macro,
# one bound to a non-literal expression) stays unresolved, the same graceful
# no-breakpoint fallback this scan already uses for every condition it cannot
# evaluate.
_LINE_DIRECTIVE_MACRO_RE = re.compile(
    rf"{_DIRECTIVE}line{_PP_SPACE}+({_MACRO_NAME}){_NAME_END}", re.MULTILINE
)

# `#define NAME <rest of line>` and `#undef NAME`, tracked for exactly two
# lookups — a macro-valued `#line NAME` (`_LINE_DIRECTIVE_MACRO_RE`) and a
# `#ifdef`/`defined` test (`_IFDEF_RE`) — and deliberately not a general macro
# table. The two read different things off the same directives: the `#line`
# value table needs an integer-literal replacement, while definedness needs only
# that the `#define` happened, so an empty or function-like `#define` decides a
# `#ifdef` while leaving `#line NAME` unresolved. The name is `_MACRO_NAME`, a
# complete identifier: `#define W$ 1` defines `W$`, never `W`, and reading it as
# `W` would prove a fact cpp never established (PR #225 review). `_DEFINE_RE`'s
# second group is the *entire* remainder of the line, exactly like `_IF_RE`'s
# condition group: `_line_breakpoints` strips its whitespace, then accepts it
# only if what is left is a bare unsigned-integer literal (see
# `_INT_LITERAL_RE`) — a function-like macro's `(params) body`, an empty
# replacement (`#define DEBUG`), or any non-literal expression all fail that
# check and simply drop any existing binding for the name, exactly as an
# explicit `#undef` would. A binding is *never* established while a
# `#define`/`#undef` cannot itself be proven live or dead (`_line_breakpoints`'
# own `stack` is non-empty, or its `macro_stack_opaque` latch is set) — only
# ever unbound: this scan does not have the macro state to know which of an
# `#ifdef`'s two arms cpp actually takes, so
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
# carry that residual: `_INCLUDE_RE` clears it at every `#include` cpp can
# actually reach (one inside a branch already proven dead is skipped, exactly
# as a `#define` there is), because there the fallback is a merely-opaque
# conditional rather than a dropped breakpoint (issue #157).
_DEFINE_RE = re.compile(
    rf"{_DIRECTIVE}define{_PP_SPACE}+({_MACRO_NAME}){_NAME_END}([^\n]*)$",
    re.MULTILINE,
)
_UNDEF_RE = re.compile(
    rf"{_DIRECTIVE}undef{_PP_SPACE}+({_MACRO_NAME}){_NAME_END}", re.MULTILINE
)

# A bare unsigned-integer literal, nothing else — what `_DEFINE_RE`'s captured
# replacement text must reduce to (after whitespace-trimming) for
# `_line_breakpoints` to trust it as a `#line`-usable value. `fullmatch` so a
# stray trailing character can never slip through.
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
# after it, the same failure mode this whole exclusion exists to avoid. A
# condition split across a backslash-continuation — `#if \` + newline + `0`,
# one logical `#if 0` to cpp — arrives already joined, since every pattern here
# runs over spliced text (`_splice_continuations`). A condition wrapped in
# balanced parens spanning its *entire* text — `(0)`,
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
# guess could break). It resolves from *proof* and never from a default: a
# `#define NAME`/`#undef NAME` this scan can itself place (see `known_defined`
# in `_line_breakpoints`) establishes defined/undefined, and so does
# `predefined`, the seed `probe_predefined_guards` measures by asking the real
# preprocessor what the command line and the compiler's builtins had already
# defined before line 1 (issue #226 — the two sources #157 could only name as
# residuals, since neither leaves a trace in the file and `list_units` really
# does forward `-D` flags). The *absence* of both still proves nothing: an
# `#include`d header can define a name this file never mentions, which is why
# `_INCLUDE_RE` below drops every fact — seed included — at an `#include`, and
# why that last shape stays this rule's residual.
#
# Proof-or-nothing is what the cost asymmetry demands, and the same asymmetry is
# what `_INCLUDE_RE` rests on: a wrongly *live* verdict costs nothing, since an
# opaque conditional is already assumed live and so lands on exactly the
# pre-#157 behaviour, whereas a wrongly *dead* verdict deletes a real breakpoint
# and corrupts the presumed-line translation of every live line after it. Only
# the dead verdict is new here, so only the dead verdict has to be proven — from
# this file's text or from a measurement of the same build, never from a guess.
#
# Any other `#if`/`#elif <expr>` still stays opaque — `defined(A) && defined(B)`
# needs a real expression evaluator, not a lookup — so it is tracked only to
# balance nesting and to know whether an *earlier* arm in its own chain already
# decided it (matching `_param_list_text`'s own stance on such bodies: it leaves
# picking the *right* one to `def_line`, not to evaluating the condition).
# The condition is separated from ``if`` by *optional* whitespace, not required
# whitespace: `#if(FOO)` and `#if!defined(X)` are legal directives, and a
# conditional this scan fails to see is far worse than one it cannot evaluate —
# it opens a nesting level whose `#endif` then pops somebody else's entry, so a
# `#define` inside it looks top-level to `known_defined` and can decide a later
# `#ifdef` *dead* on a branch cpp never took. `_NAME_END` keeps `#ifdef` and
# `#ifndef` out (they are `_IFDEF_RE`'s, and matching both would push the level
# twice), and `#if$X` too — `if$X` is one identifier to cpp, not a directive.
#
# The `if`/`elif` spellings of each shape are built from one fragment apiece
# rather than written out twice: `#elif`'s pattern has to stay character-for-
# character in step with `#if`'s, and a widening applied to only one of a pair
# is silent — `_macro_verdict` reads the same `negated`/`name` groups off
# `_IFDEF_RE` and `_ELIFDEF_RE` alike. Same `rf""` idiom as `_BRACKET_QUALIFIER`.
_CONDITION_TAIL = rf"{_NAME_END}{_PP_SPACE}*([^\n]*)$"
_DEF_OPERAND_TAIL = (
    rf"(?P<negated>n)?def{_NAME_END}{_PP_SPACE}*(?P<name>{_MACRO_NAME}{_NAME_END})?"
)

_IF_RE = re.compile(rf"{_DIRECTIVE}if{_CONDITION_TAIL}", re.MULTILINE)

# `#ifdef`/`#ifndef`, exposing the same `negated`/`name` groups as `_DEFINED_RES`
# so `_line_breakpoints` reads both spellings of the predicate through one code
# path. `name` is deliberately *optional*: an operand-less `#ifdef` is malformed,
# but cpp still opens a nesting level a later `#endif` closes, and dropping the
# event would unbalance every conditional after it — a nameless match simply
# resolves to opaque, exactly like a name this scan knows nothing about. `name`
# ends at `_NAME_END`, so a spliced operand reaches this pattern whole: `#ifdef
# W\` + newline + `IDE` tests `WIDE`, and capturing only its `W` would decide
# the guard from the wrong name entirely (PR #225 review).
_IFDEF_RE = re.compile(rf"{_DIRECTIVE}if{_DEF_OPERAND_TAIL}", re.MULTILINE)

# `defined NAME` / `defined(NAME)`, optionally negated — matched against an
# `#if`/`#elif`'s *entire* condition (`fullmatch`, for the same reason
# `_INT_LITERAL_RE` is one: a trailing operand must never slip through as a
# decided condition). Two patterns rather than one alternation because Python's
# `re` cannot reuse the `name` group name across alternatives; the parenthesized
# form is tried first so `defined (X)` is read as its operand rather than as a
# bare `(X)`. `_strip_literal_parens` runs before both, so `#if (defined(X))`
# arrives already peeled — while `#if !(defined(X))`, whose parens wrap only
# `!`'s operand, does not and correctly stays opaque.
_DEFINED_PREFIX = r"(?P<negated>!)?\s*defined"
_DEFINED_RES = (
    re.compile(rf"{_DEFINED_PREFIX}\s*\(\s*(?P<name>{_MACRO_NAME})\s*\)"),
    re.compile(rf"{_DEFINED_PREFIX}\s+(?P<name>{_MACRO_NAME})"),
)

# `#include`/`#include_next`/`#import`, tracked for one purpose: to make
# `_line_breakpoints` forget every definedness fact it had proven (see
# `_IFDEF_RE`). A header may `#define` or `#undef` any name, so nothing this file
# proved before the include still holds after it. `#import` counts because
# GCC/clang accept it in C too, and it reads a header exactly like `#include`
# does. The `#line NAME` *value* table (`_DEFINE_RE`) is deliberately left alone
# by the same asymmetry: forgetting a definedness fact only falls back to opaque
# — the assumed-live default that predates issue #157, which costs nothing —
# whereas forgetting a value binding would silently drop a real breakpoint this
# scan can otherwise resolve. `_DEFINE_RE`'s own comment names the residual that
# leaves.
_INCLUDE_RE = re.compile(
    rf"{_DIRECTIVE}(?:include(?:_next)?|import){_NAME_END}",
    re.MULTILINE,
)

# `#pragma push_macro("W")`/`#pragma pop_macro("W")` (clang/GCC), tracked for
# exactly the reason `_INCLUDE_RE` is: a `pop_macro` restores whatever the
# matching `push_macro` saved, which this scan does not model — so a top-level
# `#undef W` above it stops proving `W` undefined, and a `#define W` above it
# stops proving it defined. Without this the stale fact survives and a later
# `#ifdef W` is decided *dead* on a branch cpp really takes, deleting a real
# breakpoint (issue #157's asymmetry). Modelling the push/pop stack itself is
# not needed for that: dropping the fact falls back to opaque, which is the
# assumed-live default and costs nothing. Only `known_defined` is dropped, never
# `macros` — the same asymmetry `_INCLUDE_RE` spells out.
#
# The operand is deliberately not read. By the time `_line_breakpoints` scans,
# string literals are blanked, so `#pragma pop_macro("W")` reads as `#pragma
# pop_macro(   )` — a `name` group here would be `None` on every real input, a
# dead branch asserting a precision production does not have (PR #225 review).
# A pop therefore drops the whole `known_defined` table, which is where an
# unreadable operand landed anyway: clearing more facts only means more opaque
# conditionals, the assumed-live default.
#
# The two operations are told apart by the `op` group, because only one of them
# changes anything (PR #225 review): `push_macro` *saves* the current definition
# and leaves it in place, so every fact proven above it still holds below it,
# while `pop_macro` is the one that restores an unrecorded state. Dropping the
# fact on a push too would make `#define W` / `#pragma push_macro("W")` /
# `#ifndef W` opaque rather than dead, retaining a `#line` breakpoint in an arm
# cpp never takes.
_PRAGMA_MACRO_RE = re.compile(
    rf"{_DIRECTIVE}pragma{_PP_SPACE}+(?P<op>push|pop)_macro{_NAME_END}",
    re.MULTILINE,
)

# The `_Pragma` operator, which can spell the same restore as `#pragma
# pop_macro` without being a directive line — so `_PRAGMA_MACRO_RE` cannot see
# it (PR #225 review). Two things make it unlike every other pattern here.
#
# Its firing *position* is not knowable from text: the operator may sit in a
# macro's replacement list and fire at every later use of that macro, not where
# it is written. What is knowable is that its text always precedes every firing
# — a macro must be defined before it is used — so `_line_breakpoints` treats
# it as a latch: from the match onwards it drops every fact it holds and stops
# *establishing* new ones, leaving every later guard in the file opaque. That is
# the assumed-live default, which costs nothing, and it is the only stance that
# stays sound whether the operator fires here or a thousand lines below.
#
# Its *operand* is not knowable either, which is why this deliberately matches
# every `_Pragma`, not just a `pop_macro` one. `_line_breakpoints`' callers hand
# it text with string literals already blanked (`_stripped_for_scan`), so by the
# time this pattern runs `_Pragma("pop_macro(\"W\")")` reads as `_Pragma( )` —
# a narrower pattern matches nothing at all in production, which is exactly the
# silent no-op a first attempt at this shipped. Widening to the operator itself
# is sound because a `_Pragma` whose text we cannot read *might* be a
# `pop_macro`. The precision it costs is small and bounded: only definedness
# proof, only below the match, and only in files using the operator at all — 146
# of the 54,204 headers under `/usr/include`, and rarer still in the `.c` files
# this scan actually reads. The residual is a `pop_macro` reached without the
# `_Pragma` token appearing, e.g. assembled by token pasting.
_PRAGMA_OPERATOR_RE = re.compile(rf"(?<![\w$])_Pragma{_NAME_END}{_PP_SPACE}*\(")

# `#elif`'s condition is captured separately from `#if`'s: a literal `#elif 0`
# does not reactivate a dead branch the way `#else` does — its own condition is
# still false, so the branch it introduces stays dead regardless of what came
# before it (PR #156 follow-up to issue #145: treating every `#elif` as an
# unconditional `#else` let a still-dead `#elif 0` branch's `#line` count). A
# literal `#elif 1` is its mirror: it is live only if no earlier arm in the
# chain was already known-taken — otherwise cpp never reaches it regardless of
# its own condition being `1`. Like `_IF_RE`, it reads an already-spliced
# condition, so a split one still classifies correctly.
_ELIF_RE = re.compile(rf"{_DIRECTIVE}elif{_CONDITION_TAIL}", re.MULTILINE)

# C23's `#elifdef`/`#elifndef` — the `#elif` spelling of `_IFDEF_RE`'s predicate,
# with the same `negated`/`name` groups so it resolves through the same code
# path. `_ELIF_RE`'s `(?!\w)` keeps the two from both matching one directive.
# Tracked for the same reason `_IFDEF_RE`'s operand-less form is: an arm this
# scan never sees leaves the chain carrying the *previous* arm's state, and once
# `#ifdef` can prove that arm dead (issue #157) that silently deletes every real
# breakpoint in the unseen arm.
_ELIFDEF_RE = re.compile(rf"{_DIRECTIVE}elif{_DEF_OPERAND_TAIL}", re.MULTILINE)

# A bare `#else` has no condition of its own to read: it is taken whenever cpp
# reaches it, which happens whenever nothing before it was. This scan can
# prove that to be false — the else is dead — only once an earlier arm in the
# same chain was a literal `1`; absent that proof it must assume the else
# might be live (the same "assumed live" bias as an opaque `#if`), exactly as
# it must assume a still-unproven earlier arm might not be taken at all (PR
# #156 follow-up to issue #145: previously `#else` was unconditionally marked
# live even after a known-taken `#if 1`).
_ELSE_RE = re.compile(rf"{_DIRECTIVE}else{_NAME_END}", re.MULTILINE)
_ENDIF_RE = re.compile(rf"{_DIRECTIVE}endif{_NAME_END}", re.MULTILINE)

# A backslash immediately before a newline splices the following physical line
# onto this one (translation phase 2). `\r?` tolerates a CRLF source even
# though `list_units` itself reads via `Path.read_text()` (universal newlines),
# since `annotate_array_extents` is also callable directly on arbitrary text.
_CONTINUATION_RE = re.compile(r"\\\r?\n")


def _splice_continuations(source: str) -> tuple[str, list[int]]:
    """`source` with every backslash-continuation spliced out, plus a line map.

    Returns the spliced text and a list whose *i*-th entry is the 1-based
    physical line of `source` on which spliced line *i* (0-based) begins —
    enough to recover a physical line from any position in the spliced text,
    since splicing only ever merges a *contiguous* run of physical lines into
    one. Byte offsets are never mapped back: `_line_breakpoints` needs a
    position only to order events (which any monotonic mapping preserves) and
    to name a physical line (which this map answers directly).

    This runs first, before any directive pattern sees the text, because cpp
    decides what a logical line *is* in translation phase 2 — before phase 3
    ever looks for a leading ``#``. Doing it here rather than tolerating
    ``\\``-newline inside each pattern is what makes the whole family of
    splice hazards go away at once instead of one spelling at a time (PR #225
    review): a directive whose *keyword* is split (``#ifde\\`` + newline +
    ``f Q`` is an ordinary ``#ifdef Q`` to cpp) and one whose *operand* is
    split (``#ifdef W\\`` + newline + ``IDE`` tests ``WIDE``, not ``W``) both
    arrive whole, and the mirror case needs no filtering at all — a physical
    line that only looks like a directive because it was spliced onto the
    previous one (a multi-line ``#define``'s replacement text spilling a
    ``#line``-shaped token onto its own line) is no longer at the start of a
    line, so the ``^``-anchored patterns simply cannot match it (PR #156
    follow-up to issue #145, which used an explicit exclusion list instead).

    The ordering is load-bearing in one more way. `_DIRECTIVE` accepts C's
    ``%:`` digraph, and while `source` normally arrives with its string
    literals blanked too (`_stripped_for_scan`), a literal split across a
    continuation is *not* blanked — `_STRING_RE` stops at an unescaped newline
    by design. So ``char *s = "abc\\`` + newline + ``%:define W 1";`` would
    otherwise present a line-leading ``%:define`` and prove ``W`` defined from
    inside a string. Splicing first moves it mid-line, exactly as it is to cpp.

    Splices inside a ``//`` comment never reach here: `_COMMENT_RE` absorbs the
    backslash into the comment and `mask_comments` blanks it (see its comment).
    The reverse — a splice that *creates* a delimiter, ``/\\`` + newline +
    ``*`` — is real (verified against clang: the ``#define`` inside such a
    comment is not executed) and is why the caller re-masks the text this
    returns.
    """
    parts: list[str] = []
    physical_line_of: list[int] = [1]
    physical = 1
    last = 0
    for continuation in _CONTINUATION_RE.finditer(source):
        chunk = source[last : continuation.start()]
        parts.append(chunk)
        for _ in range(chunk.count("\n")):
            physical += 1
            physical_line_of.append(physical)
        # The spliced-out newline advances the physical line without starting a
        # new logical one — which is exactly the offset this map exists to hold.
        physical += 1
        last = continuation.end()
    tail = source[last:]
    parts.append(tail)
    for _ in range(tail.count("\n")):
        physical += 1
        physical_line_of.append(physical)
    return "".join(parts), physical_line_of


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


def _macro_verdict(match: re.Match[str] | None, known_defined: dict[str, bool]) -> str:
    """``"1"`` (live), ``"0"`` (dead), or ``"op"`` (opaque) for `match`'s macro test.

    `match` is a `_IFDEF_RE` or `_DEFINED_RES` match — both spell the predicate
    with the same ``negated``/``name`` groups. The verdict is ``"op"`` whenever
    there is no predicate to read at all: no `match`, or an operand-less,
    malformed ``#ifdef``. It is also ``"op"`` when the operand's definedness is
    not in `known_defined`; see `_IFDEF_RE` for why a name simply never
    ``#define``d in this file lands there rather than reading as undefined.

    The three return values are exactly `_RESOLVED_IFDEF_KINDS`' inner keys —
    that mapping, not this function, names the kind each verdict resolves to.
    """
    if match is None:
        return "op"
    name = match.group("name")
    if name is None:
        return "op"
    defined = known_defined.get(name)
    if defined is None:
        return "op"
    return "1" if defined != (match.group("negated") is not None) else "0"


# The kind an unresolved `#ifdef`-shaped event becomes once `_macro_verdict` has
# read it against the macro state at its own position. Written out rather than
# assembled from the pieces so every kind `_line_breakpoints`' walk dispatches on
# is greppable at both ends: a `#ifdef` that resolves live is the *same* `"if1"`
# a literal `#if 1` produces, and reduces into the same three-state vocabulary
# the walk already had — the resolution adds no branch to it.
_RESOLVED_IFDEF_KINDS = {
    "ifdef": {"1": "if1", "0": "if0", "op": "ifop"},
    "elifdef": {"1": "elif1", "0": "elif0", "op": "elifop"},
}


def _line_breakpoints(
    source_no_comments: str, predefined: Sequence[tuple[str, bool]] = ()
) -> list[tuple[int, int]]:
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
    when ``NAME``'s definedness at that point is *proven*: either by this file's
    own ``#define``/``#undef`` directives (issue #157) or by `predefined`, which
    seeds `known_defined` with what was true before the file's first line. The
    proof stays one-directional on purpose: a name neither `predefined` nor this
    file mentions stays *unknown*, never "undefined", because an ``#include``d
    header can still define it — and any ``#include`` cpp can reach drops every
    fact proven above it, the seed included. `_IFDEF_RE` carries the full
    rationale; the short version is that a wrong *live* verdict only reproduces
    the opaque default, while a wrong *dead* verdict deletes a real breakpoint.

    `predefined` is the only thing here that a scan of this file cannot derive
    for itself: a command-line ``-D`` and a compiler builtin are both invisible
    in the text (issue #226). It is measured, not guessed —
    `probe_predefined_guards` asks the same preprocessor, under the same build
    flags — so seeding it keeps the "proven, never assumed" rule above intact.
    Absent it (``()``, the default and what every caller without build flags
    passes) this decides exactly what #157 decided and no more.

    Every other branch whose own condition is not the complete literal ``0`` or
    ``1`` and not a decidable ``defined`` test stays opaque — its condition needs
    macro state this textual scan does not have, or (for ``#elif``/``#else``)
    needs knowing whether an earlier arm in its own chain was already
    known-taken — and is assumed live absent that proof, exactly as
    `_param_list_text` itself leaves picking the right branch to `def_line`,
    not to evaluating the condition.

    Every pattern runs over *spliced* text: `_splice_continuations` joins each
    backslash-continued physical line into the single logical line cpp reads in
    translation phase 2, before phase 3 ever looks for a leading ``#``. So a
    directive split anywhere — in its keyword (``#ifde\\`` + newline + ``f Q``),
    its operand (``#ifdef W\\`` + newline + ``IDE``, which tests ``WIDE``), its
    condition (``#if \\`` + newline + ``0``) or its digit sequence (``#line \\``
    + newline + ``11``) — is seen whole, and a physical line that only *looks*
    like a directive because it was spliced onto the one above (a multi-line
    ``#define``'s replacement text spilling a ``#line``-shaped token onto its
    own line) is mid-line by then and cannot match at all. A breakpoint is
    still recorded against the physical line the directive *starts* on, which
    is what clang — and so esbmc — counts from (gcc differs here).

    A ``#line`` naming a macro (``#line NAME``) resolves against a tiny
    ``#define``/``#undef``-tracking table this scan builds as it walks
    forward (issue #165 follow-up to #156/#157) — see `_DEFINE_RE`'s comment
    for exactly which bindings are trusted. A literal ``#if (0)``/``#elif
    (0)``/... — parens spanning the whole condition, however deeply nested —
    is normalized to the bare digit before the ``"0"``/``"1"`` comparison (see
    `_strip_literal_parens`), the same issue's second follow-up.
    """
    spliced, physical_line_of = _splice_continuations(source_no_comments)
    # Masked again, on the *spliced* text and with exactly the pass the callers
    # use. Two reasons (PR #225 review). Phase 2 can *create* a delimiter phase 3
    # then honours — `/\` + newline + `*`, and its string-literal twin — which
    # the caller's own pass, run before splicing as it must be, cannot have seen.
    # And running the *same* pass the callers do means this function sees one
    # text whether its caller pre-masked or not, so a pattern cannot pass its
    # tests on raw source while matching nothing in production — exactly how the
    # `_Pragma` latch first shipped as a no-op. That is not only a guard rail:
    # blanking literals is what stops a `_Pragma(` merely *mentioned* in a string
    # from latching, since `_PRAGMA_OPERATOR_RE` matches the operator rather than
    # its (already unreadable) argument. Blanking preserves offsets and newlines,
    # so `physical_line_of` still holds.
    spliced = _stripped_for_scan(spliced)

    def _events(
        pattern: re.Pattern[str], kind: str
    ) -> list[tuple[int, str, re.Match[str]]]:
        return [(m.start(), kind, m) for m in pattern.finditer(spliced)]

    def _cond_events(
        pattern: re.Pattern[str],
        kinds_by_literal: dict[str, str],
        opaque_kind: str,
        defined_kind: str,
    ) -> list[tuple[int, str, re.Match[str]]]:
        """`pattern`'s conditionals, classified by what their condition says.

        A literal `0`/`1` classifies here and for good, into `kinds_by_literal`.
        A ``defined``-shaped condition classifies as `defined_kind`, which is
        deliberately *not* yet a verdict: it stays unresolved until the walk
        reaches it, because its answer depends on macro state built along the
        way (`_RESOLVED_IFDEF_KINDS`). Anything else is `opaque_kind`.
        """
        found: list[tuple[int, str, re.Match[str]]] = []
        for m in pattern.finditer(spliced):
            condition = _strip_literal_parens(m.group(1))
            kind = kinds_by_literal.get(condition)
            if kind is not None:
                found.append((m.start(), kind, m))
                continue
            test = _macro_test(condition)
            if test is None:
                found.append((m.start(), opaque_kind, m))
            else:
                found.append((m.start(), defined_kind, test))
        return found

    events = sorted(
        _cond_events(_IF_RE, {"0": "if0", "1": "if1"}, "ifop", "ifdef")
        + _events(_IFDEF_RE, "ifdef")
        + _cond_events(_ELIF_RE, {"0": "elif0", "1": "elif1"}, "elifop", "elifdef")
        + _events(_ELIFDEF_RE, "elifdef")
        + _events(_ELSE_RE, "else")
        + _events(_ENDIF_RE, "endif")
        + _events(_INCLUDE_RE, "include")
        + _events(_PRAGMA_MACRO_RE, "pragma_macro")
        + _events(_PRAGMA_OPERATOR_RE, "pragma_operator")
        + _events(_LINE_DIRECTIVE_RE, "line")
        + _events(_LINE_DIRECTIVE_MACRO_RE, "line_macro")
        + _events(_DEFINE_RE, "define")
        + _events(_UNDEF_RE, "undef"),
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
    # absent unknown. Populated under the same fail-closed rule as `macros`:
    # only a `#define`/`#undef` outside every open conditional establishes an
    # entry, and one inside any conditional merely removes an existing one.
    # "Every" is literal — a branch this walk now proves taken (a literal
    # `#if 1`, or a resolved `#ifdef`) is no exception, because `stack` records
    # only whether an arm is known *dead*, not whether it is known *entered*.
    # Reading a `#define` there as certain would need per-arm certainty this
    # walk does not track, so it is conservatively treated as "may or may not
    # happen", exactly as #165 already did. `macro_stack_opaque` below suspends
    # establishment for the rest of the file on the same terms. See `_IFDEF_RE`
    # for why absence of a `#define` is never read as "undefined" (issue #157).
    # Seeded with what held before the file's first line (`predefined`, issue
    # #226); the file's own directives overwrite or drop those entries on the
    # exact same terms as any other, and an `#include` clears them too.
    known_defined: dict[str, bool] = dict(predefined)
    # Latched by the `_Pragma` operator, which can spell a `pop_macro` and fire
    # anywhere at or below its own text (`_PRAGMA_OPERATOR_RE`): once set, no
    # `#define`/`#undef` may *establish* a fact again, so every later guard in
    # the file falls back to the assumed-live opaque default.
    macro_stack_opaque = False
    breakpoints: list[tuple[int, int]] = []

    def _physical(pos: int) -> int:
        """The 1-based physical line of `source_no_comments` holding `pos`.

        `pos` indexes the *spliced* text, so the line it sits on there may span
        several physical lines — this names the one the logical line starts on
        (see `_splice_continuations`).
        """
        return physical_line_of[spliced.count("\n", 0, pos)]

    for pos, kind, match in events:
        dead = any(is_dead for is_dead, _ in stack)
        if kind in _RESOLVED_IFDEF_KINDS:
            # Resolved here, not when the event was built: a `#ifdef`'s verdict
            # is a function of the macro state at *its* position in the walk.
            kind = _RESOLVED_IFDEF_KINDS[kind][_macro_verdict(match, known_defined)]
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
        elif kind == "pragma_macro":
            # `pop_macro` restores a saved definition this scan never recorded,
            # so whatever was proven about the affected name stops holding here —
            # and which name that is cannot be read, so every fact goes.
            # `push_macro` saves the *current* definition without changing it,
            # so it invalidates nothing (`_PRAGMA_MACRO_RE`).
            if not dead and match.group("op") == "pop":
                known_defined.clear()
        elif kind == "pragma_operator":
            # Neither position-precise nor operand-precise, on purpose: the
            # operator can fire at every later use of the macro holding it, and
            # its string argument is already blanked by the time this scan runs,
            # so the only sound answer is to stop proving anything from here on
            # (`_PRAGMA_OPERATOR_RE`). Unlike every other event this one is
            # honoured even inside a branch proven dead — the text is what makes
            # the *later* firings possible, and a dead branch's macro body is
            # still a macro body cpp can expand somewhere live.
            known_defined.clear()
            macro_stack_opaque = True
        elif kind == "define":
            if not dead:
                name = match.group(1)
                if stack or macro_stack_opaque:
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
                    value_text = match.group(2).strip()
                    if _INT_LITERAL_RE.fullmatch(value_text):
                        macros[name] = int(value_text)
                    else:
                        macros.pop(name, None)
        elif kind == "undef":
            if not dead:
                name = match.group(1)
                macros.pop(name, None)
                if stack or macro_stack_opaque:
                    known_defined.pop(name, None)
                else:
                    known_defined[name] = False
        elif kind == "line_macro":
            if not dead:
                name = match.group(1)
                if name in macros:
                    breakpoints.append((_physical(pos), macros[name]))
        elif kind == "line" and not dead:
            breakpoints.append((_physical(pos), int(match.group(1))))
    return breakpoints


def _guard_macro_names(source_no_comments: str) -> tuple[str, ...]:
    """Every macro name a ``defined``-shaped conditional in `source_no_comments`
    tests, deduplicated.

    Order is stable but not the source's: the ``#ifdef``-spelled names come
    first, then the ``#if defined``-spelled ones, each group in its own textual
    order. Nothing reads it as a source order — `probe_predefined_guards` pairs
    each name with its probe declaration through one `enumerate`, so any stable
    order round-trips.

    Exactly the names `_line_breakpoints` would consult `known_defined` for:
    ``#ifdef``/``#ifndef`` and their ``#elifdef``/``#elifndef`` twins
    (`_IFDEF_RE`, `_ELIFDEF_RE`), plus the ``#if defined X``/``#elif
    !defined(X)`` spelling of the same predicate (`_macro_test`). A condition of
    any other shape contributes nothing — its answer needs macro *values*, which
    no probe of definedness could supply.

    Runs over spliced, comment- and literal-stripped text, the same two passes
    and in the same order `_line_breakpoints` runs them: a name reachable only
    through a backslash continuation (``#ifdef W\\`` + newline + ``IDE``) has to
    be probed under the name cpp actually tests, and a ``#ifdef`` mentioned
    inside a string literal must not be probed at all.
    """
    spliced, _ = _splice_continuations(source_no_comments)
    spliced = _stripped_for_scan(spliced)
    names: dict[str, None] = {}
    for pattern in (_IFDEF_RE, _ELIFDEF_RE):
        for match in pattern.finditer(spliced):
            name = match.group("name")
            if name is not None:
                names[name] = None
    for pattern in (_IF_RE, _ELIF_RE):
        for match in pattern.finditer(spliced):
            test = _macro_test(_strip_literal_parens(match.group(1)))
            if test is not None:
                names[test.group("name")] = None
    return tuple(names)


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

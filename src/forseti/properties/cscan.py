"""Lexical scan of C source: top-level items, doc comments, type/macro definitions.

Shared by the proposer recipes and the project index. The scan is lexical
(comments, literals and directives masked), not a parse; it reads the same text
ESBMC will, and degrades by omission rather than guessing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from forseti.esbmc import find_definition_brace, mask_comments
from forseti.esbmc.preprocessor import mask_string_literals

_COMMENT_OR_LITERAL_RE = re.compile(
    r'"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'|(?P<comment>//(?:\\\n|[^\n])*|/\*.*?\*/)',
    re.DOTALL,
)
_DIRECTIVE_RE = re.compile(r"^[ \t]*#(?:\\\n|[^\n])*", re.MULTILINE)
_DEFINE_RE = re.compile(r"^[ \t]*#[ \t]*define[ \t]+(\w+)(?:\\\n|[^\n])*", re.MULTILINE)
IDENT_RE = re.compile(r"[A-Za-z_]\w*")
_TAG_RE = re.compile(r"\b(?:struct|union|enum)\s+(\w+)\s*\{")
_FN_PTR_NAME_RE = re.compile(r"\(\s*\*\s*(\w+)\s*\)")
_TYPE_HEADS = frozenset({"typedef", "struct", "union", "enum"})
_ATTRIBUTE_RE = re.compile(r"__attribute__\s*\(\(.*?\)\)\s*$", re.DOTALL)
_FOLLOWING_WORD_RE = re.compile(r"\s*([A-Za-z_]\w*)")
_CONTINUATION_WORDS = frozenset(
    {"__attribute__", "__attribute", "__asm__", "__asm", "asm"}
)


@dataclass(frozen=True)
class Item:
    start: int
    end: int
    brace: int | None
    is_function: bool


@dataclass(frozen=True)
class Definition:
    start: int
    end: int
    names: frozenset[str]
    uses: frozenset[str]
    text: str


def blank(match: re.Match[str]) -> str:
    return re.sub(r"[^\n]", " ", match.group(0))


def code_view(no_comments: str) -> str:
    """Comment-masked source with literals and directives blanked, offsets kept."""
    return _DIRECTIVE_RE.sub(blank, mask_string_literals(no_comments))


def _is_function_head(head: str) -> bool:
    return _ATTRIBUTE_RE.sub("", head.rstrip()).rstrip().endswith(")")


def _starts_new_item(code: str, start: int, end: int) -> bool:
    """True when an identifier follows a top-level `)` that cannot continue it.

    Only a semicolon-less macro invocation (``DECLARE_THING(x)``) is followed by
    another declaration's first word; ``;``, ``{``, ``,``, ``=``, ``(`` and the
    attribute/asm keywords all continue the item instead.
    """
    follower = _FOLLOWING_WORD_RE.match(code, end)
    return (
        follower is not None
        and follower.group(1) not in _CONTINUATION_WORDS
        and not code.startswith("__attribute__", start)
    )


def _top_level_items(code: str) -> list[Item]:
    """Top-level declarations/definitions of `code` as offset spans."""
    items: list[Item] = []
    depth = 0
    start: int | None = None
    brace: int | None = None
    initialised = False
    for i, ch in enumerate(code):
        if start is None:
            if ch.isspace():
                continue
            start, brace, initialised = i, None, False
        if ch in "([{":
            if ch == "{" and depth == 0 and brace is None:
                brace = i
            depth += 1
        elif ch in ")]}":
            depth = max(depth - 1, 0)
            if depth or initialised:
                continue
            if ch == "}" and brace is not None and _is_function_head(code[start:brace]):
                items.append(Item(start, i + 1, brace, True))
                start = None
            elif ch == ")" and _starts_new_item(code, start, i + 1):
                items.append(Item(start, i + 1, None, False))
                start = None
        elif ch == "=" and depth == 0:
            initialised = True
        elif ch == ";" and depth == 0:
            items.append(Item(start, i + 1, brace, False))
            start = None
    return items


def _enumerators(text: str) -> set[str]:
    names: set[str] = set()
    for match in re.finditer(r"\benum\b[^{;]*\{", text):
        depth, i = 1, match.end()
        part = ""
        while i < len(text) and depth:
            ch = text[i]
            depth += ch in "([{"
            depth -= ch in ")]}"
            if (depth == 1 and ch == ",") or depth == 0:
                found = IDENT_RE.search(part)
                if found:
                    names.add(found.group())
                part = ""
            else:
                part += ch
            i += 1
    return names


def _declarator_names(tail: str) -> set[str]:
    """The name each comma-separated declarator in `tail` declares."""
    parts: list[str] = []
    depth, part = 0, ""
    for ch in tail:
        depth += ch in "(["
        depth -= ch in ")]"
        if ch == "," and depth == 0:
            parts.append(part)
            part = ""
        else:
            part += ch
    parts.append(part)
    names: set[str] = set()
    for declarator in parts:
        pointer = _FN_PTR_NAME_RE.search(declarator)
        if pointer:
            names.add(pointer.group(1))
            continue
        flat = re.sub(r"\([^()]*\)|\[[^\]]*\]", " ", declarator)
        idents = IDENT_RE.findall(flat)
        if idents:
            names.add(idents[-1])
    return names


def _type_names(text: str) -> frozenset[str]:
    names = set(_TAG_RE.findall(text)) | _enumerators(text)
    if text.startswith("typedef"):
        tail = text[text.rfind("}") + 1 :] if "}" in text else text[len("typedef") :]
        names |= _declarator_names(tail.rstrip("; \t\n"))
    return frozenset(names)


def _comment_spans(source: str) -> list[tuple[int, int]]:
    return [
        match.span()
        for match in _COMMENT_OR_LITERAL_RE.finditer(source)
        if match.group("comment") is not None
    ]


class Scan:
    """One lexical pass over a source file, shared by the recipes."""

    def __init__(self, source: str) -> None:
        self.source = source
        self.no_comments = mask_comments(source)
        self.code = code_view(self.no_comments)
        self.items = _top_level_items(self.code)
        self.comments = _comment_spans(source)

    def function(self, symbol: str) -> tuple[Item, int] | None:
        """The definition of `symbol` and the offset of its body's ``{``."""
        brace = find_definition_brace(self.source, symbol)
        if brace is None:
            return None
        for item in self.items:
            if item.is_function and item.brace == brace:
                return item, brace
        return None

    def doc_start(self, item_start: int) -> int:
        """Offset where the comment block directly above `item_start` begins.

        A comment is a doc comment only when no blank line separates it from the
        item and nothing else shares its first line; a run of ``//`` lines is one
        block. Anything else (a file header, a trailing comment on the previous
        statement) is not.
        """
        start = item_start
        for first, last in reversed([c for c in self.comments if c[1] <= item_start]):
            gap = self.source[last:start]
            line_start = self.source.rfind("\n", 0, first) + 1
            if (
                gap.strip()
                or gap.count("\n") > 1
                or self.source[line_start:first].strip()
                or (
                    start != item_start
                    and not (
                        self.source.startswith("//", first)
                        and self.source.startswith("//", start)
                    )
                )
            ):
                break
            start = first
        return start

    def definitions_used(self, seed: str) -> list[Definition]:
        """Type/macro definitions `seed` (code text) needs, transitively."""
        defs = self.definitions()
        used = set(IDENT_RE.findall(seed))
        selected: set[int] = set()
        changed = True
        while changed:
            changed = False
            for index, definition in enumerate(defs):
                if index not in selected and definition.names & used:
                    selected.add(index)
                    used |= definition.uses
                    changed = True
        return [defs[index] for index in sorted(selected)]

    def definitions(self) -> list[Definition]:
        """Type and macro definitions of the file, in source order."""
        defs: list[Definition] = []
        for item in self.items:
            text = self.code[item.start : item.end]
            head = IDENT_RE.match(text)
            if item.is_function or head is None or head.group() not in _TYPE_HEADS:
                continue
            names = _type_names(text)
            if names:
                defs.append(
                    Definition(
                        item.start,
                        item.end,
                        names,
                        frozenset(IDENT_RE.findall(text)) - names,
                        self.source[item.start : item.end],
                    )
                )
        for match in _DEFINE_RE.finditer(self.no_comments):
            text = match.group().strip()
            name = match.group(1)
            defs.append(
                Definition(
                    match.start(),
                    match.end(),
                    frozenset({name}),
                    frozenset(IDENT_RE.findall(text)) - {name, "define"},
                    text,
                )
            )
        return sorted(defs, key=lambda d: d.start)

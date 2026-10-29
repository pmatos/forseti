"""Proposer input recipes: named, versioned, deterministic context builders.

A *recipe* maps ``(unit id, source text, project root)`` to the text the proposer
prompt renders in place of the whole unit file (RFC-0007, "Inputs: a context pack,
not the whole file"). Recipes are the experiment's input factor, so each is pure
and deterministic: the same arguments always yield the same context, and the
recipe's ``ref`` (``name/vN``) is recorded on every property it helped propose
(`Provenance.recipe`). A shipped version's behaviour is immutable; change a recipe
by registering a new version.

The v1 recipes are single-file (they read only the source text; the project root
is part of the seam for the project-index recipes of RFC-0007 T1):

- ``baseline``: today's whole file, minus any ``main`` and the leading file-header
  comment (both leak the answer on ``examples/``).
- ``sighted``: the target function with its doc comment, plus the type and macro
  definitions it uses.
- ``blind``: ``sighted`` without the target body: its signature, doc comment, and
  only the definitions the *signature* uses, so nothing in the pack can be
  transcribed from the implementation.

The scan is lexical (comments, literals and directives masked), not a parse; it
reads the same text ESBMC will, and degrades by omission rather than guessing.
Only type and macro definitions are pulled in: objects the body reads (tables,
globals), callees, and a struct reachable only through a variable declaration are
out of scope for v1.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from forseti.esbmc import find_definition_brace, mask_comments
from forseti.esbmc.preprocessor import mask_string_literals
from forseti.unit_id import unit_id_symbol


class RecipeError(ValueError):
    """A recipe could not build its context (unknown name, or no such function)."""


@dataclass(frozen=True)
class ProposalContext:
    """The text a recipe built, and the recipe ``ref`` that built it."""

    recipe: str
    text: str


@dataclass(frozen=True)
class Recipe:
    name: str
    version: str
    build: Callable[[str, str, Path], str]

    @property
    def ref(self) -> str:
        """The provenance label, e.g. ``"blind/v1"``."""
        return f"{self.name}/v{self.version}"


_COMMENT_OR_LITERAL_RE = re.compile(
    r'"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'|(?P<comment>//(?:\\\n|[^\n])*|/\*.*?\*/)',
    re.DOTALL,
)
_DIRECTIVE_RE = re.compile(r"^[ \t]*#(?:\\\n|[^\n])*", re.MULTILINE)
_DEFINE_RE = re.compile(r"^[ \t]*#[ \t]*define[ \t]+(\w+)(?:\\\n|[^\n])*", re.MULTILINE)
_IDENT_RE = re.compile(r"[A-Za-z_]\w*")
_TAG_RE = re.compile(r"\b(?:struct|union|enum)\s+(\w+)\s*\{")
_FN_PTR_NAME_RE = re.compile(r"\(\s*\*\s*(\w+)\s*\)")
_TYPE_HEADS = frozenset({"typedef", "struct", "union", "enum"})
_ATTRIBUTE_RE = re.compile(r"__attribute__\s*\(\(.*?\)\)\s*$", re.DOTALL)
_FOLLOWING_WORD_RE = re.compile(r"\s*([A-Za-z_]\w*)")
_CONTINUATION_WORDS = frozenset(
    {"__attribute__", "__attribute", "__asm__", "__asm", "asm"}
)


@dataclass(frozen=True)
class _Item:
    start: int
    end: int
    brace: int | None
    is_function: bool


@dataclass(frozen=True)
class _Definition:
    start: int
    end: int
    names: frozenset[str]
    uses: frozenset[str]
    text: str


def _blank(match: re.Match[str]) -> str:
    return re.sub(r"[^\n]", " ", match.group(0))


def _code_view(no_comments: str) -> str:
    """Comment-masked source with literals and directives blanked, offsets kept."""
    return _DIRECTIVE_RE.sub(_blank, mask_string_literals(no_comments))


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


def _top_level_items(code: str) -> list[_Item]:
    """Top-level declarations/definitions of `code` as offset spans."""
    items: list[_Item] = []
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
                items.append(_Item(start, i + 1, brace, True))
                start = None
            elif ch == ")" and _starts_new_item(code, start, i + 1):
                items.append(_Item(start, i + 1, None, False))
                start = None
        elif ch == "=" and depth == 0:
            initialised = True
        elif ch == ";" and depth == 0:
            items.append(_Item(start, i + 1, brace, False))
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
                found = _IDENT_RE.search(part)
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
        idents = _IDENT_RE.findall(flat)
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


class _Scan:
    """One lexical pass over a source file, shared by the recipes."""

    def __init__(self, source: str) -> None:
        self.source = source
        self.no_comments = mask_comments(source)
        self.code = _code_view(self.no_comments)
        self.items = _top_level_items(self.code)
        self.comments = _comment_spans(source)

    def function(self, symbol: str) -> tuple[_Item, int] | None:
        """The definition of `symbol` and the offset of its body's ``{``."""
        brace = find_definition_brace(self.source, symbol)
        if brace is None:
            return None
        for item in self.items:
            if item.is_function and item.brace == brace:
                return item, brace
        return None

    def target(self, symbol: str) -> tuple[_Item, int]:
        found = self.function(symbol)
        if found is None:
            raise RecipeError(f"no definition of {symbol!r} found")
        return found

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

    def definitions_used(self, seed: str) -> list[_Definition]:
        """Type/macro definitions `seed` (code text) needs, transitively."""
        defs = self._definitions()
        used = set(_IDENT_RE.findall(seed))
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

    def _definitions(self) -> list[_Definition]:
        """Type and macro definitions of the file, in source order."""
        defs: list[_Definition] = []
        for item in self.items:
            text = self.code[item.start : item.end]
            head = _IDENT_RE.match(text)
            if item.is_function or head is None or head.group() not in _TYPE_HEADS:
                continue
            names = _type_names(text)
            if names:
                defs.append(
                    _Definition(
                        item.start,
                        item.end,
                        names,
                        frozenset(_IDENT_RE.findall(text)) - names,
                        self.source[item.start : item.end],
                    )
                )
        for match in _DEFINE_RE.finditer(self.no_comments):
            text = match.group().strip()
            name = match.group(1)
            defs.append(
                _Definition(
                    match.start(),
                    match.end(),
                    frozenset({name}),
                    frozenset(_IDENT_RE.findall(text)) - {name, "define"},
                    text,
                )
            )
        return sorted(defs, key=lambda d: d.start)


def _pack(definitions: list[_Definition], target: str) -> str:
    sections = ["\n".join(d.text for d in definitions), target.strip("\n")]
    return "\n\n".join(s for s in sections if s) + "\n"


def _sighted(unit_id: str, source: str, project_root: Path) -> str:
    del project_root
    scan = _Scan(source)
    item, _ = scan.target(unit_id_symbol(unit_id))
    definitions = scan.definitions_used(scan.code[item.start : item.end])
    return _pack(definitions, source[scan.doc_start(item.start) : item.end])


def _blind(unit_id: str, source: str, project_root: Path) -> str:
    del project_root
    scan = _Scan(source)
    item, brace = scan.target(unit_id_symbol(unit_id))
    definitions = scan.definitions_used(scan.code[item.start : brace])
    signature = source[scan.doc_start(item.start) : brace].rstrip() + ";"
    return _pack(definitions, signature)


def _baseline(unit_id: str, source: str, project_root: Path) -> str:
    del project_root
    symbol = unit_id_symbol(unit_id)
    scan = _Scan(source)
    cuts: list[tuple[int, int]] = []

    main = scan.function("main") if symbol != "main" else None
    if main is not None:
        cuts.append((scan.doc_start(main[0].start), main[0].end))

    target = scan.function(symbol)
    keep_from = scan.doc_start(target[0].start) if target else len(source)
    header_end = 0
    for first, last in scan.comments:
        if source[header_end:first].strip() or first >= keep_from:
            break
        header_end = last
    if header_end:
        cuts.append((0, header_end))

    out, pos = [], 0
    for start, end in sorted(cuts):
        out.append(source[pos:start])
        pos = max(pos, end)
    out.append(source[pos:])
    return "".join(out).strip("\n") + "\n"


RECIPES: dict[str, Recipe] = {
    recipe.name: recipe
    for recipe in (
        Recipe("baseline", "1", _baseline),
        Recipe("sighted", "1", _sighted),
        Recipe("blind", "1", _blind),
    )
}


def build_context(
    recipe: str, *, unit_id: str, source_text: str, project_root: Path
) -> ProposalContext:
    """Run the registered recipe `recipe`; `RecipeError` if it is not registered."""
    try:
        selected = RECIPES[recipe]
    except KeyError:
        raise RecipeError(
            f"unknown recipe {recipe!r}; choose one of {', '.join(RECIPES)}"
        ) from None
    return ProposalContext(
        recipe=selected.ref,
        text=selected.build(unit_id, source_text, project_root),
    )

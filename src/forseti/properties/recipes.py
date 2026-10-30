"""Proposer input recipes: named, versioned, deterministic context builders.

A *recipe* maps ``(unit id, source text, project root)`` to the text the proposer
prompt renders in place of the whole unit file (RFC-0007, "Inputs: a context pack,
not the whole file"). Recipes are the experiment's input factor, so each is pure
and deterministic: the same arguments always yield the same context, and the
recipe's ``ref`` (``name/vN``) is recorded on every property it helped propose
(`Provenance.recipe`). A shipped version's behaviour is immutable; change a recipe
by registering a new version.

The first three recipes are single-file (they read only the source text):

- ``baseline``: today's whole file, minus any ``main`` and the leading file-header
  comment (both leak the answer on ``examples/``).
- ``sighted``: the target function with its doc comment, plus the type and macro
  definitions it uses.
- ``blind``: ``sighted`` without the target body: its signature, doc comment, and
  only the definitions the *signature* uses, so nothing in the pack can be
  transcribed from the implementation.

The project recipes build a context pack (`forseti.properties.pack`) from the git
repository holding the unit's file, as of its HEAD commit, with the unit's file taken
from the source text. They are `callers` (target, types, call sites), `tests` (target,
types, tests that call it) and `full` (every slice). They need a git repository with a
commit, and fail with `RecipeError` otherwise. The ref does not record which commit
HEAD was, so a run that must be reproduced pins the checkout (the replay runner's
job). The callees' accepted contracts are not shown: they would make a ref depend on
the store's state as well.

The scan is lexical (comments, literals and directives masked), not a parse; it
reads the same text ESBMC will, and degrades by omission rather than guessing.
Only type and macro definitions are pulled in: objects the body reads (tables,
globals), callees, and a struct reachable only through a variable declaration are
out of scope for v1.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from forseti.unit_id import unit_id_symbol

from .cscan import Definition, Item, Scan
from .index import ProjectIndexError
from .pack import ALL_SLICES, PackConfig, SliceKind, build_project_pack


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


def _target(scan: Scan, symbol: str) -> tuple[Item, int]:
    found = scan.function(symbol)
    if found is None:
        raise RecipeError(f"no definition of {symbol!r} found")
    return found


def _pack(definitions: list[Definition], target: str) -> str:
    sections = ["\n".join(d.text for d in definitions), target.strip("\n")]
    return "\n\n".join(s for s in sections if s) + "\n"


def _sighted(unit_id: str, source: str, project_root: Path) -> str:
    del project_root
    scan = Scan(source)
    item, _ = _target(scan, unit_id_symbol(unit_id))
    definitions = scan.definitions_used(scan.code[item.start : item.end])
    return _pack(definitions, source[scan.doc_start(item.start) : item.end])


def _blind(unit_id: str, source: str, project_root: Path) -> str:
    del project_root
    scan = Scan(source)
    item, brace = _target(scan, unit_id_symbol(unit_id))
    definitions = scan.definitions_used(scan.code[item.start : brace])
    signature = source[scan.doc_start(item.start) : brace].rstrip() + ";"
    return _pack(definitions, signature)


def _baseline(unit_id: str, source: str, project_root: Path) -> str:
    del project_root
    symbol = unit_id_symbol(unit_id)
    scan = Scan(source)
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


def _project_recipe(name: str, slices: set[SliceKind]) -> Recipe:
    config = PackConfig(slices=frozenset(slices))

    def build(unit_id: str, source: str, project_root: Path) -> str:
        del project_root
        try:
            return build_project_pack(unit_id, source, config).text
        except ProjectIndexError as exc:
            raise RecipeError(f"recipe {name!r}: {exc}") from exc

    return Recipe(name, "1", build)


_SIGHTED = {SliceKind.TARGET, SliceKind.TYPES}

SINGLE_FILE_RECIPES = ("baseline", "sighted", "blind")

RECIPES: dict[str, Recipe] = {
    recipe.name: recipe
    for recipe in (
        Recipe("baseline", "1", _baseline),
        Recipe("sighted", "1", _sighted),
        Recipe("blind", "1", _blind),
        _project_recipe("callers", _SIGHTED | {SliceKind.CALLERS}),
        _project_recipe("tests", _SIGHTED | {SliceKind.TESTS}),
        _project_recipe("full", set(ALL_SLICES)),
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

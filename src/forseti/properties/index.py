"""Project index: where functions, calls, types and tests live, at one commit.

The input side of RFC-0007 T1. A `ProjectIndex` answers the structural questions the
context-pack builder (`forseti.properties.pack`) asks about a C project: which
functions are defined or declared where, who calls whom, which type and macro
definitions a piece of code needs, and which files are tests.

Temporal hygiene (RFC-0007 ground rule 1) is structural here: the index never touches
the working directory. Every byte comes from a `SourceTree`, and `GitTree` pins one
commit, so an uncommitted edit or a later commit cannot reach the proposer.

The scan is the lexical one the recipes use (`forseti.properties.cscan`): comments,
literals and directives masked, no preprocessing, no parse. A name is a call when it is
followed by ``(`` in a function body and not reached through ``.`` or ``->``;
macro-generated functions and calls are invisible, and a name defined in two files
resolves to the unit's own file first, then the first path in sorted order. The index
degrades by omission, never by guessing.

Records are plain frozen dataclasses of paths, names and offsets so the shape can be
shared with the project-scale unit slicing of RFC-0008.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from .cscan import IDENT_RE, Definition, Item, Scan

C_SUFFIXES = (".c", ".h")

_ATTRIBUTE_RE = re.compile(r"__attribute__\s*\(\(.*?\)\)", re.DOTALL)
_TRAILING_IDENT_RE = re.compile(r"(\w+)\s*$")
_CALL_RE = re.compile(r"(?<![\w.])(?<!->)(\w+)\s*\(")
_NOT_CALLS = frozenset(
    {
        "if",
        "while",
        "for",
        "switch",
        "return",
        "sizeof",
        "case",
        "else",
        "do",
        "typeof",
        "__typeof__",
        "_Alignof",
        "alignof",
        "_Static_assert",
        "__attribute__",
        "defined",
    }
)
_STATIC_RE = re.compile(r"\bstatic\b")
_TEST_DIRS = frozenset({"test", "tests", "unittest", "unittests", "kunit"})
_TEST_STEM_RE = re.compile(r"^(?:test|kunit)[_-]|[_-](?:tests?|kunit)$")


class ProjectIndexError(ValueError):
    """The index or pack could not be built (bad revision, unknown unit)."""


class Reach(StrEnum):
    """How far from the unit's own file callers, callees and types are sought.

    RFC-0007 open question 1: how far beyond the translation unit these reach is
    to be answered by measurement, so it is a parameter and not a constant.
    """

    FILE = "file"
    PROJECT = "project"


class SourceTree(Protocol):
    """A read-only snapshot of a project's files: sorted relative POSIX paths."""

    def paths(self) -> Sequence[str]: ...

    def read(self, path: str) -> str: ...


@dataclass(frozen=True)
class MappingTree:
    """An in-memory tree, for tests and for callers that already hold the text."""

    files: Mapping[str, str]

    def paths(self) -> Sequence[str]:
        return sorted(self.files)

    def read(self, path: str) -> str:
        return self.files[path]


def _git(root: Path, *args: str) -> bytes:
    try:
        done = subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, check=True
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", b"") or b""
        raise ProjectIndexError(
            f"git {' '.join(args)} failed in {root}: {detail.decode().strip() or exc}"
        ) from exc
    return done.stdout


@dataclass(frozen=True)
class GitTree:
    """The files of one commit, read from the object database."""

    root: Path
    commit: str

    @classmethod
    def at(cls, root: Path, revision: str = "HEAD") -> GitTree:
        """Pin `revision` (any commit-ish) to its full SHA, once."""
        sha = _git(root, "rev-parse", "--verify", f"{revision}^{{commit}}")
        return cls(root, sha.decode().strip())

    def paths(self) -> Sequence[str]:
        out = _git(self.root, "ls-tree", "-r", "-z", "--name-only", self.commit)
        return sorted(p for p in out.decode(errors="replace").split("\0") if p)

    def read(self, path: str) -> str:
        blob = _git(self.root, "cat-file", "blob", f"{self.commit}:{path}")
        return blob.decode(errors="replace")


@dataclass(frozen=True)
class FunctionRef:
    """A function definition or prototype: its name and where it sits in a file."""

    path: str
    name: str
    start: int
    end: int
    brace: int | None

    @property
    def is_definition(self) -> bool:
        return self.brace is not None


@dataclass(frozen=True)
class CallSite:
    """One call of a name, inside the definition `caller` of file `path`."""

    path: str
    caller: str
    line: int


def is_test_path(path: str) -> bool:
    """Whether `path` looks like a test file: a ``test(s)``/``kunit`` directory, or
    a ``test_*``, ``*_test(s)`` or ``*_kunit`` stem."""
    *directories, filename = path.split("/")
    stem = filename.rsplit(".", 1)[0]
    return any(d in _TEST_DIRS for d in directories) or bool(_TEST_STEM_RE.search(stem))


def _trailing_call_name(head: str) -> str | None:
    """The function name in `head` (``<decl-specifiers> name(params)``), or None.

    The name before the *last* top-level parenthesised group, so a leading macro
    such as ``__printf(1, 2) int log(...)`` does not capture the name.
    """
    text = _ATTRIBUTE_RE.sub(" ", head).rstrip()
    if not text.endswith(")"):
        return None
    depth = 0
    for i in range(len(text) - 1, -1, -1):
        depth += (text[i] == ")") - (text[i] == "(")
        if depth <= 0:
            break
    found = _TRAILING_IDENT_RE.search(text[:i])
    return found.group(1) if found else None


def line_of(source: str, offset: int) -> int:
    """The 1-based line holding `offset`."""
    return source.count("\n", 0, offset) + 1


class ProjectIndex:
    """Queries over the C files of a `SourceTree`, with per-file results cached."""

    def __init__(
        self, tree: SourceTree, *, suffixes: Sequence[str] = C_SUFFIXES
    ) -> None:
        self.tree = tree
        self.files = tuple(p for p in tree.paths() if p.endswith(tuple(suffixes)))
        self._sources: dict[str, str] = {}
        self._scans: dict[str, Scan] = {}
        self._refs: dict[str, tuple[FunctionRef, ...]] = {}
        self._calls: dict[tuple[str, str], tuple[CallSite, ...]] = {}

    def source(self, path: str) -> str:
        if path not in self._sources:
            if path not in self.files:
                raise ProjectIndexError(f"{path!r} is not a source file of the tree")
            self._sources[path] = self.tree.read(path)
        return self._sources[path]

    def scan(self, path: str) -> Scan:
        if path not in self._scans:
            self._scans[path] = Scan(self.source(path))
        return self._scans[path]

    def files_in_reach(self, origin: str, reach: Reach) -> tuple[str, ...]:
        """`origin` first, then (for `Reach.PROJECT`) every other file by path."""
        if reach is Reach.FILE:
            return (origin,)
        return (origin, *(f for f in self.files if f != origin))

    def functions(self, path: str) -> tuple[FunctionRef, ...]:
        """Definitions and prototypes of `path`, in source order."""
        if path not in self._refs:
            scan = self.scan(path)
            refs = []
            for item in scan.items:
                ref = self._function_ref(path, scan, item)
                if ref is not None:
                    refs.append(ref)
            self._refs[path] = tuple(refs)
        return self._refs[path]

    @staticmethod
    def _function_ref(path: str, scan: Scan, item: Item) -> FunctionRef | None:
        text = scan.code[item.start : item.end]
        if item.is_function:
            name = _trailing_call_name(scan.code[item.start : item.brace])
            return _ref(path, name, item, item.brace) if name else None
        head = IDENT_RE.match(text)
        if head is None or head.group() == "typedef" or not text.endswith(";"):
            return None
        declarator = text[:-1]
        if "=" in declarator:
            return None
        name = _trailing_call_name(declarator)
        return _ref(path, name, item, None) if name else None

    def definition(self, path: str, name: str) -> FunctionRef | None:
        """The definition of `name` in `path`, or None (also for an unknown path)."""
        if path not in self.files:
            return None
        return next(
            (r for r in self.functions(path) if r.name == name and r.is_definition),
            None,
        )

    def is_file_local(self, ref: FunctionRef) -> bool:
        """A ``static`` function of a ``.c`` file: no other file can call it."""
        head = self.scan(ref.path).code[ref.start : ref.brace]
        return ref.path.endswith(".c") and _STATIC_RE.search(head) is not None

    def find(self, name: str, origin: str, reach: Reach) -> FunctionRef | None:
        """The definition of `name` (else a prototype), the unit's own file first."""
        found: FunctionRef | None = None
        for path in self.files_in_reach(origin, reach):
            for ref in self.functions(path):
                if ref.name != name:
                    continue
                if ref.is_definition:
                    return ref
                found = found or ref
        return found

    def call_sites(self, path: str, name: str) -> tuple[CallSite, ...]:
        """Calls of `name` inside the function definitions of `path`."""
        key = (path, name)
        if key not in self._calls:
            source = self.source(path)
            sites: list[CallSite] = []
            if name in source:
                scan = self.scan(path)
                call = re.compile(rf"(?<![\w.])(?<!->){re.escape(name)}\s*\(")
                for ref in self.functions(path):
                    if ref.brace is None:
                        continue
                    sites.extend(
                        CallSite(path, ref.name, line_of(source, match.start()))
                        for match in call.finditer(scan.code, ref.brace, ref.end)
                    )
            self._calls[key] = tuple(sites)
        return self._calls[key]

    def callees(self, ref: FunctionRef) -> tuple[str, ...]:
        """Names called in `ref`'s body, first call first, without repeats."""
        if ref.brace is None:
            return ()
        body = self.scan(ref.path).code
        names = dict.fromkeys(
            m.group(1)
            for m in _CALL_RE.finditer(body, ref.brace, ref.end)
            if m.group(1) not in _NOT_CALLS and m.group(1) != ref.name
        )
        return tuple(names)

    def definitions_used(
        self, seed: str, origin: str, reach: Reach, depth: int
    ) -> list[tuple[str, Definition]]:
        """Type and macro definitions `seed` names, to `depth` levels of nesting.

        Level one is what `seed` mentions directly; each further level adds what
        those definitions mention. A name defined in several files resolves to the
        first in `files_in_reach` order. Result order: other files by path, then the
        unit's own file, each in source order.
        """
        paths = self.files_in_reach(origin, reach)
        by_name: dict[str, tuple[str, Definition]] = {}
        for path in paths:
            for definition in self.scan(path).definitions():
                for name in definition.names:
                    by_name.setdefault(name, (path, definition))
        taken: dict[tuple[str, int], tuple[str, Definition]] = {}
        frontier = set(IDENT_RE.findall(seed))
        for _ in range(depth):
            fresh: dict[tuple[str, int], tuple[str, Definition]] = {}
            for name in frontier:
                hit = by_name.get(name)
                if hit is not None and (hit[0], hit[1].start) not in taken:
                    fresh[hit[0], hit[1].start] = hit
            if not fresh:
                break
            taken.update(fresh)
            frontier = {use for _, d in fresh.values() for use in d.uses}
        rank = {path: i for i, path in enumerate(paths[1:] + paths[:1])}
        return sorted(taken.values(), key=lambda pd: (rank[pd[0]], pd[1].start))


def _ref(path: str, name: str, item: Item, brace: int | None) -> FunctionRef:
    return FunctionRef(path, name, item.start, item.end, brace)

"""Every `ADR-NNNN` cited in the tree must resolve to a record in `docs/adr/`.

#103: `ADR-0009` (D1-D4) was cited 22 times across `properties/`,
`orchestrator/` and `core/propose.py` while `docs/adr/` stopped at 0008 -- the
most-cited technical decision in the codebase had no text a change could be
checked against. ADRs are where the "why" is supposed to survive (ADR-0005), so
a citation with no record is a broken reference, not a typo.

These lock the invariant end to end: a cited number has a record, that number
names exactly one record, every record is reachable from the index, and a
record's title agrees with its filename.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ADR_DIR = REPO_ROOT / "docs" / "adr"

_CITATION = re.compile(r"ADR-(\d+)")
_RECORD_NAME = re.compile(r"^(\d{4})-[a-z0-9-]+\.md$")
_INDEX_LINK = re.compile(r"\[\d{4}\]\((\d{4}-[a-z0-9-]+\.md)\)")

# VCS internals, tool caches, and the gitignored per-project store: machine
# state, never a source of truth for a citation.
_SKIPPED_DIRS = frozenset(
    {
        ".git",
        ".forseti",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "venv",
        "htmlcov",
        "node_modules",
    }
)
# semantic-release regenerates CHANGELOG.md from commit subjects; a citation
# there is frozen history no contributor can fix without rewriting it.
_SKIPPED_FILES = frozenset({"CHANGELOG.md"})


def _normalize(number: str) -> str:
    """`9` and `0009` name the same record; compare on the canonical form."""
    return f"{int(number):04d}"


def _sources() -> Iterator[tuple[Path, str]]:
    """Every readable text file in the tree, as (repo-relative path, contents)."""
    for path in sorted(REPO_ROOT.rglob("*")):
        if not path.is_file() or path.name in _SKIPPED_FILES:
            continue
        relative = path.relative_to(REPO_ROOT)
        if any(
            part in _SKIPPED_DIRS or part.endswith(".egg-info")
            for part in relative.parts
        ):
            continue
        try:
            yield relative, path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue


def _records(directory: Path = ADR_DIR) -> dict[str, list[Path]]:
    """Accepted-or-otherwise ADR files, grouped by their four-digit number.

    Grouped rather than keyed to one path: two files claiming `0010` is the
    collision `test_no_two_records_share_a_number` exists to catch, and
    keeping only the last one would leave the index and heading checks
    validating the survivor while the other record went unseen.
    """
    found: dict[str, list[Path]] = {}
    for path in sorted(directory.glob("*.md")):
        match = _RECORD_NAME.match(path.name)
        if match is not None:
            found.setdefault(match.group(1), []).append(path)
    return found


def _duplicates(records: dict[str, list[Path]]) -> dict[str, list[str]]:
    """Numbers claimed by more than one record, as {number: [filenames]}."""
    return {
        number: sorted(path.name for path in paths)
        for number, paths in records.items()
        if len(paths) > 1
    }


def test_every_cited_adr_has_a_record() -> None:
    cited: dict[str, list[str]] = {}
    for relative, text in _sources():
        for match in _CITATION.finditer(text):
            cited.setdefault(_normalize(match.group(1)), []).append(str(relative))

    # Guards the sweep itself: an over-broad skip list would make this vacuous.
    assert cited, "no ADR citations found -- the file sweep is not reaching the tree"

    records = _records()
    dangling = {
        number: sorted(set(where))
        for number, where in cited.items()
        if number not in records
    }
    assert not dangling, f"ADR citations with no docs/adr/ record: {dangling}"


def test_no_two_records_share_a_number() -> None:
    """One number, one record -- otherwise a citation names two decisions."""
    records = _records()
    # Same guard as the sweep above: an empty docs/adr/ would pass vacuously.
    assert records, "no ADR records found -- docs/adr/ is not being read"
    duplicates = _duplicates(records)
    assert not duplicates, f"ADR numbers claimed by more than one record: {duplicates}"


def test_duplicate_numbers_are_reported_not_overwritten(tmp_path: Path) -> None:
    """The collision above is detectable, not collapsed to the last file read."""
    # Grouping keys off the filename, so the bodies are stubs -- and stubs that
    # deliberately spell no citation: this file is itself swept above, where a
    # literal `ADR-`-plus-digits would read as a reference to a missing record.
    for name in ("0010-first.md", "0010-second.md", "0011-alone.md"):
        (tmp_path / name).write_text(f"# stub {name}\n", encoding="utf-8")

    records = _records(tmp_path)
    assert [path.name for path in records["0010"]] == [
        "0010-first.md",
        "0010-second.md",
    ]
    assert sorted(records) == ["0010", "0011"]
    assert _duplicates(records) == {"0010": ["0010-first.md", "0010-second.md"]}


def test_every_record_is_listed_in_the_index() -> None:
    index = (ADR_DIR / "README.md").read_text(encoding="utf-8")
    linked = {match.group(1) for match in _INDEX_LINK.finditer(index)}
    assert {path.name for paths in _records().values() for path in paths} == linked


def test_every_record_titles_its_own_number() -> None:
    for number, paths in sorted(_records().items()):
        for path in paths:
            heading = path.read_text(encoding="utf-8").splitlines()[0]
            assert heading.startswith(f"# ADR-{number} — "), (
                f"{path.name} opens with {heading!r}, not its own number"
            )

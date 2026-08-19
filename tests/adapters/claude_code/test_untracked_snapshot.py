"""Tests for the unified untracked-snapshot leftover predicate.

The gate stages two kinds of private snapshot beside a source — the enumeration
snapshot (``_ENUM_SNAPSHOT_PREFIX``) and the verify snapshot
(``_VERIFY_SNAPSHOT_PREFIX``). A snapshot a killed hook could not clean up must
never be handed back to ``verify_and_record`` as a source in its own right. One
predicate (``_untracked_snapshot``) now identifies a *provably untracked*
leftover of *either* kind, so the scope funnel (``_in_scope_c_abspath``) asks a
single question instead of knowing two snapshot kinds exist.

These test the predicate directly, per prefix, so the collapse of the two former
per-prefix predicates cannot quietly drop one kind's coverage.

Run from the repo root with the dev venv::

    .venv/bin/python -m pytest tests/adapters/claude_code/test_untracked_snapshot.py -q
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from forseti.adapters.claude_code import forseti_gate as gate

_PREFIXES = [gate._ENUM_SNAPSHOT_PREFIX, gate._VERIFY_SNAPSHOT_PREFIX]
_PREFIX_IDS = ["enum", "verify"]


def _git_init(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)


def _git_commit_all(path: Path) -> None:
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "baseline"], check=True)


@pytest.mark.parametrize("prefix", _PREFIXES, ids=_PREFIX_IDS)
def test_recognizes_an_untracked_leftover_of_either_snapshot_kind(
    tmp_path: Path, prefix: str
) -> None:
    # A leftover of either snapshot kind that git cannot show as tracked is a
    # killed-hook remnant, exempt from every scan so it is never offered back as a
    # source. Before the two per-prefix predicates were unified, this held for the
    # enumeration prefix only.
    _git_init(tmp_path)
    rel = f"{prefix}xyz.c"
    (tmp_path / rel).write_text("int f(void) { return 0; }\n")
    assert gate._untracked_snapshot(str(tmp_path), rel) is True


@pytest.mark.parametrize("prefix", _PREFIXES, ids=_PREFIX_IDS)
def test_does_not_exempt_a_tracked_file_sharing_a_prefix(
    tmp_path: Path, prefix: str
) -> None:
    # A repository may legitimately track a source whose name shares a snapshot
    # prefix; exempting it by name alone would silently drop a real, changed file
    # (a Bash edit invisible to the direct Write/Edit hook) from every scan. Only a
    # provably untracked match is exempt.
    _git_init(tmp_path)
    rel = f"{prefix}core.c"
    (tmp_path / rel).write_text("int f(void) { return 0; }\n")
    _git_commit_all(tmp_path)
    assert gate._untracked_snapshot(str(tmp_path), rel) is False


@pytest.mark.parametrize("prefix", _PREFIXES, ids=_PREFIX_IDS)
def test_fails_closed_when_trackedness_cannot_be_determined(
    tmp_path: Path, prefix: str
) -> None:
    # `_git` reads as `None` for "git absent / timed out / not a work tree", not
    # just for "untracked". Treating that as "provably untracked" would fail *open*
    # in the one predicate whose job is to stop a silent bypass, so an unanswerable
    # query must never exempt — here `tmp_path` is deliberately never `git init`ed.
    rel = f"{prefix}xyz.c"
    (tmp_path / rel).write_text("int f(void) { return 0; }\n")
    assert gate._untracked_snapshot(str(tmp_path), rel) is False


@pytest.mark.parametrize("prefix", _PREFIXES, ids=_PREFIX_IDS)
def test_literal_pathspec_keeps_a_bracket_named_tracked_file_gated(
    tmp_path: Path, prefix: str
) -> None:
    # ``:(literal)`` stops `rel` being read as pathspec glob magic: a tracked file
    # literally named ``<prefix>[a].c`` would otherwise match as a character class
    # (matching ``<prefix>a.c``, absent here) rather than itself, so `ls-files`
    # would report nothing and the file would read as untracked and be exempted.
    _git_init(tmp_path)
    rel = f"{prefix}[a].c"
    (tmp_path / rel).write_text("int f(void) { return 0; }\n")
    _git_commit_all(tmp_path)
    assert gate._untracked_snapshot(str(tmp_path), rel) is False


def test_a_name_without_a_snapshot_prefix_is_never_a_snapshot(tmp_path: Path) -> None:
    # The `startswith` gate short-circuits before any git query, so a plain source
    # is never mistaken for a leftover regardless of trackedness.
    _git_init(tmp_path)
    rel = "core.c"
    (tmp_path / rel).write_text("int f(void) { return 0; }\n")
    assert gate._untracked_snapshot(str(tmp_path), rel) is False


def test_the_shared_forseti_stem_alone_is_not_a_snapshot_prefix(
    tmp_path: Path,
) -> None:
    # The two prefixes share a ``.forseti-`` stem but neither is a prefix of the
    # other: a name carrying only the stem (`.forseti-other-`) must not be taken
    # for a snapshot leftover, guarding against a future collapse to the stem.
    _git_init(tmp_path)
    rel = ".forseti-other-xyz.c"
    (tmp_path / rel).write_text("int f(void) { return 0; }\n")
    assert gate._untracked_snapshot(str(tmp_path), rel) is False

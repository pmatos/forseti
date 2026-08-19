"""Behaviour spec for the extracted git-porcelain module.

The verify-gate's git plumbing — running git in a project's work tree and
parsing its ``--porcelain -z`` output — lives behind its own small interface in
``forseti.adapters.claude_code.git`` so its invariants (rename-record skipping,
staged-vs-untracked classification, byte-exact blob hashing, and the probe
environment that answers "is there a work tree here at all") are a nameable,
directly testable unit rather than logic buried in the 2.5k-line gate module.

The git-backed tests build a throwaway repo under ``tmp_path``; the porcelain
parsers and the probe-env helper are pure and need no repo.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from forseti.adapters.claude_code import git


def _git_init(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)


def _git_commit_all(path: Path) -> None:
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "wip"], check=True)


# --- porcelain -z parsing (pure) --------------------------------------------


def test_iter_porcelain_z_skips_rename_and_copy_sources() -> None:
    # A rename ("R") or copy ("C") record is followed by a second NUL-separated
    # field holding the *source* path; only the current (first) path is a change
    # we verify, so the source token must be skipped.
    out = "R  new.c\x00old.c\x00C  dup.c\x00orig.c\x00 M mod.c\x00"
    assert list(git._iter_porcelain_z(out)) == [
        ("R ", "new.c"),
        ("C ", "dup.c"),
        (" M", "mod.c"),
    ]


def test_parse_porcelain_z_returns_current_paths() -> None:
    out = "R  new.c\x00old.c\x00 M mod.c\x00?? fresh.c\x00A  added.c\x00"
    assert git._parse_porcelain_z(out) == ["new.c", "mod.c", "fresh.c", "added.c"]


def test_staged_paths_from_porcelain_selects_indexed_only() -> None:
    # X-status of M/A/R/C/D means the index differs from HEAD (staged); a clean
    # X (space) is a worktree-only edit and "?" is untracked — neither is staged.
    out = "M  a.c\x00 M b.c\x00A  c.c\x00?? d.c\x00R  e.c\x00old_e.c\x00"
    assert git._staged_paths_from_porcelain(out) == ["a.c", "c.c", "e.c"]


# --- probe environment (pure) -----------------------------------------------


def test_git_probe_env_strips_ceiling_and_forces_c_locale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", "/some/ceiling")
    monkeypatch.setenv("FORSETI_MARKER", "kept")
    env = git._git_probe_env()
    assert "GIT_CEILING_DIRECTORIES" not in env
    assert env["LC_ALL"] == "C"
    assert env["FORSETI_MARKER"] == "kept"  # unrelated vars pass through


# --- running git against a work tree ----------------------------------------


def test_git_returns_none_outside_a_work_tree(tmp_path: Path) -> None:
    # Not a repo: `_git` swallows the non-zero exit and reports "unavailable".
    assert git._git(str(tmp_path), "rev-parse", "HEAD") is None


def test_git_and_git_bytes_report_none_when_git_cannot_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Best-effort contract: a git that cannot even be spawned is "unavailable"
    # (None), never an exception the caller must catch.
    def _boom(*args: object, **kwargs: object) -> None:
        raise OSError("git not launchable")

    monkeypatch.setattr(subprocess, "run", _boom)
    assert git._git(".", "rev-parse", "HEAD") is None
    assert git._git_bytes(".", "cat-file", "blob", "HEAD:x") is None


def test_git_head_returns_sha_and_none_without_commits(tmp_path: Path) -> None:
    _git_init(tmp_path)
    assert git.git_head(str(tmp_path)) is None  # a repo, but zero commits
    (tmp_path / "a.c").write_text("int a(void){return 0;}\n")
    _git_commit_all(tmp_path)
    head = git.git_head(str(tmp_path))
    assert head is not None
    assert len(head) == 40  # a full SHA-1, stripped of the trailing newline


def test_git_committed_files_since_diffs_the_baseline(tmp_path: Path) -> None:
    _git_init(tmp_path)
    (tmp_path / "a.c").write_text("int a(void){return 0;}\n")
    _git_commit_all(tmp_path)
    base = git.git_head(str(tmp_path))
    assert git.git_committed_files_since(str(tmp_path), base) == []  # HEAD unmoved

    (tmp_path / "b.c").write_text("int b(void){return 0;}\n")
    _git_commit_all(tmp_path)
    assert git.git_committed_files_since(str(tmp_path), base) == ["b.c"]
    # best-effort: a None baseline disables the scan, an unknown one degrades to []
    assert git.git_committed_files_since(str(tmp_path), None) == []
    assert git.git_committed_files_since(str(tmp_path), "0" * 40) == []


def test_git_blob_hash_index_head_and_missing(tmp_path: Path) -> None:
    _git_init(tmp_path)
    a_bytes = b"int f(void){return 0;}\n"
    (tmp_path / "foo.c").write_bytes(a_bytes)
    _git_commit_all(tmp_path)  # HEAD:foo.c holds A
    b_bytes = b"int f(void){return 1;}\n"
    (tmp_path / "foo.c").write_bytes(b_bytes)
    subprocess.run(["git", "-C", str(tmp_path), "add", "foo.c"], check=True)  # index B

    # Independent source of truth: sha256 of the exact bytes, byte-for-byte.
    assert (
        git.git_blob_hash(str(tmp_path), ":foo.c")
        == hashlib.sha256(b_bytes).hexdigest()
    )
    assert (
        git.git_blob_hash(str(tmp_path), "HEAD:foo.c")
        == hashlib.sha256(a_bytes).hexdigest()
    )
    assert git.git_blob_hash(str(tmp_path), "HEAD:nope.c") is None  # no such blob


def test_git_changed_files_none_outside_repo_then_lists_untracked(
    tmp_path: Path,
) -> None:
    assert git.git_changed_files(str(tmp_path)) is None  # not a work tree
    _git_init(tmp_path)
    (tmp_path / "new.c").write_text("int n(void){return 0;}\n")
    assert git.git_changed_files(str(tmp_path)) == ["new.c"]  # untracked, --uall


def test_staged_source_paths_lists_indexed_changes(tmp_path: Path) -> None:
    assert git.staged_source_paths(str(tmp_path)) == []  # not a work tree → []
    _git_init(tmp_path)
    assert git.staged_source_paths(str(tmp_path)) == []  # nothing staged
    (tmp_path / "s.c").write_text("int s(void){return 0;}\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "s.c"], check=True)
    assert git.staged_source_paths(str(tmp_path)) == ["s.c"]

"""Tests for out-of-band gating of Bash-written C files (issue #99).

Run from the repo root with the dev venv::

    .venv/bin/python -m pytest tests/adapters/claude_code -q

Discovery is `git status`-scoped, so the git-backed tests build a throwaway repo
under ``tmp_path``. The ESBMC-gated end-to-end tests skip without `esbmc` +
`forseti` on PATH; everything else runs pure-Python (pointer units are
NEEDS_CONTRACT and never shell out to ESBMC).
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

import pytest

from forseti.adapters.claude_code import (
    event_log,
    post_bash,
    session_start,
    stop_gate,
)
from forseti.adapters.claude_code import forseti_gate as gate


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate from any FORSETI_GATE_*/CLAUDE_PROJECT_DIR set in the outer env."""
    for var in ("FORSETI_GATE_INCLUDE", "FORSETI_GATE_EXCLUDE", "CLAUDE_PROJECT_DIR"):
        monkeypatch.delenv(var, raising=False)


def _git_init(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)


def _git_commit_all(path: Path) -> None:
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "baseline"], check=True)


def _run(
    hook_main: Callable[[], int],
    project_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    **payload: object,
) -> int:
    """Drive a hook's ``main()`` with a stdin payload pointing at `project_dir`."""
    body = {"cwd": str(project_dir), **payload}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(body)))
    return hook_main()


# --- glob config + porcelain parsing (pure) --------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        ("", ()),
        (None, ()),
        ("a:b, c", ("a", "b", "c")),
        (" vendor , *_gen.c ", ("vendor", "*_gen.c")),
    ],
)
def test_globs_split(value: str | None, expected: tuple[str, ...]) -> None:
    assert gate._globs(value) == expected


def test_matches_segment_and_glob() -> None:
    assert gate._matches("libs/vendor/x.c", ("vendor",))  # bare name = any segment
    assert gate._matches("gen/a_generated.c", ("*_generated.c",))  # path glob
    assert gate._matches("test/x.c", ("test/*",))
    assert not gate._matches("src/core.c", ("vendor", "*_generated.c"))


def test_included_defaults_and_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    assert gate._included("src/core.c")
    assert not gate._included("third_party/x.c")  # default exclude
    assert not gate._included("vendor/x.c")
    # include-list restricts; exclude wins over include
    monkeypatch.setenv("FORSETI_GATE_INCLUDE", "kernels/*")
    assert gate._included("kernels/a.c")
    assert not gate._included("src/other.c")
    # setting exclude replaces the defaults, so 'vendor' is no longer excluded
    monkeypatch.delenv("FORSETI_GATE_INCLUDE")
    monkeypatch.setenv("FORSETI_GATE_EXCLUDE", "generated")
    assert gate._included("vendor/x.c")
    assert not gate._included("generated/x.c")


def test_in_scope_c_abspath_rejects_an_untracked_enum_snapshot(tmp_path: Path) -> None:
    # The shared funnel `discover_changed_c_sources`/`divergent_blob_sources`/
    # `baseline_blob_hashes` all go through: a leftover snapshot a killed hook
    # could not clean up must never come back as a source in its own right.
    _git_init(tmp_path)
    rel = f"{gate._ENUM_SNAPSHOT_PREFIX}xyz.c"
    (tmp_path / rel).write_text("int f(void) { return 0; }\n")
    root = str(tmp_path)
    proj_real = os.path.realpath(str(tmp_path))
    assert gate._in_scope_c_abspath(str(tmp_path), root, proj_real, rel) is None


def test_untracked_snapshot_exemption_survives_an_include_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The exemption for an untracked snapshot has to come before `_included`'s
    # globs, or a project's own FORSETI_GATE_EXCLUDE (which *replaces* the
    # defaults, never extends them) plus a narrowing FORSETI_GATE_INCLUDE could
    # un-exclude it.
    _git_init(tmp_path)
    rel = f"{gate._ENUM_SNAPSHOT_PREFIX}xyz.c"
    (tmp_path / rel).write_text("int f(void) { return 0; }\n")
    monkeypatch.setenv("FORSETI_GATE_EXCLUDE", "nothing_matches_this")
    monkeypatch.setenv("FORSETI_GATE_INCLUDE", rel)
    root = str(tmp_path)
    proj_real = os.path.realpath(str(tmp_path))
    assert gate._in_scope_c_abspath(str(tmp_path), root, proj_real, rel) is None


def test_in_scope_c_abspath_still_gates_a_tracked_file_sharing_the_prefix(
    tmp_path: Path,
) -> None:
    # The residual the untracked-only exemption narrows: excluding by basename
    # prefix alone also dropped a *tracked* file that happens to share it from
    # every scan, so a Bash edit to it (invisible to the direct Write/Edit hook)
    # could ship unverified. Only a provably untracked match is exempt now.
    _git_init(tmp_path)
    rel = f"{gate._ENUM_SNAPSHOT_PREFIX}core.c"
    (tmp_path / rel).write_text("int f(void) { return 0; }\n")
    _git_commit_all(tmp_path)
    root = str(tmp_path)
    proj_real = os.path.realpath(str(tmp_path))
    assert gate._in_scope_c_abspath(str(tmp_path), root, proj_real, rel) == str(
        tmp_path / rel
    )


def test_in_scope_c_abspath_gates_when_trackedness_cannot_be_determined(
    tmp_path: Path,
) -> None:
    # `_git` reads as `None` for "git absent/timed out/not a work tree", not just
    # for "untracked" — collapsing those would fail *open* in the one predicate
    # whose job is to stop a silent bypass, so an unanswerable query must never
    # exempt a file, only gate it like anything else.
    rel = f"{gate._ENUM_SNAPSHOT_PREFIX}xyz.c"
    (tmp_path / rel).write_text("int f(void) { return 0; }\n")
    root = str(tmp_path)  # deliberately never `git init`ed
    proj_real = os.path.realpath(str(tmp_path))
    assert gate._in_scope_c_abspath(str(tmp_path), root, proj_real, rel) == str(
        tmp_path / rel
    )


def test_discover_changed_c_sources_gates_a_bash_edit_to_a_tracked_snapshot_lookalike(
    tmp_path: Path,
) -> None:
    # The reviewer's exact scenario end to end: a repo already tracks a
    # legitimate source named like the snapshot prefix; an out-of-band (Bash)
    # edit to it must still be discovered, not silently dropped by name alone.
    _git_init(tmp_path)
    rel = f"{gate._ENUM_SNAPSHOT_PREFIX}core.c"
    (tmp_path / rel).write_text("int f(void) { return 0; }\n")
    _git_commit_all(tmp_path)
    (tmp_path / rel).write_text("int f(void) { return 1; }\n")  # the Bash edit

    found = gate.discover_changed_c_sources(str(tmp_path))

    assert found == [str(tmp_path / rel)]


def test_parse_porcelain_z_handles_renames() -> None:
    # rename record: "R  new\0old"; the old-path token must be skipped
    out = "R  new.c\x00old.c\x00 M mod.c\x00?? fresh.c\x00A  added.c\x00"
    assert gate._parse_porcelain_z(out) == ["new.c", "mod.c", "fresh.c", "added.c"]


def test_content_hash_missing_file_is_none(tmp_path: Path) -> None:
    assert gate.content_hash(tmp_path / "nope.c") is None


# --- unit_id identity key (issue #152) --------------------------------------


def test_unit_id_resolves_dotdot_through_a_symlink(tmp_path: Path) -> None:
    # `link -> external/pkg`: the kernel walk `unit_id` now does resolves
    # `link/..` to `external`, the link *target*'s parent — not to `proj`, which
    # is what a lexical `os.path.relpath` collapses it to. Colliding on `proj`
    # would alias this key onto a real `proj/x.c`, even though the kernel (and so
    # the gate's read/hash/verify) opens a different file entirely.
    proj = tmp_path / "proj"
    external = tmp_path / "external" / "pkg"
    external.mkdir(parents=True)
    proj.mkdir()
    (proj / "x.c").write_text("own\n")
    (tmp_path / "external" / "x.c").write_text("ext\n")
    (proj / "link").symlink_to(external)

    aliased = gate.unit_id(str(proj), str(proj / "link" / ".." / "x.c"))
    plain = gate.unit_id(str(proj), str(proj / "x.c"))

    assert aliased == "../external/x.c"
    assert aliased != plain  # the bug: both used to key as "x.c"
    assert plain == "x.c"  # no `..` in the path: unchanged, no migration needed


def test_stale_sources_does_not_collapse_aliased_real_files(tmp_path: Path) -> None:
    # "Why it matters" in issue #152: `proj/x.c` and `link/../x.c` (through a
    # symlinked component) are two REAL files. Seed `scanned` as if `proj/x.c`
    # had just been verified, then check the aliased path — which really is
    # `external/x.c`, holding the *same* bytes by construction — is still
    # reported stale rather than silently read as already-fresh off the other
    # file's stamp (the fail-open half of the bug: a collision would let unverified
    # content slip past the gate).
    proj = tmp_path / "proj"
    external = tmp_path / "external" / "pkg"
    external.mkdir(parents=True)
    proj.mkdir()
    content = "int f(void){return 0;}\n"
    (proj / "x.c").write_text(content)
    (tmp_path / "external" / "x.c").write_text(content)
    (proj / "link").symlink_to(external)
    aliased = str(proj / "link" / ".." / "x.c")

    state = gate.load_state(str(proj))
    state["scanned"][gate.unit_id(str(proj), str(proj / "x.c"))] = gate.content_hash(
        str(proj / "x.c")
    )

    assert gate.stale_sources(str(proj), state, [aliased]) == [aliased]


# --- git discovery ----------------------------------------------------------


def test_discover_changed_c_sources(tmp_path: Path) -> None:
    _git_init(tmp_path)
    (tmp_path / "kept.c").write_text("int kept(void){return 0;}\n")
    _git_commit_all(tmp_path)  # kept.c is now committed + clean

    (tmp_path / "new.c").write_text("int n(void){return 0;}\n")  # untracked
    (tmp_path / "kept.c").write_text("int kept(void){return 1;}\n")  # modified
    (tmp_path / "vendor").mkdir()
    (tmp_path / "vendor" / "v.c").write_text("int v(void){return 0;}\n")  # excluded
    (tmp_path / "notes.txt").write_text("x")  # not a C source

    found = gate.discover_changed_c_sources(str(tmp_path))
    assert found is not None
    assert sorted(os.path.basename(f) for f in found) == ["kept.c", "new.c"]


def test_discover_non_git_returns_none(tmp_path: Path) -> None:
    (tmp_path / "a.c").write_text("int a(void){return 0;}\n")
    assert gate.discover_changed_c_sources(str(tmp_path)) is None


def test_discover_resolves_repo_root_and_scopes_to_project_dir(tmp_path: Path) -> None:
    # project_dir is a SUBDIR of the repo: git reports repo-root-relative paths,
    # so the join must be against the root, and changes outside the subdir are
    # out of scope.
    _git_init(tmp_path)
    (tmp_path / "top.c").write_text("int t(void){return 0;}\n")  # repo root, untracked
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "inner.c").write_text("int i(void){return 0;}\n")  # under project_dir

    found = gate.discover_changed_c_sources(str(sub))
    assert found is not None
    assert [os.path.basename(f) for f in found] == ["inner.c"]  # top.c is out of scope
    assert os.path.isfile(found[0])  # path resolved correctly against the repo root


def test_discover_respells_through_a_symlinked_project_root(tmp_path: Path) -> None:
    # issue #161: `project_dir` reached through a symlink (`link -> real`). Confirm
    # the actual mechanism first — `git rev-parse --show-toplevel` must report the
    # resolved root, not `link` itself, or this divergence has a different cause
    # than the one this fix targets.
    real = tmp_path / "real"
    real.mkdir()
    _git_init(real)
    (real / "sub").mkdir()
    (real / "sub" / "x.c").write_text("int f(void){return 0;}\n")
    _git_commit_all(real)
    (real / "sub" / "x.c").write_text("int f(void){return 1;}\n")  # dirty
    link = tmp_path / "link"
    link.symlink_to(real)
    resolved_link = os.path.realpath(str(link))
    assert resolved_link != str(link)  # sanity: link is genuinely a symlink

    toplevel = gate._git(str(link), "rev-parse", "--show-toplevel")
    assert toplevel is not None
    assert toplevel.strip() == resolved_link  # precondition this fix relies on

    # A direct PostToolUse edit spells the file through `link`, never through the
    # resolved root; that is the key `unit_id` must agree with.
    direct_path = str(link / "sub" / "x.c")
    found = gate.discover_changed_c_sources(str(link))
    assert found is not None
    assert found == [direct_path]  # not the resolved-root spelling
    assert gate.unit_id(str(link), found[0]) == gate.unit_id(str(link), direct_path)
    assert gate.unit_id(str(link), found[0]) == os.path.join("sub", "x.c")


def test_discover_preserves_a_symlinked_source_alias(tmp_path: Path) -> None:
    # review feedback, issue #161: an untracked `alias.c -> target.c`, both inside
    # the project. Resolving the whole path (rather than just its directory) would
    # silently substitute `target.c` for the Git-reported `alias.c`, so a
    # Bash-created alias could be skipped whenever `target.c`'s hash was already
    # `scanned` even though parsing through the alias can use a different lexical
    # include directory.
    _git_init(tmp_path)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "target.c").write_text("int f(void){return 0;}\n")
    _git_commit_all(tmp_path)
    (tmp_path / "sub" / "alias.c").symlink_to("target.c")  # untracked, in-project

    found = gate.discover_changed_c_sources(str(tmp_path))
    assert found is not None

    alias_path = str(tmp_path / "sub" / "alias.c")
    target_path = str(tmp_path / "sub" / "target.c")
    assert found == [alias_path]  # the Git-reported path, not the resolved target
    assert gate.unit_id(str(tmp_path), alias_path) == os.path.join("sub", "alias.c")
    assert gate.unit_id(str(tmp_path), alias_path) != gate.unit_id(
        str(tmp_path), target_path
    )

    # The fail-open half of the bug: had discovery returned `target_path` instead
    # (the old behavior), its hash already matches a `scanned` target.c, so
    # `stale_sources` would read the alias as already-verified off the target's
    # stamp and silently drop it rather than gate it.
    state = gate.load_state(str(tmp_path))
    state["scanned"][gate.unit_id(str(tmp_path), target_path)] = gate.content_hash(
        target_path
    )
    assert gate.stale_sources(str(tmp_path), state, found) == found


def test_in_scope_c_abspath_rejects_a_leaf_symlink_whose_target_leaves_the_project(
    tmp_path: Path,
) -> None:
    # review feedback on PR #171 (issue #161): the directory-only scope check
    # (`real_dir`) passes an untracked `alias.c -> ../outside.c` straight through,
    # since its *parent* is in-project — only the leaf's own target escapes. Left
    # unchecked, discovery would hand back the alias and the gate would hash and
    # verify whatever lives outside the project under the alias's key. Called
    # directly so the assertion isn't laundered through `discover_changed_c_sources`'s
    # `os.path.isfile` existence filter, which would drop a *dangling* symlink for
    # an unrelated reason and never exercise this check at all.
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.c").write_text("int leak(void){return 0;}\n")
    proj = tmp_path / "proj"
    proj.mkdir()
    _git_init(proj)
    (proj / "sub").mkdir()
    (proj / "sub" / "alias.c").symlink_to(outside / "secret.c")  # untracked, escapes

    root = str(proj)
    proj_real = os.path.realpath(str(proj))
    rel = os.path.join("sub", "alias.c")
    assert gate._in_scope_c_abspath(str(proj), root, proj_real, rel) is None


def test_discover_rejects_a_leaf_symlink_whose_target_leaves_the_project(
    tmp_path: Path,
) -> None:
    # Same scenario end to end through `discover_changed_c_sources`, alongside a
    # legitimately dirty in-project source — so the assertion pins "the escaping
    # alias is excluded" rather than "everything happens to come back empty".
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.c").write_text("int leak(void){return 0;}\n")
    proj = tmp_path / "proj"
    proj.mkdir()
    _git_init(proj)
    (proj / "sub").mkdir()
    (proj / "sub" / "x.c").write_text("int f(void){return 0;}\n")
    _git_commit_all(proj)
    (proj / "sub" / "x.c").write_text("int f(void){return 1;}\n")  # legit, dirty
    (proj / "sub" / "alias.c").symlink_to(outside / "secret.c")  # untracked, escapes

    found = gate.discover_changed_c_sources(str(proj))

    assert found == [str(proj / "sub" / "x.c")]


def test_discover_preserves_directory_spelling_when_a_symlink_replaces_it(
    tmp_path: Path,
) -> None:
    # review feedback on PR #171 (issue #161): Bash replaces a tracked directory
    # `a/` with an in-project symlink `a -> b`. Git reports the former `a/x.c` as
    # changed; resolving its *directory* (rather than just translating the
    # project root's own boundary) would silently rewrite that to `b/x.c`, and if
    # `b/x.c` already has a matching `scanned` digest the swap reads as
    # already-verified and is dropped — even though `a/x.c` held different
    # content before the swap and the new alias has its own include context.
    _git_init(tmp_path)
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "x.c").write_text("int fa(void){return 0;}\n")
    (tmp_path / "b" / "x.c").write_text("int fb(void){return 1;}\n")
    _git_commit_all(tmp_path)

    state = gate.load_state(str(tmp_path))
    state["scanned"][os.path.join("a", "x.c")] = gate.content_hash(
        str(tmp_path / "a" / "x.c")
    )
    state["scanned"][os.path.join("b", "x.c")] = gate.content_hash(
        str(tmp_path / "b" / "x.c")
    )

    shutil.rmtree(tmp_path / "a")
    (tmp_path / "a").symlink_to("b")  # the Bash swap: directory -> symlink

    found = gate.discover_changed_c_sources(str(tmp_path))
    assert found is not None

    a_path = str(tmp_path / "a" / "x.c")
    b_path = str(tmp_path / "b" / "x.c")
    assert found == [a_path]  # the Git-reported directory spelling, not `b/x.c`
    assert gate.unit_id(str(tmp_path), a_path) == os.path.join("a", "x.c")
    assert gate.unit_id(str(tmp_path), a_path) != gate.unit_id(str(tmp_path), b_path)

    # The fail-open half of the bug: had discovery respelled this to `b/x.c`, its
    # `scanned` digest already matches the current (identical, through the new
    # symlink) content, so the swap would read as already-verified and never gate.
    assert gate.stale_sources(str(tmp_path), state, found) == found


def test_discover_preserves_root_spelling_when_the_project_root_becomes_a_symlink(
    tmp_path: Path,
) -> None:
    # review feedback on PR #171 (issue #161): `project_dir` is a tracked
    # repository subdirectory `repo/a`, and Bash replaces `a/` itself with an
    # in-project symlink `a -> b` where `b/` is already tracked (so git never
    # reports `b/x.c`). `proj_real` now resolves to `repo/b`, and relpathing the
    # deleted `a/x.c` against that resolved boundary escapes (`../a/x.c`),
    # dropping the change — even though `project_dir/x.c` still exists through
    # the alias with content the gate never verified, while the recorded `x.c`
    # stamp and verdicts stand.
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    (repo / "a").mkdir()
    (repo / "b").mkdir()
    (repo / "a" / "x.c").write_text("int fa(void){return 0;}\n")
    (repo / "b" / "x.c").write_text("int fb(void){return 1;}\n")
    _git_commit_all(repo)
    proj = repo / "a"

    state = gate.load_state(str(proj))
    state["scanned"]["x.c"] = gate.content_hash(str(proj / "x.c"))  # pre-swap stamp

    shutil.rmtree(proj)
    proj.symlink_to("b")  # the Bash swap: the project root itself -> symlink

    found = gate.discover_changed_c_sources(str(proj))
    assert found is not None

    # Spelled through the project root's own (lexical) name — the same key a
    # direct PostToolUse edit through the alias produces — not dropped.
    assert found == [str(proj / "x.c")]
    assert gate.unit_id(str(proj), found[0]) == "x.c"

    # The fail-open half of the bug: with the change dropped, nothing re-verifies
    # and the pre-swap `x.c` stamp keeps the swap reading as already-verified.
    # Through the preserved spelling the bytes now behind that name hash against
    # the stamp and are correctly seen as new content.
    assert gate.stale_sources(str(proj), state, found) == found


def test_discover_root_swap_dedupes_reports_that_spell_to_the_same_path(
    tmp_path: Path,
) -> None:
    # Companion to the tracked-target swap: with an *untracked* `b/`, git reports
    # both the deleted `a/x.c` (under the project root's lexical boundary) and
    # the new `b/x.c` (under the resolved one) — and through the symlink both
    # spell `project_dir/x.c`. Discovery must still surface the file exactly
    # once, keyed `x.c`, and read it as stale against a pre-swap stamp.
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    (repo / "a").mkdir()
    (repo / "a" / "x.c").write_text("int fa(void){return 0;}\n")
    _git_commit_all(repo)
    (repo / "b").mkdir()
    (repo / "b" / "x.c").write_text("int fb(void){return 1;}\n")  # untracked
    proj = repo / "a"

    state = gate.load_state(str(proj))
    state["scanned"]["x.c"] = gate.content_hash(str(proj / "x.c"))

    shutil.rmtree(proj)
    proj.symlink_to("b")

    found = gate.discover_changed_c_sources(str(proj))
    assert found is not None

    assert found == [str(proj / "x.c")]  # once, not once per git report
    assert gate.stale_sources(str(proj), state, found) == found


# --- committed-since-baseline discovery (issue #99 review) ------------------


def test_git_head_and_committed_files_since(tmp_path: Path) -> None:
    _git_init(tmp_path)
    (tmp_path / "a.c").write_text("int a(void){return 0;}\n")
    _git_commit_all(tmp_path)
    base = gate.git_head(str(tmp_path))
    assert base is not None
    assert gate.git_committed_files_since(str(tmp_path), base) == []  # no movement

    (tmp_path / "b.c").write_text("int b(void){return 0;}\n")
    _git_commit_all(tmp_path)  # HEAD moves
    assert gate.git_committed_files_since(str(tmp_path), base) == ["b.c"]
    # a None baseline (no commits yet / never seeded) disables the scan
    assert gate.git_committed_files_since(str(tmp_path), None) == []
    # a rewritten/unknown baseline degrades to empty, never raises
    assert gate.git_committed_files_since(str(tmp_path), "0" * 40) == []


def test_git_head_none_without_commits(tmp_path: Path) -> None:
    _git_init(tmp_path)  # a repo, but zero commits
    assert gate.git_head(str(tmp_path)) is None


def test_discover_includes_c_committed_since_baseline(tmp_path: Path) -> None:
    # The review's bypass: a Bash command writes AND commits a C file in one shot,
    # leaving a clean worktree that `git status` cannot see. The committed-since
    # scan recovers it; without the baseline (porcelain only) it is missed.
    _git_init(tmp_path)
    (tmp_path / "seed.txt").write_text("x")
    _git_commit_all(tmp_path)
    base = gate.git_head(str(tmp_path))

    (tmp_path / "committed.c").write_text("int f(void){return 0;}\n")
    _git_commit_all(tmp_path)  # written + committed, worktree now clean

    assert gate.discover_changed_c_sources(str(tmp_path)) == []  # porcelain misses it
    found = gate.discover_changed_c_sources(str(tmp_path), baseline_head=base)
    assert found is not None
    assert [os.path.basename(f) for f in found] == ["committed.c"]


def test_discover_committed_unchanged_is_deduped_by_stale(tmp_path: Path) -> None:
    # A pre-existing dirty file committed *unchanged* is discovered but filtered
    # back out by content-hash freshness — no over-gating of untouched C.
    _git_init(tmp_path)
    (tmp_path / "seed.txt").write_text("x")
    _git_commit_all(tmp_path)
    base = gate.git_head(str(tmp_path))
    wip = tmp_path / "wip.c"
    wip.write_text("int w(void){return 0;}\n")
    state = gate.load_state(str(tmp_path))
    state["scanned"]["wip.c"] = gate.content_hash(str(wip))  # baselined while dirty
    _git_commit_all(tmp_path)  # committed with the SAME content

    found = gate.discover_changed_c_sources(str(tmp_path), baseline_head=base)
    assert found is not None
    assert [os.path.basename(f) for f in found] == ["wip.c"]  # discovered...
    assert gate.stale_sources(str(tmp_path), state, found) == []  # ...but not stale


def test_discover_committed_since_scopes_to_project_subdir(tmp_path: Path) -> None:
    # committed-since paths are repo-root-relative like porcelain, so a commit
    # outside the project subdir is out of scope.
    _git_init(tmp_path)
    (tmp_path / "seed.txt").write_text("x")
    _git_commit_all(tmp_path)
    base = gate.git_head(str(tmp_path))
    sub = tmp_path / "sub"
    sub.mkdir()
    (tmp_path / "top.c").write_text("int t(void){return 0;}\n")  # repo root
    (sub / "inner.c").write_text("int i(void){return 0;}\n")  # under project_dir
    _git_commit_all(tmp_path)

    found = gate.discover_changed_c_sources(str(sub), baseline_head=base)
    assert found is not None
    assert [os.path.basename(f) for f in found] == ["inner.c"]  # top.c out of scope


def test_baseline_scanned_records_head(tmp_path: Path) -> None:
    _git_init(tmp_path)
    (tmp_path / "seed.txt").write_text("x")
    _git_commit_all(tmp_path)
    base = gate.git_head(str(tmp_path))
    gate.baseline_scanned(str(tmp_path))
    assert gate.load_state(str(tmp_path))["baseline_head"] == base


# --- deleted-source reconciliation (issue #99 review) -----------------------


def test_prune_deleted_units_drops_gone_files(tmp_path: Path) -> None:
    kept = tmp_path / "kept.c"
    kept.write_text("int k(void){return 0;}\n")
    state = gate.load_state(str(tmp_path))
    # one unit whose file still exists, one whose file is gone
    gate.record(state, gate.UnitVerdict("kept.c::k", "kept.c", "k", "violated", 1))
    gate.record(state, gate.UnitVerdict("gone.c::g", "gone.c", "g", "violated", 1))
    state["scanned"] = {"kept.c": "h1", "gone.c": "h2"}

    pruned = gate.prune_deleted_units(state, str(tmp_path))
    assert pruned == ["gone.c::g"]
    assert set(state["units"]) == {"kept.c::k"}  # present file untouched
    assert "gone.c" not in state["scanned"]  # stale baseline cleared
    assert state["scanned"]["kept.c"] == "h1"


def test_prune_deleted_units_keeps_units_without_a_file_field(tmp_path: Path) -> None:
    # A malformed/legacy unit with no `file` we cannot locate — keep it, never guess.
    state = {"units": {"x::f": {"verdict": "violated"}}, "scanned": {}}
    assert gate.prune_deleted_units(state, str(tmp_path)) == []
    assert "x::f" in state["units"]


# --- content-hash freshness / dedup ----------------------------------------


def test_verify_and_record_stamps_scanned_and_dedups(tmp_path: Path) -> None:
    # a pointer unit is NEEDS_CONTRACT: recorded without ever shelling to ESBMC.
    src = tmp_path / "buf.c"
    src.write_text("int f(int *p){return *p;}\n")

    gate.verify_and_record(str(src), project_dir=str(tmp_path))
    state = gate.load_state(str(tmp_path))
    assert state["scanned"]["buf.c"] == gate.content_hash(str(src))
    # unchanged content → not stale (this dedup is what protects stop_attempts)
    assert gate.stale_sources(str(tmp_path), state, [str(src)]) == []

    src.write_text("int f(int *p){return p[1];}\n")  # out-of-band modification
    assert gate.stale_sources(str(tmp_path), state, [str(src)]) == [str(src)]


# --- killed-verify freshness (issue #140) -----------------------------------


class _Killed(RuntimeError):
    """Stand-in for a mid-verify hook kill: the run never reaches its final write."""


def _enumerate_one_unit(monkeypatch: pytest.MonkeyPatch, name: str = "f") -> None:
    """Enumerate one non-pointer unit without shelling out to `forseti list-units`."""
    monkeypatch.setattr(
        gate,
        "extract_function_defs",
        lambda file_path, *, project_dir, content=None: [
            gate.FuncDef(name, takes_pointer=False)
        ],
    )


def _kill_during_verify(monkeypatch: pytest.MonkeyPatch, calls: list[str]) -> None:
    """Make every `verify_function` call die as if the hook were killed."""

    def _boom(
        file_path: str,
        function: str,
        *,
        project_dir: str,
        k: int = gate.DEFAULT_K,
        verify_path: str | None = None,
    ) -> NoReturn:
        calls.append(function)
        raise _Killed(function)

    monkeypatch.setattr(gate, "verify_function", _boom)


def test_killed_verify_leaves_file_stale_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The hole: the up-front `scanned` stamp makes a killed verify look content-fresh
    # while its units sit at pending `unknown`, so no later scan would ever retry it
    # and the gate could only block its way to a residual.
    src = tmp_path / "slow.c"
    src.write_text("int f(void){return 0;}\n")
    _enumerate_one_unit(monkeypatch)
    _kill_during_verify(monkeypatch, [])

    with pytest.raises(_Killed):
        gate.verify_and_record(str(src), project_dir=str(tmp_path))

    state = gate.load_state(str(tmp_path))
    assert state["scanned"]["slow.c"] == gate.content_hash(str(src))  # still stamped
    assert state["units"]["slow.c::f"]["verdict"] == "unknown"  # still blocking
    # ...and now ALSO stale, so the next scan re-runs the never-finished verify.
    assert gate.stale_sources(str(tmp_path), state, [str(src)]) == [str(src)]


def test_completed_verify_clears_the_retry_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "done.c"
    src.write_text("int f(void){return 0;}\n")
    _enumerate_one_unit(monkeypatch)
    monkeypatch.setattr(
        gate,
        "verify_function",
        lambda fp, fn, *, project_dir, k=gate.DEFAULT_K, verify_path=None: (
            gate.UnitVerdict("done.c::f", "done.c", "f", "verified", k)
        ),
    )

    gate.verify_and_record(str(src), project_dir=str(tmp_path))
    state = gate.load_state(str(tmp_path))
    assert state["pending"] == {}
    # unchanged content → not stale (the dedup that protects stop_attempts)
    assert gate.stale_sources(str(tmp_path), state, [str(src)]) == []


def test_final_unknown_does_not_force_staleness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The reason freshness is keyed on an explicit unfinished-verify marker rather
    # than on "any unknown/error unit": a genuine ESBMC-timeout `unknown` is a FINAL
    # verdict. Treating it as stale would re-verify the file on every single scan,
    # resetting stop_attempts each round — an unbounded loop instead of a residual.
    src = tmp_path / "hard.c"
    src.write_text("int f(void){return 0;}\n")
    _enumerate_one_unit(monkeypatch)
    monkeypatch.setattr(
        gate,
        "verify_function",
        lambda fp, fn, *, project_dir, k=gate.DEFAULT_K, verify_path=None: (
            gate.UnitVerdict(
                "hard.c::f", "hard.c", "f", "unknown", k, detail="timeout after 110s"
            )
        ),
    )

    gate.verify_and_record(str(src), project_dir=str(tmp_path))
    state = gate.load_state(str(tmp_path))
    assert gate.blocking_units(state)  # it still blocks — it is not a pass
    assert gate.stale_sources(str(tmp_path), state, [str(src)]) == []  # but not retried


def test_blocking_error_on_a_retry_spends_an_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Every error path returns BEFORE the block that bumps the retry counter, so a
    # marker left behind by a previous kill would make a persistently-erroring file
    # re-verify forever with the counter frozen. The error charges the marker one
    # attempt instead — the killed run's units stay retryable, and the retries still
    # run out.
    #
    # The charge happens even though the verdict is *not* recorded here: the killed
    # run stamped these bytes, so a `?` would be unclearable the moment that run's
    # successor finishes, while the retry budget still has to shrink. What keeps the
    # turn blocked is that run's own pending `unknown`.
    src = tmp_path / "broken.c"
    src.write_text("int f(void){return 0;}\n")
    _enumerate_one_unit(monkeypatch)
    _kill_during_verify(monkeypatch, [])
    with pytest.raises(_Killed):
        gate.verify_and_record(str(src), project_dir=str(tmp_path))

    def _unavailable(
        file_path: str | os.PathLike[str],
        *,
        project_dir: str,
        content: bytes | None = None,
    ) -> NoReturn:
        raise gate.UnitsUnavailable("forseti CLI could not be launched")

    monkeypatch.setattr(gate, "extract_function_defs", _unavailable)
    verdicts = gate.verify_and_record(str(src), project_dir=str(tmp_path))

    assert verdicts == []
    state = gate.load_state(str(tmp_path))
    assert "broken.c::?" not in state["units"]
    assert state["pending"]["broken.c"]["attempts"] == 2  # charged, not frozen
    assert gate.blocking_units(state)  # the killed run's unit keeps the turn blocked
    # The kill's pending `unknown` unit is still retryable — the point of the marker.
    assert gate.stale_sources(str(tmp_path), state, [str(src)]) == [str(src)]


def test_prune_deleted_units_clears_the_retry_marker(tmp_path: Path) -> None:
    state = {
        "units": {"gone.c::f": {"verdict": "unknown", "file": "gone.c"}},
        "scanned": {"gone.c": "abc"},
        "pending": {"gone.c": {"hash": "abc", "attempts": 1}},
    }
    assert gate.prune_deleted_units(state, str(tmp_path)) == ["gone.c::f"]
    assert state["pending"] == {}  # nothing left to retry — the file is gone


def test_post_bash_retries_a_killed_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Behavioral: the killed verify is actually re-run by the next Bash scan, and
    # the retry's verdict lands (the file no longer sits at pending `unknown`).
    _git_init(tmp_path)
    src = tmp_path / "oob.c"
    src.write_text("int f(void){return 0;}\n")
    _enumerate_one_unit(monkeypatch)

    calls: list[str] = []
    _kill_during_verify(monkeypatch, calls)
    with pytest.raises(_Killed):
        _run(post_bash.main, tmp_path, monkeypatch)
    assert calls == ["f"]

    monkeypatch.setattr(
        gate,
        "verify_function",
        lambda fp, fn, *, project_dir, k=gate.DEFAULT_K, verify_path=None: (
            gate.UnitVerdict("oob.c::f", "oob.c", "f", "verified", k)
        ),
    )
    assert _run(post_bash.main, tmp_path, monkeypatch) == 0  # re-verified, not skipped
    state = gate.load_state(str(tmp_path))
    assert state["units"]["oob.c::f"]["verdict"] == "verified"
    assert state["pending"] == {}


def test_killed_verify_retries_are_capped_then_residual(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A file that can NEVER finish inside the hook budget must not retry forever:
    # each retry resets stop_attempts, so an uncapped retry would loop instead of
    # ever reaching the loud residual. After the cap it goes quiet and the recorded
    # pending `unknown` units carry the block to MAX_STOP_ATTEMPTS.
    _git_init(tmp_path)
    src = tmp_path / "endless.c"
    src.write_text("int f(void){return 0;}\n")
    _enumerate_one_unit(monkeypatch)
    calls: list[str] = []
    _kill_during_verify(monkeypatch, calls)

    for _ in range(gate.MAX_PENDING_VERIFY_ATTEMPTS + 2):
        with contextlib.suppress(_Killed):
            _run(post_bash.main, tmp_path, monkeypatch)
    assert len(calls) == gate.MAX_PENDING_VERIFY_ATTEMPTS  # capped, not once per scan

    # Exhausted: the Stop-gate no longer sees it as stale, so its patience runs out
    # and the still-pending unit ends the turn as a LOUD residual.
    for _ in range(gate.MAX_STOP_ATTEMPTS + 1):
        _run(stop_gate.main, tmp_path, monkeypatch)
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert "decision" not in out  # allowed to end...
    assert "UNVERIFIED" in out["systemMessage"]  # ...loudly, never a silent pass
    assert "endless.c::f" in out["systemMessage"]


# --- concurrent runs own their own retry marker (PR #148 review) ------------


def _concurrent_run_starts(project_dir: Path, path: Path, content: str) -> str:
    """Replay a *second* `verify_and_record` run's up-front block, then "get killed".

    Writes `content`, stamps `scanned`, stores that run's own unfinished-verify
    marker (counter bumped exactly as the real block does) and pre-records the unit
    as pending `unknown` — the state a concurrent PostToolUse hook leaves behind
    when it is killed before its verdicts land. Returns the content's digest.
    """
    path.write_text(content)
    rel = gate.unit_id(str(project_dir), str(path))
    digest = gate.content_hash(str(path))
    assert digest is not None
    with gate.gate_lock(str(project_dir)):
        state = gate.load_state(str(project_dir))
        state["scanned"][rel] = digest
        prior = gate._pending_attempts(state["pending"].get(rel), digest)
        state["pending"][rel] = {
            "hash": digest,
            "attempts": (prior or 0) + 1,
            "pid": os.getpid() + 1,
        }
        gate.record(
            state,
            gate.UnitVerdict(
                f"{rel}::f",
                rel,
                "f",
                "unknown",
                gate.DEFAULT_K,
                detail="verification pending",
            ),
        )
        gate.save_state(str(project_dir), state)
    return digest


def test_cleanup_preserves_a_newer_runs_pending_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The gate supports concurrent PostToolUse hooks on one path across successive
    # edits, so an older run can reach its cleanup after a NEWER-content run stored
    # its own marker. Dropping that marker leaves the file hashing fresh (the newer
    # run stamped `scanned` with its own digest) with nothing left to retry, so a
    # kill of the newer run would never be re-verified.
    src = tmp_path / "race.c"
    src.write_text("int f(void){return 0;}\n")
    _enumerate_one_unit(monkeypatch)
    newer: dict[str, str] = {}

    def _verify_while_a_newer_run_starts(
        fp: str,
        fn: str,
        *,
        project_dir: str,
        k: int = gate.DEFAULT_K,
        verify_path: str | None = None,
    ) -> gate.UnitVerdict:
        newer["digest"] = _concurrent_run_starts(
            tmp_path, src, "int f(void){return 1;}\n"
        )
        return gate.UnitVerdict("race.c::f", "race.c", "f", "verified", k)

    monkeypatch.setattr(gate, "verify_function", _verify_while_a_newer_run_starts)
    gate.verify_and_record(str(src), project_dir=str(tmp_path))

    state = gate.load_state(str(tmp_path))
    entry = state["pending"]["race.c"]  # the newer run's marker survives
    assert entry["hash"] == newer["digest"]
    # ...so its killed verify is still retried instead of being skipped as fresh.
    assert state["scanned"]["race.c"] == newer["digest"]  # content-fresh by hash
    assert gate.stale_sources(str(tmp_path), state, [str(src)]) == [str(src)]


def test_cleanup_preserves_a_concurrent_retry_of_the_same_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Same bytes, two live runs: the second bumps the counter under the lock and its
    # pre-record puts the units back to pending `unknown`. Matching on content alone
    # would let this run pop that marker, leaving those `unknown` units content-fresh
    # and unretryable if the second run is killed — hence the ownership token.
    src = tmp_path / "same.c"
    src.write_text("int f(void){return 0;}\n")
    digest = gate.content_hash(str(src))
    _enumerate_one_unit(monkeypatch)

    def _verify_while_same_content_retries(
        fp: str,
        fn: str,
        *,
        project_dir: str,
        k: int = gate.DEFAULT_K,
        verify_path: str | None = None,
    ) -> gate.UnitVerdict:
        _concurrent_run_starts(tmp_path, src, src.read_text())  # identical bytes
        return gate.UnitVerdict("same.c::f", "same.c", "f", "verified", k)

    monkeypatch.setattr(gate, "verify_function", _verify_while_same_content_retries)
    gate.verify_and_record(str(src), project_dir=str(tmp_path))

    state = gate.load_state(str(tmp_path))
    assert state["pending"]["same.c"] == {
        "hash": digest,
        "attempts": 2,
        "pid": os.getpid() + 1,
    }
    assert gate.stale_sources(str(tmp_path), state, [str(src)]) == [str(src)]


def test_cleanup_survives_a_concurrent_error_charging_its_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The other half of the charge: a concurrent run that fails to enumerate spends an
    # attempt on THIS run's marker — it cannot tell whose claim it is, and dropping it
    # would strand a killed run's units. That rewrites the counter, so identifying the
    # marker by its whole contents would stop recognising the owner's own claim: the
    # finished file would keep a marker nothing needs to retry and be re-verified on
    # the next scan. Ownership is `hash` + `pid`, which the charge leaves alone.
    src = tmp_path / "charged.c"
    src.write_text("int f(void){return 0;}\n")
    _enumerate_one_unit(monkeypatch)

    def _verify_while_a_concurrent_scan_errors(
        fp: str,
        fn: str,
        *,
        project_dir: str,
        k: int = gate.DEFAULT_K,
        verify_path: str | None = None,
    ) -> gate.UnitVerdict:
        def _unavailable(
            file_path: str | os.PathLike[str],
            *,
            project_dir: str,
            content: bytes | None = None,
        ) -> NoReturn:
            raise gate.UnitsUnavailable("forseti CLI could not be launched")

        monkeypatch.setattr(gate, "extract_function_defs", _unavailable)
        gate.verify_and_record(str(src), project_dir=project_dir)  # charges the marker
        assert gate.load_state(project_dir)["pending"]["charged.c"]["attempts"] == 2
        return gate.UnitVerdict("charged.c::f", "charged.c", "f", "verified", k)

    monkeypatch.setattr(gate, "verify_function", _verify_while_a_concurrent_scan_errors)
    gate.verify_and_record(str(src), project_dir=str(tmp_path))

    state = gate.load_state(str(tmp_path))
    assert state["pending"] == {}  # the owner still cleared the marker it stored...
    # ...so a file whose verdicts are all final is not re-verified for nothing.
    assert gate.stale_sources(str(tmp_path), state, [str(src)]) == []


def test_blocking_error_charge_stops_at_the_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The charge spends a retry budget, it does not keep a tally: once the budget is
    # gone, a further error must leave the counter at the cap rather than counting
    # it up forever (PR #148 review).
    #
    # Reaching that charge takes a spent marker on a file that is still *offered* —
    # a capped marker alone retires the file, and then nothing is published at all
    # (see the deferral tests). Here the stamp its killed run left has since been
    # withdrawn by another run's drift check, so the file reads stale for the other
    # reason while the marker for these bytes is already spent.
    src = tmp_path / "spent.c"
    digest = _concurrent_run_starts(tmp_path, src, "int f(void){return 0;}\n")
    with gate.gate_lock(str(tmp_path)):
        state = gate.load_state(str(tmp_path))
        state["pending"]["spent.c"]["attempts"] = gate.MAX_PENDING_VERIFY_ATTEMPTS
        del state["scanned"]["spent.c"]
        gate.save_state(str(tmp_path), state)

    def _unavailable(
        file_path: str | os.PathLike[str],
        *,
        project_dir: str,
        content: bytes | None = None,
    ) -> NoReturn:
        raise gate.UnitsUnavailable("forseti CLI could not be launched")

    monkeypatch.setattr(gate, "extract_function_defs", _unavailable)
    verdicts = gate.verify_and_record(str(src), project_dir=str(tmp_path))

    assert [v.verdict for v in verdicts] == ["error"]  # published, so it did charge
    state = gate.load_state(str(tmp_path))
    assert state["pending"]["spent.c"] == {
        "hash": digest,
        "attempts": gate.MAX_PENDING_VERIFY_ATTEMPTS,  # clamped, not MAX + 1
        "pid": os.getpid() + 1,
    }


def test_blocking_error_preserves_a_newer_runs_pending_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The error cleanup is scoped the same way: it touches only a marker recording
    # the bytes THIS run read. `raw` is read before the units are enumerated, so a
    # newer run's marker — a claim on content this error says nothing about — must
    # come through the error unchanged, counter included.
    #
    # It comes through untouched here in the strongest way — nothing is written at
    # all. The newer run's stamp vouches for what is on disk, so this error is
    # deferred, and the claim it would otherwise charge records *other* bytes than
    # the ones this run read.
    src = tmp_path / "err.c"
    src.write_text("int f(void){return 0;}\n")
    newer: dict[str, str] = {}

    def _newer_run_starts_then_enumeration_fails(
        file_path: str | os.PathLike[str],
        *,
        project_dir: str,
        content: bytes | None = None,
    ) -> NoReturn:
        newer["digest"] = _concurrent_run_starts(
            tmp_path, src, "int f(void){return 1;}\n"
        )
        raise gate.UnitsUnavailable("forseti CLI could not be launched")

    monkeypatch.setattr(
        gate, "extract_function_defs", _newer_run_starts_then_enumeration_fails
    )
    verdicts = gate.verify_and_record(str(src), project_dir=str(tmp_path))

    assert verdicts == []  # deferred to the run that owns what is on disk
    state = gate.load_state(str(tmp_path))
    assert "err.c::?" not in state["units"]
    assert state["pending"]["err.c"] == {
        "hash": newer["digest"],
        "attempts": 1,
        "pid": os.getpid() + 1,
    }
    assert gate.stale_sources(str(tmp_path), state, [str(src)]) == [str(src)]


def _newer_run_finishes(project_dir: Path, path: Path, content: str) -> str:
    """The same second run, but one that *completed*: stamped, verified, no claim."""
    path.write_text(content)
    rel = gate.unit_id(str(project_dir), str(path))
    digest = gate.content_hash(str(path))
    assert digest is not None
    with gate.gate_lock(str(project_dir)):
        state = gate.load_state(str(project_dir))
        state["scanned"][rel] = digest
        gate.record(
            state,
            gate.UnitVerdict(f"{rel}::f", rel, "f", "verified", gate.DEFAULT_K),
        )
        gate.save_state(str(project_dir), state)
    return digest


def test_enumeration_failure_defers_to_a_run_that_already_finished(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The unclearable shape, one caller over from the drift error. This run reads
    # A, a concurrent run replaces it with B and verifies B through, and then this
    # run's enumeration of A fails. Published, `err.c::?` would never be cleared:
    # B's stamp matches disk and B left no claim, so `stale_sources` never re-offers
    # the file and the reconcile that prunes `?` never runs — the Stop gate would
    # block to the cap on a file B legitimately verified.
    src = tmp_path / "err.c"
    src.write_text("int f(void){return 0;}\n")
    newer: dict[str, str] = {}

    def _newer_run_finishes_then_enumeration_fails(
        file_path: str | os.PathLike[str],
        *,
        project_dir: str,
        content: bytes | None = None,
    ) -> NoReturn:
        newer["digest"] = _newer_run_finishes(tmp_path, src, "int f(void){return 1;}\n")
        raise gate.UnitsUnavailable("forseti CLI could not be launched")

    monkeypatch.setattr(
        gate, "extract_function_defs", _newer_run_finishes_then_enumeration_fails
    )
    verdicts = gate.verify_and_record(str(src), project_dir=str(tmp_path))

    assert verdicts == []  # deferred to the run that owns what is on disk
    state = gate.load_state(str(tmp_path))
    assert "err.c::?" not in state["units"]
    assert state["scanned"]["err.c"] == newer["digest"]  # B's stamp, untouched
    assert gate.blocking_units(state) == []  # B verified it; nothing left to block
    # Why publishing would have stuck: nothing marks the file for a re-scan.
    assert gate.stale_sources(str(tmp_path), state, [str(src)]) == []


def test_enumeration_failure_defers_on_content_already_gated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A no-op edit of content that is already stamped, with `esbmc` since gone from
    # `PATH` (or a `TemporaryDirectory` that would not clean up). The stamp equals
    # both what is on disk and the bytes this error is about — which is the
    # tempting case for keeping the block, and the wrong one: those very bytes were
    # enumerated by the run that stamped them and its verdicts still describe them,
    # so this adds nothing and its `?` would be unclearable on top of a verified
    # file. The tooling failure surfaces on the next edit that *changes* the file,
    # where no stamp matches — see below.
    src = tmp_path / "err.c"
    src.write_text("int f(void){return 0;}\n")
    _newer_run_finishes(tmp_path, src, "int f(void){return 0;}\n")

    def _unavailable(
        file_path: str | os.PathLike[str],
        *,
        project_dir: str,
        content: bytes | None = None,
    ) -> NoReturn:
        raise gate.UnitsUnavailable("esbmc not found on PATH")

    monkeypatch.setattr(gate, "extract_function_defs", _unavailable)
    verdicts = gate.verify_and_record(str(src), project_dir=str(tmp_path))

    assert verdicts == []
    state = gate.load_state(str(tmp_path))
    assert "err.c::?" not in state["units"]
    assert state["units"]["err.c::f"]["verdict"] == "verified"  # what still stands


def test_enumeration_failure_blocks_when_the_stamp_is_for_other_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The discrimination that keeps the deferral honest: a stamp exists, but for
    # content that is no longer on disk, so nobody has gated what is there now.
    # This must block — and it is clearable, because the file reads stale.
    src = tmp_path / "err.c"
    src.write_text("int f(void){return 0;}\n")
    _newer_run_finishes(tmp_path, src, "int f(void){return 0;}\n")
    src.write_text("int f(void){return 1;}\n")  # edited past the stamp

    def _unavailable(
        file_path: str | os.PathLike[str],
        *,
        project_dir: str,
        content: bytes | None = None,
    ) -> NoReturn:
        raise gate.UnitsUnavailable("esbmc not found on PATH")

    monkeypatch.setattr(gate, "extract_function_defs", _unavailable)
    verdicts = gate.verify_and_record(str(src), project_dir=str(tmp_path))

    assert [v.verdict for v in verdicts] == ["error"]
    state = gate.load_state(str(tmp_path))
    assert state["units"]["err.c::?"]["verdict"] == "error"
    assert gate.blocking_units(state)
    assert gate.stale_sources(str(tmp_path), state, [str(src)]) == [str(src)]


def test_blocking_error_preserves_a_concurrent_same_content_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two hooks on the SAME unchanged bytes: one is killed after storing its marker,
    # the other fails to enumerate. The failing run never created a marker of its
    # own — every error path returns before that block — so a delete here could only
    # drop the killed run's claim, leaving its pending `unknown` units content-fresh
    # (that run stamped `scanned` with these bytes) and unretryable. It charges the
    # marker one attempt instead: the claim survives, the retry budget still shrinks.
    src = tmp_path / "shared.c"
    digest = _concurrent_run_starts(tmp_path, src, "int f(void){return 0;}\n")

    def _unavailable(
        file_path: str | os.PathLike[str],
        *,
        project_dir: str,
        content: bytes | None = None,
    ) -> NoReturn:
        raise gate.UnitsUnavailable("forseti CLI could not be launched")

    monkeypatch.setattr(gate, "extract_function_defs", _unavailable)
    verdicts = gate.verify_and_record(str(src), project_dir=str(tmp_path))

    assert verdicts == []  # the killed run stamped these bytes; it owns the file
    state = gate.load_state(str(tmp_path))
    assert state["pending"]["shared.c"] == {
        "hash": digest,
        "attempts": 2,  # charged anyway — the budget shrinks even when quiet...
        "pid": os.getpid() + 1,  # ...and it is still the creating run's marker
    }
    assert state["units"]["shared.c::f"]["verdict"] == "unknown"  # still pending
    assert state["scanned"]["shared.c"] == digest  # content-fresh by hash...
    # ...and still retried, so the killed run's units are not stranded.
    assert gate.stale_sources(str(tmp_path), state, [str(src)]) == [str(src)]


def test_errored_retries_are_capped_then_residual(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Behavioral counterpart: a file that errors on every scan must not re-verify
    # forever. Each error blocks and resets stop_attempts, so an uncharged marker
    # would loop; charging it retires the retry after MAX_PENDING_VERIFY_ATTEMPTS and
    # the recorded error + pending unit reach the LOUD residual.
    _git_init(tmp_path)
    src = tmp_path / "cursed.c"
    src.write_text("int f(void){return 0;}\n")
    _enumerate_one_unit(monkeypatch)
    _kill_during_verify(monkeypatch, [])
    with contextlib.suppress(_Killed):
        _run(post_bash.main, tmp_path, monkeypatch)  # spends the first attempt

    scans: list[str] = []

    def _unavailable(
        file_path: str | os.PathLike[str],
        *,
        project_dir: str,
        content: bytes | None = None,
    ) -> NoReturn:
        scans.append(str(file_path))
        raise gate.UnitsUnavailable("forseti CLI could not be launched")

    monkeypatch.setattr(gate, "extract_function_defs", _unavailable)
    for _ in range(gate.MAX_PENDING_VERIFY_ATTEMPTS + 2):
        _run(post_bash.main, tmp_path, monkeypatch)
    assert len(scans) == gate.MAX_PENDING_VERIFY_ATTEMPTS - 1  # capped, then quiet

    capsys.readouterr()
    for _ in range(gate.MAX_STOP_ATTEMPTS + 1):
        _run(stop_gate.main, tmp_path, monkeypatch)
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert "decision" not in out  # allowed to end...
    assert "UNVERIFIED" in out["systemMessage"]  # ...loudly, never a silent pass
    assert "cursed.c" in out["systemMessage"]


def test_recovered_enumeration_clears_the_error_and_the_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The other direction: `forseti`/esbmc was briefly unavailable. Charging the
    # marker keeps the file a retry candidate, so the scan that finds the CLI again
    # re-verifies it — and a transient failure cannot leave the turn blocked
    # forever. Two things make that true: the failing scan records no `?` at all
    # while the killed run's stamp still vouches for these bytes, and any `?` that
    # *was* recorded (a scan of content nothing vouches for) is reconciled away by
    # the up-front prune, since `?` is not a function the file defines.
    _git_init(tmp_path)
    src = tmp_path / "flaky.c"
    src.write_text("int f(void){return 0;}\n")
    _enumerate_one_unit(monkeypatch)
    _kill_during_verify(monkeypatch, [])
    with contextlib.suppress(_Killed):
        _run(post_bash.main, tmp_path, monkeypatch)

    def _unavailable(
        file_path: str | os.PathLike[str],
        *,
        project_dir: str,
        content: bytes | None = None,
    ) -> NoReturn:
        raise gate.UnitsUnavailable("forseti CLI could not be launched")

    monkeypatch.setattr(gate, "extract_function_defs", _unavailable)
    _run(post_bash.main, tmp_path, monkeypatch)
    blocked = gate.load_state(str(tmp_path))
    assert "flaky.c::?" not in blocked["units"]  # nothing stranded to clear
    assert gate.blocking_units(blocked)  # ...and the turn is blocked regardless
    assert blocked["pending"]["flaky.c"]["attempts"] == 2  # the budget still shrank

    _enumerate_one_unit(monkeypatch)  # the CLI is back
    monkeypatch.setattr(gate, "verify_function", _verified_verdict("flaky.c"))
    assert _run(post_bash.main, tmp_path, monkeypatch) == 0

    state = gate.load_state(str(tmp_path))
    assert state["units"]["flaky.c::f"]["verdict"] == "verified"
    assert "flaky.c::?" not in state["units"]  # the error is reconciled away
    assert state["pending"] == {}
    assert not gate.blocking_units(state)


def test_unreadable_file_leaves_the_pending_marker_alone(tmp_path: Path) -> None:
    # A run that cannot even read the file never learns which bytes it was scanning,
    # so it cannot name the claim to charge and leaves the marker exactly as it is.
    # (Such a file also stops being scanned at all — `content_hash` fails, so
    # `stale_sources` skips it and the frozen counter cannot loop.)
    target = tmp_path / "dir.c"
    target.mkdir()  # read_bytes → IsADirectoryError, an OSError
    marker = {"hash": "deadbeef", "attempts": 1}
    with gate.gate_lock(str(tmp_path)):
        state = gate.load_state(str(tmp_path))
        state["pending"]["dir.c"] = dict(marker)
        gate.save_state(str(tmp_path), state)

    verdicts = gate.verify_and_record(str(target), project_dir=str(tmp_path))

    assert [v.verdict for v in verdicts] == ["error"]
    state = gate.load_state(str(tmp_path))
    assert state["pending"]["dir.c"] == marker
    assert gate.stale_sources(str(tmp_path), state, [str(target)]) == []


# --- a pending marker is a discovery source of its own (PR #148 review) -----


def _verified_verdict(rel: str, function: str = "f") -> Callable[..., gate.UnitVerdict]:
    """Stand-in `verify_function` whose verdict lands (the retry that finishes)."""

    def _verify(
        fp: str,
        fn: str,
        *,
        project_dir: str,
        k: int = gate.DEFAULT_K,
        verify_path: str | None = None,
    ) -> gate.UnitVerdict:
        return gate.UnitVerdict(f"{rel}::{fn}", rel, fn, "verified", k)

    return _verify


def test_pending_retry_reaches_a_file_git_never_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Both scans feed on git discovery, so a killed verify of a file `git status`
    # never reports — committed and clean here, equally a gitignored or excluded
    # path — was never even offered to `stale_sources`: its pending `unknown` unit
    # could only block its way to a residual. The `pending` marker names it instead.
    _git_init(tmp_path)
    src = tmp_path / "clean.c"
    src.write_text("int f(void){return 0;}\n")
    _git_commit_all(tmp_path)
    _enumerate_one_unit(monkeypatch)
    _kill_during_verify(monkeypatch, [])
    with pytest.raises(_Killed):
        gate.verify_and_record(str(src), project_dir=str(tmp_path))

    assert gate.discover_changed_c_sources(str(tmp_path)) == []  # git sees nothing

    _run(stop_gate.main, tmp_path, monkeypatch)  # named as unverified, not silent
    blocked = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert blocked["decision"] == "block"
    assert "clean.c" in blocked["reason"]

    monkeypatch.setattr(gate, "verify_function", _verified_verdict("clean.c"))
    assert _run(post_bash.main, tmp_path, monkeypatch) == 0  # ...and re-verified
    state = gate.load_state(str(tmp_path))
    assert state["units"]["clean.c::f"]["verdict"] == "verified"
    assert state["pending"] == {}


def test_pending_retry_runs_in_a_non_git_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Out-of-band *discovery* needs git; the retry does not — the file is named by
    # the gate's own state — so a killed verify is still re-run where git is absent.
    src = tmp_path / "nogit.c"
    src.write_text("int f(void){return 0;}\n")
    _enumerate_one_unit(monkeypatch)
    _kill_during_verify(monkeypatch, [])
    with pytest.raises(_Killed):
        gate.verify_and_record(str(src), project_dir=str(tmp_path))
    assert gate.discover_changed_c_sources(str(tmp_path)) is None  # no work tree

    monkeypatch.setattr(gate, "verify_function", _verified_verdict("nogit.c"))
    assert _run(post_bash.main, tmp_path, monkeypatch) == 0
    state = gate.load_state(str(tmp_path))
    assert state["units"]["nogit.c::f"]["verdict"] == "verified"
    assert state["pending"] == {}


def test_pending_retry_of_stale_content_is_left_to_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Only a marker recording the bytes NOW on disk is a retry candidate: once the
    # file has been rewritten, the interrupted verify's content is gone and gating
    # the new bytes is discovery's job, under its own scope rules.
    src = tmp_path / "moved.c"
    src.write_text("int f(void){return 0;}\n")
    _enumerate_one_unit(monkeypatch)
    _kill_during_verify(monkeypatch, [])
    with pytest.raises(_Killed):
        gate.verify_and_record(str(src), project_dir=str(tmp_path))

    state = gate.load_state(str(tmp_path))
    assert gate.pending_retry_sources(str(tmp_path), state) == [str(src)]
    src.write_text("int f(void){return 1;}\n")
    assert gate.pending_retry_sources(str(tmp_path), state) == []


def test_sources_needing_verify_reports_a_file_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A file both scans name can still arrive under two literal spellings (here, a
    # trailing `.` path component) even though discovery and the pending scan both
    # join through `project_dir` now (issue #161) — verifying it twice in one scan
    # would double the ESBMC cost and name it twice in the Stop note.
    src = tmp_path / "dup.c"
    src.write_text("int f(void){return 0;}\n")
    _enumerate_one_unit(monkeypatch)
    _kill_during_verify(monkeypatch, [])
    with pytest.raises(_Killed):
        gate.verify_and_record(str(src), project_dir=str(tmp_path))

    state = gate.load_state(str(tmp_path))
    # The same file under discovery's spelling.
    discovered = [os.path.join(str(tmp_path), ".", "dup.c")]
    assert gate.sources_needing_verify(str(tmp_path), state, discovered) == discovered


# --- staged / committed blob freshness (issue #99 review) -------------------


def _stage(path: Path, name: str) -> None:
    subprocess.run(["git", "-C", str(path), "add", name], check=True)


def test_staged_paths_from_porcelain() -> None:
    # Only a set X-status (index change) counts as staged: ' M' (worktree-only) and
    # '??' (untracked) are excluded; a rename ('R  new\0old') keeps the new path.
    out = "MM a.c\x00 M b.c\x00A  c.c\x00?? d.c\x00D  e.c\x00R  new.c\x00old.c\x00"
    assert gate._staged_paths_from_porcelain(out) == ["a.c", "c.c", "e.c", "new.c"]


def test_git_blob_hash(tmp_path: Path) -> None:
    _git_init(tmp_path)
    src = tmp_path / "foo.c"
    src.write_text("int f(void){return 0;}\n")  # A
    _git_commit_all(tmp_path)
    a_hash = gate.content_hash(str(src))

    src.write_text("int f(void){return 1;}\n")  # B, staged
    _stage(tmp_path, "foo.c")
    b_hash = gate.content_hash(str(src))
    src.write_text("int f(void){return 0;}\n")  # worktree reverted to A

    assert gate.git_blob_hash(str(tmp_path), ":foo.c") == b_hash  # index holds B
    assert gate.git_blob_hash(str(tmp_path), "HEAD:foo.c") == a_hash  # HEAD holds A
    assert gate.git_blob_hash(str(tmp_path), "HEAD:nope.c") is None  # no such blob


def test_divergent_blob_sources_flags_staged_blob(tmp_path: Path) -> None:
    # The review's bypass: stage a divergent blob, then revert the worktree so it
    # hashes as the last-verified content. `stale_sources` sees nothing; the staged
    # blob would still commit unverified.
    _git_init(tmp_path)
    src = tmp_path / "foo.c"
    src.write_text("int f(void){return 0;}\n")  # A
    _git_commit_all(tmp_path)
    state = gate.load_state(str(tmp_path))
    state["scanned"]["foo.c"] = gate.content_hash(str(src))  # A verified

    src.write_text("int f(void){return 1;}\n")  # B
    _stage(tmp_path, "foo.c")  # index = B
    src.write_text("int f(void){return 0;}\n")  # worktree back to A (hashes fresh)

    assert gate.stale_sources(str(tmp_path), state, [str(src)]) == []  # worktree fresh
    assert gate.divergent_blob_sources(str(tmp_path), state) == [
        {"rel": "foo.c", "reason": "staged"}
    ]


def test_divergent_blob_sources_flags_committed_blob(tmp_path: Path) -> None:
    # The committed variant: commit a divergent blob, revert the worktree. `HEAD`
    # holds unverified C while the worktree hashes clean.
    _git_init(tmp_path)
    src = tmp_path / "foo.c"
    src.write_text("int f(void){return 0;}\n")  # A
    _git_commit_all(tmp_path)
    base = gate.git_head(str(tmp_path))
    state = gate.load_state(str(tmp_path))
    state["scanned"]["foo.c"] = gate.content_hash(str(src))

    src.write_text("int f(void){return 1;}\n")  # B
    _git_commit_all(tmp_path)  # HEAD now B
    src.write_text("int f(void){return 0;}\n")  # worktree reverted to A

    assert gate.stale_sources(str(tmp_path), state, [str(src)]) == []
    assert gate.divergent_blob_sources(str(tmp_path), state, baseline_head=base) == [
        {"rel": "foo.c", "reason": "committed"}
    ]


def test_divergent_blob_sources_committed_survives_worktree_delete(
    tmp_path: Path,
) -> None:
    # `cat > new.c && git add new.c && rm new.c && git commit`: HEAD holds the blob
    # but the worktree file is gone, so discovery's isfile filter drops it. The blob
    # scan must catch it directly, or committed C ends the turn with no verdict.
    _git_init(tmp_path)
    (tmp_path / "seed.txt").write_text("x")
    _git_commit_all(tmp_path)
    base = gate.git_head(str(tmp_path))
    state = gate.load_state(str(tmp_path))

    new = tmp_path / "new.c"
    new.write_text("int g(void){return 0;}\n")
    _stage(tmp_path, "new.c")
    new.unlink()  # rm before the commit — worktree file gone, blob still staged
    # commit the staged index directly (not `git add -A`, which would restage the
    # worktree deletion and drop the blob) — HEAD now carries new.c
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "add new"], check=True)

    # discovery misses it (nothing to verify on disk) — proves the blob scan is not
    # bounded by discovery.
    assert gate.discover_changed_c_sources(str(tmp_path), baseline_head=base) == []
    assert gate.divergent_blob_sources(str(tmp_path), state, baseline_head=base) == [
        {"rel": "new.c", "reason": "committed"}
    ]


def test_divergent_blob_sources_dedups_verified_staged(tmp_path: Path) -> None:
    # A normal `git add` of already-verified C must NOT be gated: the staged blob
    # equals the recorded scanned hash, so it dedups straight out.
    _git_init(tmp_path)
    src = tmp_path / "foo.c"
    src.write_text("int f(void){return 0;}\n")  # A committed
    _git_commit_all(tmp_path)
    src.write_text("int f(void){return 2;}\n")  # A' — worktree edit, verified
    state = gate.load_state(str(tmp_path))
    state["scanned"]["foo.c"] = gate.content_hash(str(src))  # A' verified
    _stage(tmp_path, "foo.c")  # stage the verified A' (index = scanned)

    assert gate.divergent_blob_sources(str(tmp_path), state) == []


def test_divergent_blob_sources_ignores_worktree_only_edit(tmp_path: Path) -> None:
    # ' M': a verified worktree edit that was never staged. The index still holds the
    # OLD HEAD blob; it must not be mistaken for a divergent staged blob (over-gate).
    _git_init(tmp_path)
    src = tmp_path / "foo.c"
    src.write_text("int f(void){return 0;}\n")  # A committed
    _git_commit_all(tmp_path)
    src.write_text("int f(void){return 1;}\n")  # B, edited but NOT staged
    state = gate.load_state(str(tmp_path))
    state["scanned"]["foo.c"] = gate.content_hash(str(src))  # B verified (worktree)

    assert gate.divergent_blob_sources(str(tmp_path), state) == []


def test_divergent_blob_sources_non_git_is_empty(tmp_path: Path) -> None:
    (tmp_path / "a.c").write_text("int a(void){return 0;}\n")
    state = gate.load_state(str(tmp_path))
    assert gate.divergent_blob_sources(str(tmp_path), state) == []


# --- Stop-gate backstop -----------------------------------------------------


def test_stop_gate_blocks_on_out_of_band(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _git_init(tmp_path)
    (tmp_path / "oob.c").write_text("int f(void){return 0;}\n")  # never verified

    _run(stop_gate.main, tmp_path, monkeypatch)
    out = json.loads(capsys.readouterr().out)
    assert out.get("decision") == "block"
    assert "out-of-band" in out["reason"] and "oob.c" in out["reason"]


def test_stop_gate_allows_when_out_of_band_is_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _git_init(tmp_path)
    src = tmp_path / "oob.c"
    src.write_text("int f(void){return 0;}\n")
    # simulate a prior verify: unit VERIFIED and its content stamped fresh
    state = gate.load_state(str(tmp_path))
    state["scanned"]["oob.c"] = gate.content_hash(str(src))
    gate.record(state, gate.UnitVerdict("oob.c::f", "oob.c", "f", "verified", 1))
    gate.save_state(str(tmp_path), state)

    rc = _run(stop_gate.main, tmp_path, monkeypatch)
    assert rc == 0 and capsys.readouterr().out.strip() == ""  # clean allow, silent


def test_stop_gate_residual_after_max_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _git_init(tmp_path)
    (tmp_path / "oob.c").write_text("int f(void){return 0;}\n")
    state = gate.load_state(str(tmp_path))
    state["stop_attempts"] = gate.MAX_STOP_ATTEMPTS  # next attempt exceeds the cap
    gate.save_state(str(tmp_path), state)

    _run(stop_gate.main, tmp_path, monkeypatch)
    out = json.loads(capsys.readouterr().out)
    assert "decision" not in out  # allowed to end...
    assert "out-of-band" in out["systemMessage"]  # ...but with a LOUD residual


def test_stop_gate_prunes_untracked_deleted_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The review's real case: a C file WRITTEN out-of-band via Bash is untracked,
    # so once it is removed `git status` never reports it as deleted. Its VIOLATED
    # unit must still be pruned by file existence — not block the turn forever.
    _git_init(tmp_path)
    src = tmp_path / "new.c"
    src.write_text("int f(void){return 0;}\n")
    state = gate.load_state(str(tmp_path))
    state["scanned"]["new.c"] = gate.content_hash(str(src))
    gate.record(state, gate.UnitVerdict("new.c::f", "new.c", "f", "violated", 1))
    gate.save_state(str(tmp_path), state)
    src.unlink()  # `rm new.c` via Bash — untracked, so git status shows nothing

    rc = _run(stop_gate.main, tmp_path, monkeypatch)
    assert rc == 0 and capsys.readouterr().out.strip() == ""  # not blocked
    after = gate.load_state(str(tmp_path))
    assert "new.c::f" not in after["units"]  # stale unit pruned
    assert "new.c" not in after["scanned"]  # baseline cleared for a future recreate
    assert any(
        e.get("decision") == "pruned_deleted" for e in event_log.read_events(tmp_path)
    )  # reconcile is traced, never silent


def test_stop_gate_prunes_committed_deleted_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The committed-then-deleted variant: git DOES report `old.c` as deleted here,
    # but the same file-existence prune handles it with no git-status special case.
    _git_init(tmp_path)
    src = tmp_path / "old.c"
    src.write_text("int f(void){return 0;}\n")
    _git_commit_all(tmp_path)
    state = gate.load_state(str(tmp_path))
    gate.record(state, gate.UnitVerdict("old.c::f", "old.c", "f", "unknown", 1))
    gate.save_state(str(tmp_path), state)
    src.unlink()  # `rm old.c` via Bash — git status now shows ` D old.c`

    rc = _run(stop_gate.main, tmp_path, monkeypatch)
    assert rc == 0 and capsys.readouterr().out.strip() == ""  # not blocked
    assert "old.c::f" not in gate.load_state(str(tmp_path))["units"]


def test_stop_gate_blocks_on_c_committed_in_same_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The review's bypass: a Bash command that writes AND commits a C file in one
    # shot leaves a clean worktree; the baseline-HEAD scan must still block on it.
    _git_init(tmp_path)
    (tmp_path / "seed.txt").write_text("x")
    _git_commit_all(tmp_path)
    _run(session_start.main, tmp_path, monkeypatch, source="startup")  # baselines HEAD

    (tmp_path / "committed.c").write_text("int f(void){return 0;}\n")
    _git_commit_all(tmp_path)  # written + committed → worktree clean

    _run(stop_gate.main, tmp_path, monkeypatch)
    out = json.loads(capsys.readouterr().out)
    assert out.get("decision") == "block"
    assert "out-of-band" in out["reason"] and "committed.c" in out["reason"]


def test_stop_gate_blocks_on_verified_unit_modified_then_committed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The property that actually closes the hole: a unit already VERIFIED at its
    # last content, then changed AND committed in one Bash command, is re-gated —
    # never a silent pass on the new, unverified content.
    _git_init(tmp_path)
    src = tmp_path / "u.c"
    src.write_text("int f(void){return 0;}\n")
    _git_commit_all(tmp_path)
    _run(session_start.main, tmp_path, monkeypatch, source="startup")  # baselines HEAD

    state = gate.load_state(str(tmp_path))  # record the current content as VERIFIED
    state["scanned"]["u.c"] = gate.content_hash(str(src))
    gate.record(state, gate.UnitVerdict("u.c::f", "u.c", "f", "verified", 1))
    gate.save_state(str(tmp_path), state)

    src.write_text("int f(void){return 1;}\n")  # changed...
    _git_commit_all(tmp_path)  # ...and committed in one shot

    _run(stop_gate.main, tmp_path, monkeypatch)
    out = json.loads(capsys.readouterr().out)
    assert out.get("decision") == "block"
    assert "u.c" in out["reason"]


def test_stop_gate_allows_committed_unchanged_baselined_c(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A pre-existing dirty C file committed UNCHANGED must not be gated: the
    # content-hash baseline dedups it even though committed-since discovers it.
    _git_init(tmp_path)
    (tmp_path / "seed.txt").write_text("x")
    _git_commit_all(tmp_path)
    wip = tmp_path / "wip.c"
    wip.write_text("int w(void){return 0;}\n")  # dirty before the baseline
    _run(session_start.main, tmp_path, monkeypatch, source="startup")  # scanned + HEAD

    _git_commit_all(tmp_path)  # commit wip.c unchanged → clean worktree

    rc = _run(stop_gate.main, tmp_path, monkeypatch)
    assert rc == 0 and capsys.readouterr().out.strip() == ""  # not gated


def test_stop_gate_blocks_on_staged_blob_then_converges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The review's bypass end-to-end: a staged divergent blob with a reverted (clean)
    # worktree blocks; then re-staging the verified worktree content clears it — a
    # convergent block, not a dead-end loop.
    _git_init(tmp_path)
    src = tmp_path / "foo.c"
    src.write_text("int f(void){return 0;}\n")  # A
    _git_commit_all(tmp_path)
    state = gate.load_state(str(tmp_path))
    state["scanned"]["foo.c"] = gate.content_hash(str(src))
    gate.record(state, gate.UnitVerdict("foo.c::f", "foo.c", "f", "verified", 1))
    gate.save_state(str(tmp_path), state)

    src.write_text("int f(void){return 1;}\n")  # B
    _stage(tmp_path, "foo.c")  # index = B (unverified)
    src.write_text("int f(void){return 0;}\n")  # worktree back to A (hashes fresh)

    _run(stop_gate.main, tmp_path, monkeypatch)
    out = json.loads(capsys.readouterr().out)
    assert out.get("decision") == "block"
    assert "Staged in the index" in out["reason"] and "foo.c" in out["reason"]
    # The remediation must be index-shaped, not "edit the file" (which can't help).
    assert "git add" in out["reason"]

    _stage(tmp_path, "foo.c")  # re-stage the verified worktree A → index == scanned
    rc = _run(stop_gate.main, tmp_path, monkeypatch)
    assert rc == 0 and capsys.readouterr().out.strip() == ""  # now allowed, silent


def test_stop_gate_blocks_on_committed_blob_worktree_reverted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A divergent blob committed in one Bash command, then the worktree reverted so it
    # hashes clean: `HEAD` holds unverified C. The committed-since scan must block, and
    # — because the divergence lives in HEAD — its remediation is HEAD-shaped: `git
    # add` cannot clear it; re-verifying the committed content can.
    _git_init(tmp_path)
    src = tmp_path / "foo.c"
    src.write_text("int f(void){return 0;}\n")  # A
    _git_commit_all(tmp_path)
    _run(session_start.main, tmp_path, monkeypatch, source="startup")  # baselines HEAD
    state = gate.load_state(str(tmp_path))
    state["scanned"]["foo.c"] = gate.content_hash(str(src))
    gate.record(state, gate.UnitVerdict("foo.c::f", "foo.c", "f", "verified", 1))
    gate.save_state(str(tmp_path), state)

    src.write_text("int f(void){return 1;}\n")  # B
    _git_commit_all(tmp_path)  # HEAD now B
    src.write_text("int f(void){return 0;}\n")  # worktree reverted to A (hashes fresh)

    _run(stop_gate.main, tmp_path, monkeypatch)
    out = json.loads(capsys.readouterr().out)
    assert out.get("decision") == "block"
    assert "Committed since session start" in out["reason"] and "foo.c" in out["reason"]
    # The message must NOT steer the agent to a `git add` that leaves HEAD divergent.
    assert "cannot clear" in out["reason"]

    # `git add foo.c` (the index remediation) does NOT converge a committed
    # divergence — HEAD still holds B — so the turn keeps blocking.
    _stage(tmp_path, "foo.c")  # stage worktree A; HEAD is still B
    _run(stop_gate.main, tmp_path, monkeypatch)
    assert json.loads(capsys.readouterr().out).get("decision") == "block"

    # Re-verifying the committed content is what converges: bring B into the worktree,
    # reconcile the index to it, and let the gate stamp B verified — now the worktree,
    # index, and HEAD blob all equal the last-verified hash.
    src.write_text("int f(void){return 1;}\n")  # B in the worktree
    _stage(tmp_path, "foo.c")  # index = B (matches HEAD), no stale staged A left
    state = gate.load_state(str(tmp_path))
    state["scanned"]["foo.c"] = gate.content_hash(str(src))  # B verified
    gate.save_state(str(tmp_path), state)
    rc = _run(stop_gate.main, tmp_path, monkeypatch)
    assert rc == 0 and capsys.readouterr().out.strip() == ""  # now allowed, silent


def test_stop_gate_staged_blob_residual_after_max_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A persistent staged-blob divergence must degrade to a LOUD residual after the
    # attempt cap — never an infinite block, never a silent pass.
    _git_init(tmp_path)
    src = tmp_path / "foo.c"
    src.write_text("int f(void){return 0;}\n")
    _git_commit_all(tmp_path)
    state = gate.load_state(str(tmp_path))
    state["scanned"]["foo.c"] = gate.content_hash(str(src))
    state["stop_attempts"] = gate.MAX_STOP_ATTEMPTS  # next attempt exceeds the cap
    gate.save_state(str(tmp_path), state)

    src.write_text("int f(void){return 1;}\n")
    _stage(tmp_path, "foo.c")
    src.write_text("int f(void){return 0;}\n")

    _run(stop_gate.main, tmp_path, monkeypatch)
    out = json.loads(capsys.readouterr().out)
    assert "decision" not in out  # allowed to end...
    assert "Staged in the index" in out["systemMessage"]  # ...but with a LOUD residual


def test_stop_gate_non_git_allows_but_records_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "a.c").write_text("int a(void){return 0;}\n")  # no git repo here
    rc = _run(stop_gate.main, tmp_path, monkeypatch)
    assert rc == 0 and capsys.readouterr().out.strip() == ""
    skips = [
        e
        for e in event_log.read_events(tmp_path)
        if e.get("decision") == "oob_scan_skipped"
    ]
    assert skips  # degraded scope is traced, never a silent no-op


# --- post_bash hook ---------------------------------------------------------


def test_post_bash_non_git_is_traced_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "a.c").write_text("int a(void){return 0;}\n")
    assert _run(post_bash.main, tmp_path, monkeypatch) == 0
    events = event_log.read_events(tmp_path)
    assert any(e.get("decision") == "oob_scan_skipped" for e in events)


def test_post_bash_blocks_on_malformed_env_var_before_verifying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Issue #95 review: `_verify_file` reads `gate.DEFAULT_K`/`VERIFY_TIMEOUT_S`
    # (env_int/env_float-backed) via `verify_and_record` -- a malformed value
    # must block here, before this hook could verify and durably record a
    # verdict under the wrong value (the same fail-closed check
    # `stop_gate.main()` opens with).
    _git_init(tmp_path)
    (tmp_path / "a.c").write_text("int a(void){return 0;}\n")
    monkeypatch.setenv("FORSETI_UNWIND", "not-a-number")
    gate.env_int("FORSETI_UNWIND", "1")  # exercise the real parse path

    def _boom(*_a: object, **_kw: object) -> None:
        raise AssertionError("must not verify with a malformed env config")

    monkeypatch.setattr(gate, "verify_and_record", _boom)

    assert _run(post_bash.main, tmp_path, monkeypatch) == 2
    err = capsys.readouterr().err
    assert "FORSETI_UNWIND" in err
    assert "not-a-number" in err


def test_post_bash_skips_unchanged_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _git_init(tmp_path)
    src = tmp_path / "buf.c"
    src.write_text("int f(int *p){return *p;}\n")  # pointer → no ESBMC needed

    assert _run(post_bash.main, tmp_path, monkeypatch) == 0  # verifies + stamps
    state = gate.load_state(str(tmp_path))
    assert state["scanned"]["buf.c"] == gate.content_hash(str(src))

    # A later Bash call that changed nothing must NOT re-verify, or it would reset
    # the Stop-gate's patience and defeat MAX_STOP_ATTEMPTS termination.
    state["stop_attempts"] = 2
    gate.save_state(str(tmp_path), state)
    assert _run(post_bash.main, tmp_path, monkeypatch) == 0
    assert gate.load_state(str(tmp_path))["stop_attempts"] == 2  # untouched → skipped


_HAVE_ESBMC = shutil.which("esbmc") is not None and shutil.which("forseti") is not None


@pytest.mark.skipif(not _HAVE_ESBMC, reason="needs esbmc + forseti on PATH")
def test_post_bash_catches_out_of_band_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _git_init(tmp_path)
    src = tmp_path / "mix.c"  # my_abs overflows at INT64_MIN — VIOLATED
    src.write_text(
        "#include <stdint.h>\nint64_t my_abs(int64_t x) { return (x < 0) ? -x : x; }\n"
    )
    rc = _run(post_bash.main, tmp_path, monkeypatch)

    assert rc == 2  # counterexample fed back, exactly like the edit path
    assert "did not verify" in capsys.readouterr().err
    state = gate.load_state(str(tmp_path))
    assert state["units"]["mix.c::my_abs"]["verdict"] == "violated"
    assert state["scanned"]["mix.c"] == gate.content_hash(str(src))


@pytest.mark.skipif(not _HAVE_ESBMC, reason="needs esbmc + forseti on PATH")
def test_post_bash_passes_safe_out_of_band_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _git_init(tmp_path)
    src = tmp_path / "safe.c"
    src.write_text("#include <stdint.h>\nint64_t id(int64_t x) { return x; }\n")
    rc = _run(post_bash.main, tmp_path, monkeypatch)

    assert rc == 0
    assert "VERIFIED up to k" in capsys.readouterr().out
    assert (
        gate.load_state(str(tmp_path))["units"]["safe.c::id"]["verdict"] == "verified"
    )


# --- SessionStart baseline: scope to changes SINCE session start -------------


def test_baseline_scanned_seeds_dirty_tree(tmp_path: Path) -> None:
    _git_init(tmp_path)
    (tmp_path / "wip.c").write_text("int w(void){return 0;}\n")  # pre-existing, dirty
    n = gate.baseline_scanned(str(tmp_path))
    assert n == 1
    state = gate.load_state(str(tmp_path))
    assert state["scanned"]["wip.c"] == gate.content_hash(str(tmp_path / "wip.c"))


def test_baseline_scanned_non_git_is_none(tmp_path: Path) -> None:
    (tmp_path / "a.c").write_text("int a(void){return 0;}\n")
    assert gate.baseline_scanned(str(tmp_path)) is None


def test_preexisting_dirty_c_not_gated_after_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The failure the advisor flagged: without a baseline, a `git status` scan
    # gates C the user had dirty before the session and Claude never touched.
    _git_init(tmp_path)
    wip = tmp_path / "wip.c"
    wip.write_text("int w(void){return 0;}\n")  # user's pre-existing WIP

    _run(session_start.main, tmp_path, monkeypatch, source="startup")  # baseline

    # A pure conversational turn (Stop, no edits) must NOT block on the WIP file.
    rc = _run(stop_gate.main, tmp_path, monkeypatch)
    assert rc == 0 and capsys.readouterr().out.strip() == ""
    # And a Bash call must NOT verify the untouched WIP (no ESBMC needed to prove
    # it: post_bash skips it entirely because it is baselined fresh).
    assert _run(post_bash.main, tmp_path, monkeypatch) == 0
    assert "wip.c" not in gate.load_state(str(tmp_path)).get("units", {})


def test_changed_after_baseline_is_gated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _git_init(tmp_path)
    wip = tmp_path / "wip.c"
    wip.write_text("int w(void){return 0;}\n")
    _run(session_start.main, tmp_path, monkeypatch, source="startup")

    wip.write_text("int w(void){return 1;}\n")  # the agent changes it in-session
    _run(stop_gate.main, tmp_path, monkeypatch)
    out = json.loads(capsys.readouterr().out)
    assert out.get("decision") == "block"  # now it IS gated
    assert "wip.c" in out["reason"]


def test_session_start_resume_does_not_rebaseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # On resume the live scanned map must be preserved — a mid-session out-of-band
    # change must not be masked by a re-baseline.
    _git_init(tmp_path)
    src = tmp_path / "a.c"
    src.write_text("int a(void){return 0;}\n")
    state = gate.load_state(str(tmp_path))
    state["scanned"]["a.c"] = "STALE-ON-PURPOSE"  # pretend a mid-session verify
    gate.save_state(str(tmp_path), state)

    _run(session_start.main, tmp_path, monkeypatch, source="resume")
    # resume left it untouched (still marked stale) → the change stays gate-able
    assert gate.load_state(str(tmp_path))["scanned"]["a.c"] == "STALE-ON-PURPOSE"


# --- SessionStart baseline: the index/HEAD blobs too (issue #139) ------------


def _mm_repo(tmp_path: Path) -> tuple[Path, str, str]:
    """A repo opening at ``MM foo.c``: HEAD holds A, the index B, the worktree A.

    Returns the source path and the (A, B) content hashes.
    """
    _git_init(tmp_path)
    src = tmp_path / "foo.c"
    src.write_text("int f(void){return 0;}\n")  # A
    _git_commit_all(tmp_path)
    a_hash = gate.content_hash(str(src))
    src.write_text("int f(void){return 1;}\n")  # B — the user's staged WIP
    _stage(tmp_path, "foo.c")
    b_hash = gate.content_hash(str(src))
    src.write_text("int f(void){return 0;}\n")  # worktree reverted to A
    assert a_hash is not None and b_hash is not None
    return src, a_hash, b_hash


def test_baseline_scanned_records_index_and_head_blobs(tmp_path: Path) -> None:
    # The worktree hash alone misses the staged blob; both refs are baselined, and
    # they survive the JSON round-trip through gate_state.json (read back from disk,
    # the way the Stop-gate reads them).
    _src, a_hash, b_hash = _mm_repo(tmp_path)
    gate.baseline_scanned(str(tmp_path))

    state = gate.load_state(str(tmp_path))
    assert state["scanned"]["foo.c"] == a_hash  # worktree bytes
    assert state["baseline_blobs"]["foo.c"] == [b_hash, a_hash]  # index, then HEAD


def test_preexisting_staged_blob_not_gated_after_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Issue #139: a session opening at `MM foo.c` blocked immediately on the user's
    # pre-existing staged WIP. A pure conversational turn must end cleanly.
    _mm_repo(tmp_path)
    _run(session_start.main, tmp_path, monkeypatch, source="startup")

    rc = _run(stop_gate.main, tmp_path, monkeypatch)
    assert rc == 0 and capsys.readouterr().out.strip() == ""


def test_baseline_covers_staged_blob_with_deleted_worktree(tmp_path: Path) -> None:
    # `MD`: staged WIP whose worktree copy is gone. Discovery's existence filter
    # drops it from `scanned`, so the blob baseline is the only thing standing
    # between the user's staged WIP and an immediate block.
    src, _a_hash, b_hash = _mm_repo(tmp_path)
    src.unlink()  # worktree deleted — porcelain now reports `MD`

    gate.baseline_scanned(str(tmp_path))
    state = gate.load_state(str(tmp_path))
    assert "foo.c" not in state["scanned"]  # nothing on disk to hash
    assert b_hash in state["baseline_blobs"]["foo.c"]  # ...but the index blob is
    assert gate.divergent_blob_sources(str(tmp_path), state) == []


def test_agent_staged_blob_still_gated_after_baseline(tmp_path: Path) -> None:
    # Fail-closed: the baseline exempts the pre-session blob only. The agent staging
    # its OWN unverified bytes hashes to something no baseline holds, so it gates.
    src, _a_hash, _b_hash = _mm_repo(tmp_path)
    gate.baseline_scanned(str(tmp_path))

    src.write_text("int f(void){return 2;}\n")  # C — the agent's change, staged
    _stage(tmp_path, "foo.c")

    state = gate.load_state(str(tmp_path))
    assert gate.divergent_blob_sources(str(tmp_path), state) == [
        {"rel": "foo.c", "reason": "staged"}
    ]


def test_baseline_exempts_committing_preexisting_staged_wip(tmp_path: Path) -> None:
    # The staged baseline follows its blob into HEAD: committing the pre-session
    # index ships bytes the baseline already recorded, so the committed-since check
    # must not re-gate them under a different `reason`.
    _mm_repo(tmp_path)
    gate.baseline_scanned(str(tmp_path))
    state = gate.load_state(str(tmp_path))
    # commit the index as-is (not `git add -A`, which would restage the worktree)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "wip"], check=True)

    divergent = gate.divergent_blob_sources(
        str(tmp_path), state, baseline_head=state["baseline_head"]
    )
    assert divergent == []


def test_baseline_blobs_skips_untracked_path(tmp_path: Path) -> None:
    # An untracked file has neither an index nor a HEAD blob: it is baselined by its
    # worktree bytes only, and records no phantom blob exemption.
    _git_init(tmp_path)
    (tmp_path / "seed.txt").write_text("x")
    _git_commit_all(tmp_path)
    new = tmp_path / "new.c"
    new.write_text("int g(void){return 0;}\n")

    gate.baseline_scanned(str(tmp_path))
    state = gate.load_state(str(tmp_path))
    assert state["scanned"]["new.c"] == gate.content_hash(str(new))
    assert "new.c" not in state["baseline_blobs"]


def test_divergent_blob_sources_gates_on_malformed_baseline(tmp_path: Path) -> None:
    # A truncated/hand-edited baseline must not grant an exemption: an unusable
    # entry reads as "nothing baselined" and the staged blob still blocks.
    _mm_repo(tmp_path)
    gate.baseline_scanned(str(tmp_path))
    state = gate.load_state(str(tmp_path))
    state["baseline_blobs"]["foo.c"] = "b7e2"  # not a list of hashes

    assert gate.divergent_blob_sources(str(tmp_path), state) == [
        {"rel": "foo.c", "reason": "staged"}
    ]


def test_prune_deleted_units_clears_blob_baseline(tmp_path: Path) -> None:
    # Both halves of the baseline have the same lifetime: a file removed
    # out-of-band drops its worktree hash AND its blob hashes, so a same-name file
    # recreated later cannot inherit a stale exemption.
    src, _a_hash, _b_hash = _mm_repo(tmp_path)
    gate.baseline_scanned(str(tmp_path))
    state = gate.load_state(str(tmp_path))
    gate.record(state, gate.UnitVerdict("foo.c::f", "foo.c", "f", "verified", 1))
    src.unlink()

    assert gate.prune_deleted_units(state, str(tmp_path)) == ["foo.c::f"]
    assert "foo.c" not in state["scanned"]
    assert "foo.c" not in state["baseline_blobs"]

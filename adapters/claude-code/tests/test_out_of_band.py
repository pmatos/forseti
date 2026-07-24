"""Tests for out-of-band gating of Bash-written C files (issue #99).

Run from the repo root with the dev venv::

    .venv/bin/python -m pytest adapters/claude-code/tests -q

Discovery is `git status`-scoped, so the git-backed tests build a throwaway repo
under ``tmp_path``. The ESBMC-gated end-to-end tests skip without `esbmc` +
`forseti` on PATH; everything else runs pure-Python (pointer units are
NEEDS_CONTRACT and never shell out to ESBMC).
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import event_log
import forseti_gate as gate
import post_bash
import pytest
import session_start
import stop_gate


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
    hook_main, project_dir: Path, monkeypatch: pytest.MonkeyPatch, **payload
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


def test_parse_porcelain_z_handles_renames() -> None:
    # rename record: "R  new\0old"; the old-path token must be skipped
    out = "R  new.c\x00old.c\x00 M mod.c\x00?? fresh.c\x00A  added.c\x00"
    assert gate._parse_porcelain_z(out) == ["new.c", "mod.c", "fresh.c", "added.c"]


def test_content_hash_missing_file_is_none(tmp_path: Path) -> None:
    assert gate.content_hash(tmp_path / "nope.c") is None


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

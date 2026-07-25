"""Tests for out-of-band gating of Bash-written C files (issue #99).

Run from the repo root with the dev venv::

    .venv/bin/python -m pytest adapters/claude-code/tests -q

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


# --- killed-verify freshness (issue #140) -----------------------------------


class _Killed(RuntimeError):
    """Stand-in for a mid-verify hook kill: the run never reaches its final write."""


def _enumerate_one_unit(monkeypatch: pytest.MonkeyPatch, name: str = "f") -> None:
    """Enumerate one non-pointer unit without shelling out to `forseti list-units`."""
    monkeypatch.setattr(
        gate,
        "extract_function_defs",
        lambda file_path, *, project_dir: [gate.FuncDef(name, takes_pointer=False)],
    )


def _kill_during_verify(monkeypatch: pytest.MonkeyPatch, calls: list[str]) -> None:
    """Make every `verify_function` call die as if the hook were killed."""

    def _boom(file_path, function, *, project_dir, k=gate.DEFAULT_K):
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
        lambda fp, fn, *, project_dir, k=gate.DEFAULT_K: gate.UnitVerdict(
            "done.c::f", "done.c", "f", "verified", k
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
        lambda fp, fn, *, project_dir, k=gate.DEFAULT_K: gate.UnitVerdict(
            "hard.c::f", "hard.c", "f", "unknown", k, detail="timeout after 110s"
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
    # re-verify forever with the counter frozen (each error resets stop_attempts).
    # The error charges the marker one attempt instead — the killed run's units stay
    # retryable, and the retries still run out.
    src = tmp_path / "broken.c"
    src.write_text("int f(void){return 0;}\n")
    _enumerate_one_unit(monkeypatch)
    _kill_during_verify(monkeypatch, [])
    with pytest.raises(_Killed):
        gate.verify_and_record(str(src), project_dir=str(tmp_path))

    def _unavailable(file_path, *, project_dir):
        raise gate.UnitsUnavailable("forseti CLI could not be launched")

    monkeypatch.setattr(gate, "extract_function_defs", _unavailable)
    verdicts = gate.verify_and_record(str(src), project_dir=str(tmp_path))

    assert [v.verdict for v in verdicts] == ["error"]
    state = gate.load_state(str(tmp_path))
    assert state["pending"]["broken.c"]["attempts"] == 2  # charged, not frozen
    assert gate.blocking_units(state)  # the error itself keeps the turn blocked
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
        lambda fp, fn, *, project_dir, k=gate.DEFAULT_K: gate.UnitVerdict(
            "oob.c::f", "oob.c", "f", "verified", k
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

    def _verify_while_a_newer_run_starts(fp, fn, *, project_dir, k=gate.DEFAULT_K):
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

    def _verify_while_same_content_retries(fp, fn, *, project_dir, k=gate.DEFAULT_K):
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


def test_blocking_error_preserves_a_newer_runs_pending_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The error cleanup is scoped the same way: it touches only a marker recording
    # the bytes THIS run read. `raw` is read before the units are enumerated, so a
    # newer run's marker — a claim on content this error says nothing about — must
    # come through the error unchanged, counter included.
    src = tmp_path / "err.c"
    src.write_text("int f(void){return 0;}\n")
    newer: dict[str, str] = {}

    def _newer_run_starts_then_enumeration_fails(file_path, *, project_dir):
        newer["digest"] = _concurrent_run_starts(
            tmp_path, src, "int f(void){return 1;}\n"
        )
        raise gate.UnitsUnavailable("forseti CLI could not be launched")

    monkeypatch.setattr(
        gate, "extract_function_defs", _newer_run_starts_then_enumeration_fails
    )
    verdicts = gate.verify_and_record(str(src), project_dir=str(tmp_path))

    assert [v.verdict for v in verdicts] == ["error"]  # this scan still blocks
    state = gate.load_state(str(tmp_path))
    assert state["pending"]["err.c"] == {
        "hash": newer["digest"],
        "attempts": 1,
        "pid": os.getpid() + 1,
    }
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

    def _unavailable(file_path, *, project_dir):
        raise gate.UnitsUnavailable("forseti CLI could not be launched")

    monkeypatch.setattr(gate, "extract_function_defs", _unavailable)
    verdicts = gate.verify_and_record(str(src), project_dir=str(tmp_path))

    assert [v.verdict for v in verdicts] == ["error"]
    state = gate.load_state(str(tmp_path))
    assert state["pending"]["shared.c"] == {
        "hash": digest,
        "attempts": 2,  # charged...
        "pid": os.getpid() + 1,  # ...but still the creating run's marker
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

    def _unavailable(file_path, *, project_dir):
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


def _verified_verdict(rel: str, function: str = "f"):
    """Stand-in `verify_function` whose verdict lands (the retry that finishes)."""

    def _verify(fp, fn, *, project_dir, k=gate.DEFAULT_K):
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
    # Discovery joins the git root and the pending scan joins `project_dir`, so a
    # file both scans name arrives under two spellings — verifying it twice in one
    # scan would double the ESBMC cost and name it twice in the Stop note.
    src = tmp_path / "dup.c"
    src.write_text("int f(void){return 0;}\n")
    _enumerate_one_unit(monkeypatch)
    _kill_during_verify(monkeypatch, [])
    with pytest.raises(_Killed):
        gate.verify_and_record(str(src), project_dir=str(tmp_path))

    state = gate.load_state(str(tmp_path))
    discovered = [os.path.join(str(tmp_path), ".", "dup.c")]  # same file, other spell
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

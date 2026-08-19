"""Git plumbing for the Forseti verify-gate hooks.

The gate discovers what to verify by asking git what changed in a project's work
tree and parsing its ``--porcelain -z`` output. That is a self-contained unit —
it depends on nothing gate-specific, only ``subprocess``/``os``/``hashlib`` — so
it lives behind its own small interface here rather than in the 2.5k-line gate
module. Every function is best-effort: a missing git, a directory that is not a
work tree, or a non-zero exit surfaces as ``None`` (or an empty list), which the
caller reports as "out-of-band discovery is unavailable" — distinct from a clean
tree, never a silent skip.

``forseti_gate`` re-exports the names its own logic and the hook tests reach for,
so callers keep writing ``gate.git_head(...)`` etc.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from collections.abc import Iterator


def _git_probe_env() -> dict[str, str]:
    """Environment for a git call whose job is "does a work tree exist here at
    all", not "respect the caller's ambient discovery constraints".

    `GIT_CEILING_DIRECTORIES` stops git's *upward* repository discovery at a
    listed boundary — it does not mean no repository exists past it, and an
    ancestor repository's own `git add -A`, invoked directly from its own
    directory (which never needs discovery from `start_dir` at all), still
    stages `start_dir`'s contents normally: verified empirically that probing
    a subdirectory with the ceiling set at its parent repo produces the exact
    same `fatal: not a git repository (or any of the parent directories)`
    diagnostic as a genuine no-repository-anywhere answer, while `git add -A`
    run from the parent still stages a file placed in that subdirectory
    (review feedback, issue #151). Removing the variable here — rather than
    leaving each caller to inherit whatever the ambient environment
    happens to impose — makes the probe answer the only question that
    matters for deciding whether to protect a snapshot. `LC_ALL=C` keeps the
    stderr substring match immune to a translated git.
    """
    env = dict(os.environ)
    env.pop("GIT_CEILING_DIRECTORIES", None)
    env["LC_ALL"] = "C"
    return env


def _git(project_dir: str, *args: str, env: dict[str, str] | None = None) -> str | None:
    """Run a git subcommand in `project_dir`; its stdout, or ``None`` on failure.

    ``None`` covers git being absent, `project_dir` not being a work tree, or a
    non-zero exit — the caller treats all three as "out-of-band discovery is
    unavailable" (distinct from a clean tree), so it can report the degraded scope
    loudly instead of silently skipping.

    `env` defaults to inheriting the ambient environment unmodified, same as
    before this parameter existed. Pass `_git_probe_env()` for a call whose
    job is confirming whether a work tree genuinely exists — see that
    function for why the ambient environment isn't trusted there.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", project_dir, *args],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _git_bytes(project_dir: str, *args: str) -> bytes | None:
    """Like `_git`, but return raw stdout bytes (no text decoding), or ``None``.

    Used to hash a git blob (``git cat-file blob <ref>``): the hash must be taken
    over the exact bytes so it lines up with `content_hash`'s ``read_bytes`` digest —
    a text decode/re-encode (newline translation) would spuriously diverge.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", project_dir, *args],
            capture_output=True,
            timeout=30,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _iter_porcelain_z(out: str) -> Iterator[tuple[str, str]]:
    """Yield ``(XY status, path)`` for each ``git status --porcelain -z`` record.

    Each record is ``XY<space>path``; a rename/copy record is followed by a second
    NUL-separated field holding the *original* path, which we skip (the current
    path — the first field — is what we verify). ``-z`` means paths are never
    quoted, so no unescaping is needed.
    """
    tokens = out.split("\0")
    i = 0
    while i < len(tokens):
        entry = tokens[i]
        i += 1
        if not entry or len(entry) < 4:
            continue
        status, path = entry[:2], entry[3:]
        if "R" in status or "C" in status:
            i += 1  # the following token is the rename/copy source — skip it
        yield status, path


def _parse_porcelain_z(out: str) -> list[str]:
    """Relative paths from ``git status --porcelain -z`` (current path per record)."""
    return [path for _, path in _iter_porcelain_z(out)]


def _staged_paths_from_porcelain(out: str) -> list[str]:
    """Paths with a STAGED change: porcelain X-status neither clean (space) nor ``?``.

    An ``X`` of ``M``/``A``/``R``/``C``/… means the index holds a blob differing from
    ``HEAD`` (a `git add` of new or modified content) — the blob a plain `git commit`
    would ship. A clean ``X`` (space) is a worktree-only edit the porcelain scan
    already covers; ``?`` is untracked (no index entry).
    """
    return [path for status, path in _iter_porcelain_z(out) if status[0] not in " ?"]


def git_blob_hash(project_dir: str, ref: str) -> str | None:
    """SHA-256 of the git blob at `ref` (``:path`` = index, ``HEAD:path`` = commit).

    ``None`` when the ref does not resolve to a blob — a staged deletion, a rename's
    stale source, or a path absent from ``HEAD`` — so the caller treats "no blob
    there" as nothing to gate rather than a divergence.
    """
    out = _git_bytes(project_dir, "cat-file", "blob", ref)
    return hashlib.sha256(out).hexdigest() if out is not None else None


def staged_source_paths(project_dir: str) -> list[str]:
    """Repo-root-relative paths with a STAGED change in the index (best-effort).

    The blobs a plain `git commit` would ship. Empty when not a git work tree.
    """
    out = _git(project_dir, "status", "--porcelain", "-z", "-uall")
    return [] if out is None else _staged_paths_from_porcelain(out)


def git_changed_files(project_dir: str) -> list[str] | None:
    """Repo-root-relative paths `git status` reports as changed, or ``None``.

    ``--untracked-files=all`` so a brand-new Bash-written C file (untracked) is
    seen; git's own ignore rules already drop gitignored build/vendor output, so
    the scan never sweeps generated trees. Paths are relative to the repository
    root (git's porcelain contract), not `project_dir`.
    """
    out = _git(project_dir, "status", "--porcelain", "-z", "-uall")
    return None if out is None else _parse_porcelain_z(out)


def git_head(project_dir: str) -> str | None:
    """The current ``HEAD`` commit SHA, or ``None``.

    ``None`` when `project_dir` is not a work tree *or* the repo has no commits
    yet (`git rev-parse HEAD` fails) — the SessionStart baseline stores this, and a
    ``None`` baseline simply disables the committed-since scan (below).
    """
    out = _git(project_dir, "rev-parse", "HEAD")
    return out.strip() if out else None


def git_committed_files_since(project_dir: str, baseline_head: str | None) -> list[str]:
    """Repo-root-relative paths committed since `baseline_head` (best-effort).

    ``git status`` reports only working-tree/index state, so a Bash command that
    writes a C file *and* commits it in one shot (``cat > f.c && git commit ...``)
    leaves a clean tree the porcelain scan cannot see (issue #99 review). Diffing
    the session baseline HEAD against the current HEAD recovers those committed
    paths. Best-effort and purely additive: an empty list when there is no
    baseline, HEAD has not moved, or the diff fails (e.g. the baseline commit was
    rewritten) — the porcelain scan still covers uncommitted work either way.

    ``-z`` for unquoted NUL-separated paths; ``--name-only`` emits one entry per
    changed path (a rename's stale source, if listed, is dropped later by the
    existence filter), so no rename bookkeeping is needed here.
    """
    if not baseline_head:
        return []
    out = _git(project_dir, "diff", "--name-only", "-z", baseline_head, "HEAD")
    if out is None:
        return []
    return [p for p in out.split("\0") if p]

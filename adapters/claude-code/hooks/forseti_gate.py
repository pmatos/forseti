"""Shared logic for the Forseti Claude Code verify-gate hooks.

Forseti stays a stateless verdict oracle: this module shells out to the
``forseti verify`` CLI once per edited C function and records the resulting
verdict in a small per-project gate file (``.forseti/gate_state.json``). The
*gate* is what is stateful — the write→verify→fix loop is owned by the harness
(the PostToolUse + Stop hooks), never by Forseti.

Function-level, no harness: ESBMC is invoked with ``--function <name>`` so it
havocs the parameters and checks the built-in safety properties (memory safety,
signed overflow, array bounds, division by zero, UB). Semantic/functional
contracts — which *do* need an expressed harness — are the v1 property path, not
this gate.
"""

from __future__ import annotations

import contextlib
import fcntl
import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

# The safety-property profile. Bounds / pointer / div-by-zero are ESBMC defaults;
# signed overflow is opt-in, so we add it. Unsigned overflow is intentionally
# left OFF — wraparound is legal and common (hashes, counters) and enabling it
# yields false positives. Tune here; this is the one knob that defines "safe".
SAFETY_FLAGS: tuple[str, ...] = ("--overflow-check",)


class UnitsUnavailable(RuntimeError):
    """A file's function definitions could not be enumerated.

    Distinct from "the file defines no functions" (an empty list): a failed
    enumeration must surface as a blocking `error` verdict, never be mistaken for
    a clean pass (kill-safety — a not-verified unit can't silently slip through).
    Raised when `forseti list-units` cannot be run or its payload is unusable, and
    when the gate's own configuration is (`FORSETI_BUILD_FLAGS`).
    """


def _build_flags() -> tuple[str, ...]:
    """The project's build flags (`-I`, `-D`, ...) from ``FORSETI_BUILD_FLAGS``.

    Read per call, not once at import, so a hook process that sets the variable
    after this module loads still sees it. Split with `shlex` (stdlib — the hooks
    stay dependency-free) so a quoted path with spaces survives as one argument.

    Kept separate from `SAFETY_FLAGS` because the two have different destinations:
    build flags describe how the *translation unit* is constructed, so they must
    reach the `list-units` parse as well as the verify, while `--overflow-check`
    is a property-checking flag that means nothing to a `--parse-tree-only` run.
    Passing the wrong set to either is the failure this split exists to prevent —
    without its `-I` the enumeration fails outright (a blocking `error` on every
    edited file), and a missing `-D` silently changes *which* functions the file
    even defines, so the gate would verify a different unit list than the project
    builds.

    Unbalanced quoting raises `UnitsUnavailable` rather than escaping as a bare
    `ValueError`: quoting *is* this knob's interface (that is the whole reason for
    `shlex`), so a typo is the expected user error, and it has to land as the
    blocking verdict the gate is built around instead of a hook traceback.
    Degrading to no flags would be worse than either — it drops the `-I` and
    resurfaces as a baffling `PARSING ERROR` on a file that is perfectly fine.
    """
    raw = os.environ.get("FORSETI_BUILD_FLAGS", "")
    try:
        return tuple(shlex.split(raw))
    except ValueError as exc:
        raise UnitsUnavailable(
            f"FORSETI_BUILD_FLAGS is not parseable as a shell word list ({exc}): "
            f"{raw!r} — check for an unbalanced quote"
        ) from exc


# The default loop-unwind bound k. A VERIFIED is only ever "verified up to k".
# Override per project with FORSETI_UNWIND; functions with loops need a higher k
# (a k below the trip count can report a spurious verdict — roadmap Risk 1).
DEFAULT_K = int(os.environ.get("FORSETI_UNWIND", "1"))

# Per-function verify budget. Passed to `forseti verify --timeout` so ESBMC
# itself honors it — without it the Core CLI falls back to its 30s default and
# this knob is inert. The subprocess is bounded a little higher (below) so ESBMC
# self-terminates with UNKNOWN before the hard kill.
VERIFY_TIMEOUT_S = float(os.environ.get("FORSETI_VERIFY_TIMEOUT_S", "110"))
_SUBPROCESS_MARGIN_S = 15.0

# Budget for the one `forseti list-units` parse per edited file. A `--parse-tree-only`
# run does no solving, so it is fast; keep it well under the verify budget.
LIST_UNITS_TIMEOUT_S = float(os.environ.get("FORSETI_LIST_UNITS_TIMEOUT_S", "30"))

# How many times the Stop-gate blocks before it gives up and lets the turn end
# with a LOUD unverified residual (never a silent pass, but never an infinite
# loop either).
MAX_STOP_ATTEMPTS = 3

C_SUFFIXES = {".c", ".h"}

# Out-of-band discovery (issue #99): a C file written via the `Bash` tool
# (`cat > f.c`, a generator script, `sed -i`) never fires the Write/Edit/MultiEdit
# PostToolUse hook, so it is never recorded and the Stop-gate lets the turn end
# with unverified C. Discovery uses `git status` to find changed C, keyed on a
# content hash (no mtime `cp -p`/`tar` hole) against a `scanned` baseline that
# `baseline_scanned` seeds at session start — so the gate fires on C changed
# *during* the session, never on pre-existing dirty or committed/third-party C the
# agent never touched (the over-reach a whole-tree scan couldn't avoid). The
# baseline has two halves, because the gate reads C from two places: `scanned`
# holds each dirty file's *worktree* bytes, and `baseline_blobs` holds the *index*
# and `HEAD` blob hashes the blob scan (`divergent_blob_sources`) compares against
# — without the second half a session opening at `MM foo.c` (staged WIP, worktree
# reverted) would block on the user's own pre-session staged blob (issue #139).
# `FORSETI_GATE_INCLUDE`/`FORSETI_GATE_EXCLUDE` narrow it further; a bare path
# segment (`vendor`) prunes any directory of that name, a glob (`*_generated.c`,
# `test/*`) is matched against the project-relative path.
_DEFAULT_EXCLUDE_GLOBS: tuple[str, ...] = ("third_party", "vendor", "node_modules")

_STATE_DIR = ".forseti"
_STATE_FILE = "gate_state.json"
_LOCK_FILE = "gate_state.lock"

# Verdict string for a unit the gate declines to check at the function level: it
# takes a pointer/array parameter, so an unconstrained (havoc'd) caller makes the
# function-level memory-safety verdict meaningless — a *sound* but unactionable
# `dereference failure`. Rather than feed that phantom back as a fixable
# counterexample, the gate marks the unit NEEDS_CONTRACT: not verified, but
# non-blocking and loudly reported. A generated memory precondition/harness
# (issue #122, stage S2) is what will actually verify these. See RFC-0003.
NEEDS_CONTRACT = "needs_contract"
_NEEDS_CONTRACT_DETAIL = (
    "pointer/array parameter(s); function-level safety is unreliable without a "
    "memory precondition/harness — not gated (see issue #122)"
)


@dataclass(frozen=True)
class UnitVerdict:
    """One function's verdict from a single ``forseti verify`` call.

    ``argv`` (the exact ESBMC command line) and ``duration_s`` (wall-clock of the
    verify) come from the CLI's ``--json`` payload and are carried for the loop
    trace (``event_log``); they are ``None`` when the call never reached ESBMC
    (CLI missing, timeout, unparseable output).
    """

    unit_id: str  # "relpath::symbol"
    file: str
    function: str
    verdict: str  # verified | violated | unknown | error | needs_contract
    k: int
    counterexample: str | None = None
    detail: str | None = None
    argv: tuple[str, ...] | None = None
    duration_s: float | None = None

    @property
    def passed(self) -> bool:
        return self.verdict == "verified"


def resolve_forseti_cmd() -> list[str]:
    """The command prefix for the Forseti CLI: the installed script, else the module."""
    found = shutil.which("forseti")
    if found:
        return [found]
    return [sys.executable, "-m", "forseti.core"]


def is_c_source(path: str | os.PathLike[str]) -> bool:
    return Path(path).suffix.lower() in C_SUFFIXES


@dataclass(frozen=True)
class FuncDef:
    """A top-level function definition the gate found.

    `takes_pointer` is true when any parameter is a pointer or array — the case
    the function-level gate cannot verify without a materialized backing object
    (`NEEDS_CONTRACT`). It is read from `forseti list-units`' canonical,
    typedef-resolved parameter types, so a pointer hidden behind a typedef counts.
    """

    name: str
    takes_pointer: bool


def _func_def(unit: object) -> FuncDef:
    """One `units` entry → `FuncDef`, with both fields type-checked, never coerced.

    `bool()` on a non-boolean silently *inverts* the classification: the JSON
    string `"false"` is truthy, so a scalar function reported by an incompatible
    `forseti` build would be parked in non-blocking `NEEDS_CONTRACT` and never
    verified — the edited file then passes the gate unchecked. A wrong type is an
    unusable payload, not a value with a sensible default, so it blocks. A
    *missing* `takes_pointer` blocks for the same reason (it used to default to
    "scalar"), mirroring how an absent `units` key is treated.
    """
    if not isinstance(unit, dict):
        raise UnitsUnavailable(
            f"malformed list-units payload: units entry is not an object: {unit!r}"
        )
    name = unit.get("function")
    if not isinstance(name, str) or not name:
        raise UnitsUnavailable(
            "malformed list-units payload: units entry has no `function` name: "
            f"{unit!r}"
        )
    takes_pointer = unit.get("takes_pointer")
    if not isinstance(takes_pointer, bool):
        raise UnitsUnavailable(
            f"malformed list-units payload: `takes_pointer` for {name!r} is not a "
            f"JSON boolean: {takes_pointer!r}"
        )
    return FuncDef(name, takes_pointer)


@contextlib.contextmanager
def _enumerable_source(
    file_path: str | os.PathLike[str], content: bytes | None, *, project_dir: str
) -> Iterator[tuple[str, list[str]]]:
    """Yield ``(path to enumerate, extra esbmc flags)`` for `file_path`.

    With `content` ``None`` the file is enumerated where it lies. Given `content`,
    those exact bytes are written to a snapshot in a private temp directory and
    *that* is what gets parsed — so the enumeration is of the caller's bytes by
    construction, not of whatever the file happens to hold when the CLI re-reads
    it (issue #141). Write the bytes; never `shutil.copy` or re-read `file_path`,
    or the race walks straight back in.

    A snapshot is not where the source lives, and clang searches the *including
    file's own* directory first for a quoted ``#include "sibling.h"`` — so a
    header that needed no ``-I`` in place would go missing, turning every such
    `.c` into a blocking `error`. The original's directory is handed back as an
    ``-I`` to stand in for it. The snapshot keeps the original basename so a
    parse error names something recognisable (and so `parse_units`' path match is
    unaffected).

    That directory is derived **lexically** — absolutized against `project_dir`
    (the subprocess's cwd) but never `resolve()`d. For a symlinked source, clang
    searches the directory of the path it was *given*, so resolving would name the
    link target's directory instead: a header beside the link would go missing,
    and a same-named header beside the target would be silently preferred —
    enumerating units the in-place parse never sees.
    """
    if content is None:
        yield str(file_path), []
        return
    target = os.path.abspath(os.path.join(project_dir, os.fspath(file_path)))
    with tempfile.TemporaryDirectory(prefix="forseti-units-") as tmp:
        snapshot = Path(tmp) / os.path.basename(target)
        snapshot.write_bytes(content)
        yield str(snapshot), [f"-I{os.path.dirname(target)}"]


def extract_function_defs(
    file_path: str | os.PathLike[str],
    *,
    project_dir: str,
    content: bytes | None = None,
) -> list[FuncDef]:
    """Enumerate `file_path`'s function definitions via ``forseti list-units``.

    Shells out to the same authoritative clang-based frontend that *verifies* the
    unit (``forseti list-units --json``) rather than a regex, so typedef'd
    pointers, K&R and multi-line signatures, function-like macros, ``#if`` blocks,
    and a ``*`` inside a comment are all classified correctly (issue #131). The
    hook stays dependency-free — it only spawns the CLI, never imports the
    package. Raises `UnitsUnavailable` when the CLI cannot be run or the parse
    fails (missing binary, C parse error, timeout, unreadable file), so the caller
    records a blocking `error` verdict instead of silently skipping the file.

    Pass `content` to enumerate exactly those bytes rather than re-reading the
    file (`_enumerable_source`): the caller has already hashed them, and a
    concurrent rewrite between the hash and the CLI's read is what issue #141
    closes. Omit it to parse the file in place.

    Only ``.c`` translation units are enumerated: ESBMC cannot parse a header
    standalone (``forseti verify`` errors on a ``.h`` too — "failed to figure out
    type of file"), and clang's path-match keeps a header-resident definition out
    of its includer's unit list, so functions defined in a ``.h`` are simply out
    of gate scope. A non-``.c`` file yields ``[]`` (a clean pass) rather than an
    unresolvable block.
    """
    if Path(file_path).suffix.lower() != ".c":
        return []
    with _enumerable_source(file_path, content, project_dir=project_dir) as (
        source,
        include_flags,
    ):
        return _list_units(source, include_flags, project_dir=project_dir)


def _list_units(
    source: str, include_flags: list[str], *, project_dir: str
) -> list[FuncDef]:
    """Run ``forseti list-units`` on `source` and map its payload to `FuncDef`s."""
    argv = [
        *resolve_forseti_cmd(),
        "list-units",
        source,
        "--json",
        # Pass the float through, don't truncate. `list-units` hands its --timeout
        # straight to `subprocess.run(timeout=...)` (esbmc's own --timeout is not
        # used on the parse-tree-only path), where `0` expires immediately rather
        # than meaning "unbounded" — so `int(0.5)` would turn a half-second budget
        # into a blocking `error` on every edited `.c`. `str()` round-trips exactly;
        # argparse reparses it as the float the CLI declares.
        "--timeout",
        str(LIST_UNITS_TIMEOUT_S),
    ]
    # The parse needs the project's own `-I`/`-D` — a translation unit whose
    # `#include` cannot be resolved makes esbmc exit nonzero, which is a blocking
    # `error` on every edited file rather than a listing. Only the build flags go
    # here: `SAFETY_FLAGS` is for the verify, and a property flag on a
    # `--parse-tree-only` run is at best inert. `include_flags` rides the same
    # passthrough and goes *first*: it stands in for the search directory the
    # source would have had in place, which clang consults ahead of any `-I`.
    esbmc_flags = [*include_flags, *_build_flags()]
    if esbmc_flags:
        argv += ["--", *esbmc_flags]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=LIST_UNITS_TIMEOUT_S + _SUBPROCESS_MARGIN_S,
            cwd=project_dir,
        )
    except OSError as exc:  # missing CLI, etc. — resolve_forseti_cmd falls back to -m
        raise UnitsUnavailable(
            "forseti CLI could not be launched; install the forseti package "
            f"(pip install -e .) so `forseti` is on PATH: {exc}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise UnitsUnavailable(
            f"list-units exceeded {LIST_UNITS_TIMEOUT_S:g}s (raise "
            "FORSETI_LIST_UNITS_TIMEOUT_S)"
        ) from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()[:800] or f"exit {proc.returncode}"
        raise UnitsUnavailable(detail)
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise UnitsUnavailable(
            (proc.stderr or proc.stdout).strip()[:800] or "no JSON output"
        ) from exc
    # Require an explicit list-valued `units`. Defaulting a missing key to `[]`
    # would let an older/incompatible `forseti` build — one that exits 0 with a
    # JSON object that has no `units` member — read as "this file defines no
    # functions" (a clean pass), so an edited `.c` slips through unverified. An
    # *empty* list is still a legitimate no-functions pass; only absence blocks.
    if not isinstance(payload, dict) or not isinstance(payload.get("units"), list):
        raise UnitsUnavailable(
            "list-units payload has no list-valued `units` key (incompatible "
            f"`forseti` build?): {proc.stdout.strip()[:800] or '<empty>'}"
        )
    return [_func_def(u) for u in payload["units"]]


def extract_functions(
    file_path: str | os.PathLike[str], *, project_dir: str
) -> list[str]:
    """Names of `file_path`'s top-level functions (via ``forseti list-units``)."""
    return [d.name for d in extract_function_defs(file_path, project_dir=project_dir)]


def unit_id(project_dir: str, file_path: str) -> str:
    try:
        return os.path.relpath(file_path, project_dir)
    except ValueError:
        return file_path


def content_hash(path: str | os.PathLike[str]) -> str | None:
    """SHA-256 of a file's bytes, or ``None`` if it cannot be read.

    The freshness key for the out-of-band scan: a file is re-verified only when
    its content hash differs from the one recorded at its last verify. Content —
    not mtime — so a `cp -p`/`tar` that preserves an old timestamp cannot sneak an
    unverified change past the gate, and (load-bearing) an *unchanged* file is
    never re-verified, which is what keeps the Stop-gate's `stop_attempts` counter
    from being reset every turn.
    """
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def _globs(value: str | None) -> tuple[str, ...]:
    """Split a ``:``/``,``-separated include/exclude setting into patterns."""
    if not value:
        return ()
    return tuple(p.strip() for p in re.split(r"[:,]", value) if p.strip())


def _matches(rel: str, patterns: Iterable[str]) -> bool:
    """True if project-relative path `rel` matches any include/exclude `patterns`.

    A pattern with a glob metacharacter or a ``/`` is matched against the whole
    relative path (``fnmatch``); a bare name (``vendor``) matches when it is any
    path segment, so it prunes a directory of that name at any depth.
    """
    parts = set(PurePosixPath(rel).parts)
    for pat in patterns:
        if "/" in pat or any(ch in pat for ch in "*?["):
            if fnmatch.fnmatch(rel, pat):
                return True
        elif pat in parts:
            return True
    return False


def _included(rel: str) -> bool:
    """Apply the `FORSETI_GATE_INCLUDE`/`FORSETI_GATE_EXCLUDE` globs to `rel`.

    Exclude wins over include. When `FORSETI_GATE_EXCLUDE` is unset the built-in
    `_DEFAULT_EXCLUDE_GLOBS` apply; setting it replaces (not extends) them.
    """
    exclude = _globs(os.environ.get("FORSETI_GATE_EXCLUDE")) or _DEFAULT_EXCLUDE_GLOBS
    if _matches(rel, exclude):
        return False
    include = _globs(os.environ.get("FORSETI_GATE_INCLUDE"))
    return not include or _matches(rel, include)


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


def _git(project_dir: str, *args: str) -> str | None:
    """Run a git subcommand in `project_dir`; its stdout, or ``None`` on failure.

    ``None`` covers git being absent, `project_dir` not being a work tree, or a
    non-zero exit — the caller treats all three as "out-of-band discovery is
    unavailable" (distinct from a clean tree), so it can report the degraded scope
    loudly instead of silently skipping.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", project_dir, *args],
            capture_output=True,
            text=True,
            timeout=30,
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


def _in_scope_c_abspath(
    project_dir: str, root: str, proj_real: str, rel: str
) -> str | None:
    """Absolute path for repo-root-relative `rel` if it is an in-scope, included C
    source under `project_dir`, else ``None``.

    Applies the C-suffix, project-subtree, and include/exclude-glob filters shared by
    the worktree scan (`discover_changed_c_sources`) and the staged/committed-blob
    scan (`divergent_blob_sources`). Deliberately does **not** check file existence —
    a staged or committed blob can outlive its worktree file (a `git add`-then-`rm`),
    and the blob scan must still gate it.
    """
    abspath = os.path.join(root, rel)
    if not is_c_source(abspath):
        return None
    try:
        if os.path.commonpath([proj_real, os.path.realpath(abspath)]) != proj_real:
            return None  # changed outside this project subtree — out of scope
    except ValueError:
        return None  # different drive/root — cannot be under proj
    if not _included(os.path.relpath(abspath, project_dir)):
        return None
    return abspath


def discover_changed_c_sources(
    project_dir: str, *, baseline_head: str | None = None
) -> list[str] | None:
    """Absolute paths of changed, still-present C sources under `project_dir`.

    git reports paths relative to the *repository root*, which need not be
    `project_dir`, so they are joined to the resolved root and then scoped back to
    `project_dir` (a subdir checkout gates only its own changes). Include/exclude
    globs are applied to the project-relative path, matching `unit_id`. ``None``
    when discovery is unavailable (not a git repo).

    Discovery is the union of the working-tree/index changes (``git status``) and
    C committed since the session `baseline_head` (issue #99 review): a Bash
    command that writes and commits a C file in one shot leaves a clean tree the
    porcelain scan alone would miss. The union is deduped and content-hash
    freshness (`stale_sources`) remains the real gate, so a file committed
    *unchanged* since the baseline is filtered back out. A ``None`` baseline (no
    commits yet, or SessionStart never seeded one) degrades cleanly to the
    porcelain-only scan.
    """
    root = _git(project_dir, "rev-parse", "--show-toplevel")
    rels = git_changed_files(project_dir)
    if root is None or rels is None:
        return None
    root = root.strip()
    # Both git subcommands emit repo-root-relative paths, so the committed-since
    # set unions straight into `rels` before the shared join/scope/filter below.
    # dict.fromkeys dedups while preserving order (a path both dirty and committed
    # is verified once).
    committed = git_committed_files_since(project_dir, baseline_head)
    rels = list(dict.fromkeys([*rels, *committed]))
    # Keep the returned path expressed relative to the raw `project_dir` so its
    # `unit_id`/`scanned` key matches what `verify_and_record` stamps; realpath is
    # used only to compare against the (possibly symlinked) project subtree.
    proj_real = os.path.realpath(project_dir)
    found: list[str] = []
    for rel in rels:
        abspath = _in_scope_c_abspath(project_dir, root, proj_real, rel)
        # A path git reports changed but that is gone from disk (a Bash `rm`) is
        # skipped here — there is nothing to *verify*. Its recorded units are
        # reconciled separately by `prune_deleted_units`, which keys off actual
        # file existence so it also catches an untracked file git never tracked
        # (issue #99 review): keep discovery about "what to verify", pruning about
        # "what no longer exists". A staged/committed blob that outlives its worktree
        # file is gated instead by `divergent_blob_sources`.
        if abspath is not None and os.path.isfile(abspath):
            found.append(abspath)
    return found


def stale_sources(project_dir: str, state: dict, files: Iterable[str]) -> list[str]:
    """Subset of `files` whose content differs from the last recorded verify.

    A file is stale when it has never been verified (`scanned` has no entry) or
    its current content hash differs from the recorded one — i.e. it was written
    or modified out-of-band since the gate last saw it.
    """
    scanned = state.get("scanned", {})
    stale: list[str] = []
    for abspath in files:
        digest = content_hash(abspath)
        if digest is None:
            continue
        if scanned.get(unit_id(project_dir, abspath)) != digest:
            stale.append(abspath)
    return stale


def _baselined_blobs(baseline: object, rel: str) -> tuple[str, ...]:
    """The session-start blob hashes recorded for `rel` — empty if unusable.

    Reads the `baseline_blobs` half of the SessionStart baseline defensively: a
    hand-edited or truncated `gate_state.json` yields ``()``, which *gates* the blob
    rather than exempting it. The unusable-input direction has to be the blocking
    one here — an exemption is the only thing this map can grant.
    """
    if not isinstance(baseline, dict):
        return ()
    entry = baseline.get(rel)
    if not isinstance(entry, list):
        return ()
    return tuple(h for h in entry if isinstance(h, str))


def divergent_blob_sources(
    project_dir: str, state: dict, *, baseline_head: str | None = None
) -> list[dict]:
    """C whose STAGED or COMMITTED blob was never verified (issue #99 review).

    `stale_sources` hashes only the worktree copy, so a Bash command that stages one
    C blob and then reverts the worktree — ``git status`` reports ``MM`` yet the
    worktree hashes as the last-verified content — could commit the staged blob
    unverified; likewise a divergent blob committed while the worktree is reverted
    leaves ``HEAD`` holding C the gate never saw, still worktree-fresh. This scans the
    index (staged) and the committed-since-baseline (``HEAD``) blobs **directly** —
    not `discover_changed_c_sources`, whose existence filter would drop a
    staged/committed-then-``rm``ed path (its blob still ships) — and reports each blob
    whose SHA-256 differs from the recorded `scanned` (last-verified) hash.

    Keyed by content, so a blob equal to verified content is deduped straight out (no
    over-gating a `git add`/commit of already-verified C). A blob equal to one the
    SessionStart baseline recorded (`baseline_blobs` — the index/``HEAD`` blobs of the
    pre-session dirty tree) dedups out the same way, and stays exempt for the whole
    session: it is the user's own WIP, unchanged since session start, which is exactly
    what the baseline exists to keep out of scope (issue #139). The agent staging or
    committing *different* bytes yields a hash no baseline holds, so it still gates.
    Only the staged set (porcelain X-status) and the committed-since set are consulted,
    never every tracked file, so a merely worktree-edited-but-uncommitted unit is *not*
    dragged in by a stale ``HEAD``. Degrades to empty when not a git repo. Each entry is
    ``{"rel": <project-relative path>, "reason": "staged" | "committed"}``.
    """
    root = _git(project_dir, "rev-parse", "--show-toplevel")
    if root is None:
        return []
    root = root.strip()
    proj_real = os.path.realpath(project_dir)
    scanned = state.get("scanned", {})
    baseline = state.get("baseline_blobs", {})
    # committed first: an already-shipped divergence is the graver of the two, and
    # dedup keeps a single (rel, reason) entry per path.
    candidates = [
        (rel, f"HEAD:{rel}", "committed")
        for rel in git_committed_files_since(project_dir, baseline_head)
    ]
    candidates += [
        (rel, f":{rel}", "staged") for rel in staged_source_paths(project_dir)
    ]

    results: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for repo_rel, ref, reason in candidates:
        abspath = _in_scope_c_abspath(project_dir, root, proj_real, repo_rel)
        if abspath is None:
            continue
        rel = unit_id(project_dir, abspath)
        key = (rel, reason)
        if key in seen:
            continue
        digest = git_blob_hash(project_dir, ref)
        if digest is None:
            continue  # no blob at that ref (staged deletion / rename source) — skip
        if digest == scanned.get(rel) or digest in _baselined_blobs(baseline, rel):
            continue  # already-verified content, or the pre-session baseline
        seen.add(key)
        results.append({"rel": rel, "reason": reason})
    return results


def baseline_blob_hashes(project_dir: str) -> dict[str, list[str]] | None:
    """Session-start hashes of the INDEX and ``HEAD`` blobs of the dirty C tree.

    The blob half of the SessionStart baseline (issue #139), companion to the
    worktree hashes `baseline_scanned` seeds into `scanned`. `divergent_blob_sources`
    compares the index (``:path``) and ``HEAD`` (``HEAD:path``) blobs, which the
    worktree bytes need not equal: a repo opening at ``MM foo.c`` (or ``MD`` — staged,
    worktree deleted) holds a staged blob that `content_hash` never sees, so the Stop
    check would fire on the user's pre-existing WIP the moment the session began.

    Enumerated from `git_changed_files` — every porcelain-reported path — rather than
    `discover_changed_c_sources`, whose existence filter drops exactly the
    staged-then-deleted case this fixes. Paths run through the same
    `_in_scope_c_abspath` filter (and are keyed by the same `unit_id`) as the consumer,
    so the two sets cannot drift apart into a hole or a permanent over-gate. Refs that
    hold no blob are skipped, so an untracked path (neither ref resolves) records
    nothing and a pre-session rename records just its index blob. ``None`` when not a
    git work tree. Costs two ``git cat-file`` calls per dirty C path, once per session.
    """
    root = _git(project_dir, "rev-parse", "--show-toplevel")
    rels = git_changed_files(project_dir)
    if root is None or rels is None:
        return None
    root = root.strip()
    proj_real = os.path.realpath(project_dir)
    baseline: dict[str, list[str]] = {}
    for repo_rel in dict.fromkeys(rels):
        abspath = _in_scope_c_abspath(project_dir, root, proj_real, repo_rel)
        if abspath is None:
            continue
        digests: list[str] = []
        for ref in (f":{repo_rel}", f"HEAD:{repo_rel}"):
            digest = git_blob_hash(project_dir, ref)
            if digest is not None and digest not in digests:
                digests.append(digest)
        if digests:
            baseline[unit_id(project_dir, abspath)] = digests
    return baseline


def baseline_scanned(project_dir: str) -> int | None:
    """Mark the currently-dirty C tree as "seen" at session start (issue #99).

    The out-of-band scan gates C whose content differs from the recorded
    `scanned` hash. Without a baseline that is *everything* `git status` reports —
    including C that was dirty before the session and the agent never touched, the
    over-reach this issue was careful to avoid. Seeding `scanned` with each
    pre-session dirty file's current hash scopes the gate to "changed **since
    session start**": those files are gated only once the agent actually modifies
    them. The baseline HEAD is recorded too, so the committed-since scan can later
    catch C a Bash command writes *and* commits in one shot (issue #99 review).

    The worktree bytes are only half of it: the blob scan reads the index and ``HEAD``,
    so `baseline_blob_hashes` seeds those hashes into `baseline_blobs` in the same
    write (issue #139) — otherwise a session opening at ``MM``/``MD foo.c`` blocks
    immediately on the user's pre-existing staged blob, which `content_hash` never saw.
    Returns the number of files whose worktree content was baselined (the `scanned`
    entries), or ``None`` if not a git repo.
    """
    discovered = discover_changed_c_sources(project_dir)
    if discovered is None:
        return None
    # Both resolve outside the lock — they spawn git subprocesses. A `None` blob
    # baseline (the work tree vanished between the two calls) records nothing, which
    # over-gates pre-session staged WIP rather than exempting a blob we never hashed.
    head = git_head(project_dir)
    blobs = baseline_blob_hashes(project_dir) or {}
    with gate_lock(project_dir):
        state = load_state(project_dir)
        baseline: dict[str, str] = {}
        for abspath in discovered:
            digest = content_hash(abspath)
            if digest is not None:
                baseline[unit_id(project_dir, abspath)] = digest
        state["scanned"] = baseline
        state["baseline_blobs"] = blobs
        state["baseline_head"] = head
        save_state(project_dir, state)
    return len(baseline)


def verify_function(
    file_path: str, function: str, *, project_dir: str, k: int = DEFAULT_K
) -> UnitVerdict:
    """Run ``forseti verify`` on one function and map its JSON payload to a verdict."""
    rel = unit_id(project_dir, file_path)
    uid = f"{rel}::{function}"
    try:
        # Same build flags the enumeration parsed with, so the verify sees the
        # same translation unit the unit list was taken from. Unparseable config
        # becomes this unit's `error` verdict rather than a hook traceback — a
        # direct caller (not coming through `verify_and_record`, which fails at
        # enumeration first) must still get a verdict back.
        build_flags = _build_flags()
    except UnitsUnavailable as exc:
        return UnitVerdict(uid, rel, function, "error", k, detail=str(exc))
    argv = [
        *resolve_forseti_cmd(),
        "verify",
        file_path,
        "--function",
        function,
        "--unwind",
        str(k),
        "--timeout",
        str(int(VERIFY_TIMEOUT_S)),
        "--json",
        "--",
        *SAFETY_FLAGS,
        *build_flags,
    ]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=VERIFY_TIMEOUT_S + _SUBPROCESS_MARGIN_S,
            cwd=project_dir,
        )
    except FileNotFoundError:
        return UnitVerdict(
            uid,
            rel,
            function,
            "error",
            k,
            detail="forseti CLI not found; install the forseti package "
            "(pip install -e .) so `forseti` is on PATH",
        )
    except subprocess.TimeoutExpired:
        return UnitVerdict(
            uid,
            rel,
            function,
            "unknown",
            k,
            detail=f"verify exceeded {VERIFY_TIMEOUT_S:g}s (raise "
            "FORSETI_VERIFY_TIMEOUT_S, raise k, or simplify the unit)",
        )

    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return UnitVerdict(
            uid,
            rel,
            function,
            "error",
            k,
            detail=(proc.stderr or proc.stdout).strip()[:800] or "no output",
        )

    verdict = str(payload.get("verdict", "error"))
    raw_argv = payload.get("argv")
    return UnitVerdict(
        uid,
        rel,
        function,
        verdict,
        int(payload.get("unwind", k)),
        counterexample=payload.get("counterexample"),
        detail=payload.get("reason") or payload.get("message"),
        argv=tuple(raw_argv) if isinstance(raw_argv, list) else None,
        duration_s=payload.get("duration_s"),
    )


def _gate_path(project_dir: str) -> Path:
    return Path(project_dir) / _STATE_DIR / _STATE_FILE


@contextlib.contextmanager
def gate_lock(project_dir: str) -> Iterator[None]:
    """Serialize gate-state read-modify-write across concurrent hook processes.

    Parallel PostToolUse hooks (one per edited file in a batch) each do
    load_state → mutate → save_state; without a lock the last writer wins and a
    concurrently-recorded `violated` unit can be dropped, letting the Stop-gate
    pass silently. An exclusive advisory lock on a sidecar file makes the whole
    sequence atomic between processes. POSIX-only — the platform ESBMC and this
    gate target.
    """
    path = Path(project_dir) / _STATE_DIR / _LOCK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def load_state(project_dir: str) -> dict:
    path = _gate_path(project_dir)
    if path.exists():
        try:
            state = json.loads(path.read_text())
            state.setdefault("units", {})
            state.setdefault("stop_attempts", 0)
            state.setdefault("scanned", {})
            state.setdefault("baseline_blobs", {})
            state.setdefault("baseline_head", None)
            return state
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "units": {},
        "stop_attempts": 0,
        "scanned": {},
        "baseline_blobs": {},
        "baseline_head": None,
    }


def save_state(project_dir: str, state: dict) -> None:
    # Write atomically (temp + os.replace) so a hook killed mid-write can never
    # leave a truncated gate_state.json — load_state fails open to an empty unit
    # set, which would make the Stop-gate forget outstanding violations.
    path = _gate_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    os.replace(tmp, path)


def record(state: dict, verdict: UnitVerdict) -> None:
    state["units"][verdict.unit_id] = asdict(verdict)


def prune_missing_units(
    state: dict, project_dir: str, file_path: str, keep: set[str]
) -> None:
    """Drop tracked units for `file_path` whose function is not in `keep`.

    `record` only ever upserts, so a function renamed or removed as part of a fix
    would leave its stale (often `violated`) entry behind and the Stop-gate would
    block forever on a unit that no longer exists. Reconciling against the set of
    functions the file *still* defines (`keep`) — at the end of a run rather than
    by blanket-pruning up front — clears those without a mid-run hook kill being
    able to drop a still-unverified violation.
    """
    prefix = f"{unit_id(project_dir, file_path)}::"
    stale = [
        u
        for u in state["units"]
        if u.startswith(prefix) and u[len(prefix) :] not in keep
    ]
    for uid in stale:
        del state["units"][uid]


def prune_deleted_units(state: dict, project_dir: str) -> list[str]:
    """Drop recorded units whose backing C source no longer exists on disk.

    `record`/`verify_and_record` only ever upsert per file, so a C source removed
    out-of-band — `rm f.c` via Bash, whether it was committed or written earlier
    this session — leaves its `violated`/`unknown` units in the gate state and the
    Stop-gate would block forever (then only emit a residual after the attempt cap)
    on a unit whose file is gone. Discovery correctly skips a missing file *for
    verification*; this reconciles the recorded side, dropping units whose file is
    absent and clearing each such file's `scanned` **and** `baseline_blobs` baseline
    (so a same-name file recreated later re-verifies from scratch — the two baselines
    are cleared together, or content matching a dropped one could still slip past the
    blob scan).

    Keys off each unit's recorded `file` (project-relative), not `git status`, so
    it also catches an untracked Bash-written file git never knew existed — the
    case a git-scoped deletion scan would miss. Only ever *removes* already-recorded
    units (files the agent touched), so it can never over-reach into gating C the
    agent left alone. Returns the pruned unit ids.
    """
    units = state.get("units", {})
    scanned = state.get("scanned", {})
    baseline_blobs = state.get("baseline_blobs", {})
    pruned: list[str] = []
    gone_rels: set[str] = set()
    for uid, unit in list(units.items()):
        rel = unit.get("file")
        if not rel:
            continue  # cannot locate the backing file — keep it, never guess
        if not os.path.isfile(os.path.join(project_dir, rel)):
            del units[uid]
            pruned.append(uid)
            gone_rels.add(rel)
    for rel in gone_rels:
        scanned.pop(rel, None)
        if isinstance(baseline_blobs, dict):
            baseline_blobs.pop(rel, None)
    return pruned


_NON_BLOCKING_VERDICTS = frozenset({"verified", NEEDS_CONTRACT})


def blocking_units(state: dict) -> list[dict]:
    """Units the Stop-gate must block on: not `verified` and not `needs_contract`.

    `verified` passed; `needs_contract` is honestly-unverified but is not something
    a source fix can resolve (it needs a generated harness — issue #122), so it is
    reported loudly yet never blocks. Everything else (`violated` / `unknown` /
    `error`, incl. the pre-recorded pending `unknown`) blocks — preserving the
    kill-safety guarantee that a not-yet-verified unit cannot silently pass.
    """
    return [
        u
        for u in state["units"].values()
        if u.get("verdict") not in _NON_BLOCKING_VERDICTS
    ]


def needs_contract_units(state: dict) -> list[dict]:
    """Units marked `needs_contract` — reported as a loud residual, never blocking."""
    return [u for u in state["units"].values() if u.get("verdict") == NEEDS_CONTRACT]


def _needs_contract_verdict(rel: str, function: str, k: int) -> UnitVerdict:
    """The `NEEDS_CONTRACT` verdict for a pointer/array-taking unit (no ESBMC run)."""
    return UnitVerdict(
        f"{rel}::{function}",
        rel,
        function,
        NEEDS_CONTRACT,
        k,
        detail=_NEEDS_CONTRACT_DETAIL,
    )


def verify_and_record(
    file_path: str, *, project_dir: str, k: int = DEFAULT_K
) -> list[UnitVerdict]:
    """Verify each function in `file_path`, persisting every verdict as it lands.

    Kill-safety: the hook has a wall-clock timeout, and verifying a file with
    several functions can exceed it. Up front (under the lock) every current
    function is reconciled and pre-recorded as `unknown`; then each real verdict
    overwrites its entry the moment it lands. So a hook kill at any point leaves
    the not-yet-verified functions as `unknown` — which the Stop-gate blocks on —
    rather than absent; it can never drop an already-found or still-pending
    violation and pass silently.
    """
    rel = unit_id(project_dir, file_path)

    def _blocking_error(detail: str) -> list[UnitVerdict]:
        """Record a blocking `error` verdict for the whole file and return it.

        Neither caller stamps `scanned`: a file we could not read or enumerate
        must not be recorded as already-scanned, or the out-of-band scan would
        treat it as handled and the edit would pass unverified.
        """
        verdict = UnitVerdict(f"{rel}::?", rel, "?", "error", k, detail=detail)
        with gate_lock(project_dir):
            state = load_state(project_dir)
            record(state, verdict)
            state["stop_attempts"] = 0
            save_state(project_dir, state)
        return [verdict]

    try:
        raw = Path(file_path).read_bytes()
    except OSError as exc:
        return _blocking_error(str(exc))

    try:
        # `content=raw` — enumerate a snapshot of the very bytes we are about to
        # hash, never a re-read of the file. Without it `list-units` reads the
        # path again and can land on a transient rewrite: an *empty* `.c`
        # enumerates as a successful empty list (exit 0), so the zero-byte
        # instant a `> f.c`/heredoc passes through would prune every unit this
        # file has — dropping an already-recorded blocking verdict — and then
        # stamp `scanned` with the final content's digest, leaving the Stop-gate
        # and the out-of-band scan both satisfied (issue #141).
        defs = extract_function_defs(file_path, project_dir=project_dir, content=raw)
    except UnitsUnavailable as exc:
        # Couldn't enumerate the file's units (esbmc missing, C parse error, …).
        # Record a blocking `error` verdict rather than skip: a file that was
        # edited but can't be parsed must not pass silently.
        return _blocking_error(str(exc)[:800])

    # Stamp the content hash so a later out-of-band scan treats this exact content
    # as already verified — that dedup is what keeps the Stop-gate from re-blocking
    # (and resetting its patience) on a file nothing has touched since.
    digest = hashlib.sha256(raw).hexdigest()

    # The snapshot guarantees we enumerated `raw`; this guarantees `raw` is still
    # what the file holds. Together they give the stamping invariant: if
    # `scanned[rel]` was set to H, the units recorded alongside it were enumerated
    # from content hashing to H *and* the file still hashed to H right after. A
    # rewrite that lands and stays (A → B) fails closed here rather than relying
    # on the out-of-band scan to re-gate B later — which it cannot do at all
    # outside a git work tree. Compared by content, not by `stat` metadata, so it
    # holds on a filesystem with coarse timestamp granularity too. The block
    # clears on the next edit's reconcile.
    if content_hash(file_path) != digest:
        return _blocking_error(
            "source changed while its units were being enumerated; not recording "
            "a scan of content that was not enumerated — re-edit to re-verify"
        )

    # Reconcile + record every current function BEFORE the slow verifies: drop
    # functions the file no longer defines, reset the Stop-gate's patience, and
    # pre-record each — a pointer/array-taking unit as its final `needs_contract`
    # (we skip its meaningless function-level verify), every other as pending
    # `unknown` so a mid-run kill leaves the not-yet-verified ones blocking
    # rather than absent.
    with gate_lock(project_dir):
        state = load_state(project_dir)
        state["stop_attempts"] = 0
        state.setdefault("scanned", {})[rel] = digest
        prune_missing_units(state, project_dir, file_path, {d.name for d in defs})
        for d in defs:
            if d.takes_pointer:
                record(state, _needs_contract_verdict(rel, d.name, k))
            else:
                record(
                    state,
                    UnitVerdict(
                        f"{rel}::{d.name}",
                        rel,
                        d.name,
                        "unknown",
                        k,
                        detail="verification pending",
                    ),
                )
        save_state(project_dir, state)

    verdicts: list[UnitVerdict] = []
    for d in defs:
        if d.takes_pointer:
            # Signature-based, a priori: skip the (meaningless) function-level
            # verify — no ESBMC call — and report NEEDS_CONTRACT. Classifying by
            # signature, never by matching "dereference failure" in a cex, keeps a
            # genuine out-of-bounds bug (same string) from being suppressed.
            verdicts.append(_needs_contract_verdict(rel, d.name, k))
            continue
        verdict = verify_function(file_path, d.name, project_dir=project_dir, k=k)
        verdicts.append(verdict)
        with gate_lock(project_dir):  # overwrite the pending entry
            state = load_state(project_dir)
            record(state, verdict)
            save_state(project_dir, state)

    # The verifies read the *real* path, not the snapshot: a verdict has to
    # describe the translation unit that actually ships, and the snapshot's
    # `-I <original dir>` stand-in is only an approximation of in-place include
    # resolution (a shadowing header could resolve differently) — plus every
    # counterexample and the trace's `argv` would name a temp file that no longer
    # exists. So this boundary is guarded, not eliminated: re-hash once after the
    # loop and fail closed on drift. Un-stamping is what makes the out-of-band
    # scan re-gate the file, and the `error` blocks when there is no scan to fall
    # back on (outside a git work tree); both clear on the next edit's reconcile.
    #
    # What this catches: a rewrite that lands and *stays* (A → B) during the
    # verifies. What it does NOT catch — the acknowledged residual — is a
    # transient A → B → A: a verdict computed against B stays attached to A's
    # stamp, because the final bytes compare equal. Detecting that needs
    # verification of immutable content, which costs more than it buys (above).
    # So the invariant is precisely: if `scanned[rel]` is H, the units were
    # enumerated from content hashing to H and the file hashed to H both then and
    # after the loop — NOT that every verdict was computed against H.
    if content_hash(file_path) != digest:
        with gate_lock(project_dir):
            state = load_state(project_dir)
            scanned = state.setdefault("scanned", {})
            # Ownership-scoped: only drop the stamp if it is still the one *this*
            # run wrote. A concurrent hook that has since re-stamped its own
            # digest owns the entry, and popping it would re-gate its verified
            # content.
            if scanned.get(rel) == digest:
                scanned.pop(rel, None)
            save_state(project_dir, state)
        return _blocking_error(
            "source changed while its units were being verified; the verdicts "
            "describe content that is no longer on disk — re-edit to re-verify"
        )
    return verdicts

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
import shutil
import subprocess
import sys
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

# The safety-property profile. Bounds / pointer / div-by-zero are ESBMC defaults;
# signed overflow is opt-in, so we add it. Unsigned overflow is intentionally
# left OFF — wraparound is legal and common (hashes, counters) and enabling it
# yields false positives. Tune here; this is the one knob that defines "safe".
SAFETY_FLAGS: tuple[str, ...] = ("--overflow-check",)

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
# agent never touched (the over-reach a whole-tree scan couldn't avoid).
# `FORSETI_GATE_INCLUDE`/`FORSETI_GATE_EXCLUDE` narrow it further; a bare path
# segment (`vendor`) prunes any directory of that name, a glob (`*_generated.c`,
# `test/*`) is matched against the project-relative path.
_DEFAULT_EXCLUDE_GLOBS: tuple[str, ...] = ("third_party", "vendor", "node_modules")

_STATE_DIR = ".forseti"
_STATE_FILE = "gate_state.json"
_LOCK_FILE = "gate_state.lock"

# Control-flow keywords that a permissive definition regex could mistake for a
# function name; filtered out belt-and-suspenders.
_KEYWORDS = {
    "if",
    "for",
    "while",
    "switch",
    "do",
    "else",
    "return",
    "sizeof",
    "case",
    "default",
    "goto",
}

# A top-level C function *definition*: starts at column 0 with at least one
# return-type token, then `name(params)` and an opening `{` (a trailing `;`
# makes it a prototype, which `[^;{}]*` excludes). Heuristic, not a parser —
# good enough for the small kernels this gate targets; documented in the README.
_FUNC_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_ \t\*]*?[ \t\*]"
    r"([A-Za-z_][A-Za-z0-9_]*)"
    r"[ \t]*\(([^;{}]*)\)[ \t\r\n]*\{",
    re.MULTILINE,
)

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
    (`NEEDS_CONTRACT`).
    """

    name: str
    takes_pointer: bool


# C comments (block `/* ... */`, possibly multi-line, and line `// ...`) so a `*`
# inside a comment in the parameter list is not mistaken for a pointer declarator.
_C_COMMENT_RE = re.compile(r"/\*.*?\*/|//[^\n]*", re.DOTALL)


def _params_take_pointer(params: str) -> bool:
    """True if a C parameter list has a pointer or array parameter.

    Heuristic (matches the definition regex's reach, not a parser): a `*` or `[`
    anywhere in the parameter text — after stripping comments, so `/* ... */` /
    `// ...` don't false-positive — means at least one parameter is a pointer, or
    an array that decays to a pointer at the function boundary. Still misses a
    pointer hidden behind a typedef, or a `*` inside a string literal; a real C
    parse is the robust fix (documented in the README).
    """
    cleaned = _C_COMMENT_RE.sub(" ", params)
    return "*" in cleaned or "[" in cleaned


def extract_function_defs(source_text: str) -> list[FuncDef]:
    """Top-level function definitions in `source_text` (heuristic, deduped by name)."""
    seen: set[str] = set()
    defs: list[FuncDef] = []
    for match in _FUNC_RE.finditer(source_text):
        name = match.group(1)
        if name in _KEYWORDS or name in seen:
            continue
        seen.add(name)
        defs.append(FuncDef(name, _params_take_pointer(match.group(2))))
    return defs


def extract_functions(source_text: str) -> list[str]:
    """Names of top-level functions defined in `source_text` (heuristic, deduped)."""
    return [d.name for d in extract_function_defs(source_text)]


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


def _parse_porcelain_z(out: str) -> list[str]:
    """Relative paths from ``git status --porcelain -z`` (NUL-separated, unquoted).

    Each record is ``XY<space>path``; a rename/copy record is followed by a second
    NUL-separated field holding the *original* path, which we skip (the current
    path — the first field — is what we verify). ``-z`` means paths are never
    quoted, so no unescaping is needed.
    """
    tokens = out.split("\0")
    paths: list[str] = []
    i = 0
    while i < len(tokens):
        entry = tokens[i]
        i += 1
        if not entry or len(entry) < 4:
            continue
        status, path = entry[:2], entry[3:]
        if "R" in status or "C" in status:
            i += 1  # the following token is the rename/copy source — skip it
        paths.append(path)
    return paths


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


def git_changed_files(project_dir: str) -> list[str] | None:
    """Repo-root-relative paths `git status` reports as changed, or ``None``.

    ``--untracked-files=all`` so a brand-new Bash-written C file (untracked) is
    seen; git's own ignore rules already drop gitignored build/vendor output, so
    the scan never sweeps generated trees. Paths are relative to the repository
    root (git's porcelain contract), not `project_dir`.
    """
    out = _git(project_dir, "status", "--porcelain", "-z", "-uall")
    return None if out is None else _parse_porcelain_z(out)


def discover_changed_c_sources(project_dir: str) -> list[str] | None:
    """Absolute paths of changed, still-present C sources under `project_dir`.

    git reports paths relative to the *repository root*, which need not be
    `project_dir`, so they are joined to the resolved root and then scoped back to
    `project_dir` (a subdir checkout gates only its own changes). Include/exclude
    globs are applied to the project-relative path, matching `unit_id`. ``None``
    when discovery is unavailable (not a git repo).
    """
    root = _git(project_dir, "rev-parse", "--show-toplevel")
    rels = git_changed_files(project_dir)
    if root is None or rels is None:
        return None
    root = root.strip()
    # Keep the returned path expressed relative to the raw `project_dir` so its
    # `unit_id`/`scanned` key matches what `verify_and_record` stamps; realpath is
    # used only to compare against the (possibly symlinked) project subtree.
    proj_real = os.path.realpath(project_dir)
    found: list[str] = []
    for rel in rels:
        abspath = os.path.join(root, rel)
        # A path git reports changed but that is gone from disk (a Bash `rm`) is
        # skipped here — there is nothing to *verify*. Its recorded units are
        # reconciled separately by `prune_deleted_units`, which keys off actual
        # file existence so it also catches an untracked file git never tracked
        # (issue #99 review): keep discovery about "what to verify", pruning about
        # "what no longer exists".
        if not is_c_source(abspath) or not os.path.isfile(abspath):
            continue
        try:
            if os.path.commonpath([proj_real, os.path.realpath(abspath)]) != proj_real:
                continue  # changed outside this project subtree — out of scope
        except ValueError:
            continue  # different drive/root — cannot be under proj
        if _included(os.path.relpath(abspath, project_dir)):
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


def baseline_scanned(project_dir: str) -> int | None:
    """Mark the currently-dirty C tree as "seen" at session start (issue #99).

    The out-of-band scan gates C whose content differs from the recorded
    `scanned` hash. Without a baseline that is *everything* `git status` reports —
    including C that was dirty before the session and the agent never touched, the
    over-reach this issue was careful to avoid. Seeding `scanned` with each
    pre-session dirty file's current hash scopes the gate to "changed **since
    session start**": those files are gated only once the agent actually modifies
    them. Returns the number baselined, or ``None`` if not a git repo.
    """
    discovered = discover_changed_c_sources(project_dir)
    if discovered is None:
        return None
    with gate_lock(project_dir):
        state = load_state(project_dir)
        baseline: dict[str, str] = {}
        for abspath in discovered:
            digest = content_hash(abspath)
            if digest is not None:
                baseline[unit_id(project_dir, abspath)] = digest
        state["scanned"] = baseline
        save_state(project_dir, state)
    return len(baseline)


def verify_function(
    file_path: str, function: str, *, project_dir: str, k: int = DEFAULT_K
) -> UnitVerdict:
    """Run ``forseti verify`` on one function and map its JSON payload to a verdict."""
    rel = unit_id(project_dir, file_path)
    uid = f"{rel}::{function}"
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
            return state
        except (json.JSONDecodeError, OSError):
            pass
    return {"units": {}, "stop_attempts": 0, "scanned": {}}


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
    absent and clearing each such file's `scanned` baseline (so a same-name file
    recreated later re-verifies from scratch).

    Keys off each unit's recorded `file` (project-relative), not `git status`, so
    it also catches an untracked Bash-written file git never knew existed — the
    case a git-scoped deletion scan would miss. Only ever *removes* already-recorded
    units (files the agent touched), so it can never over-reach into gating C the
    agent left alone. Returns the pruned unit ids.
    """
    units = state.get("units", {})
    scanned = state.get("scanned", {})
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
    try:
        raw = Path(file_path).read_bytes()
    except OSError as exc:
        verdict = UnitVerdict(f"{rel}::?", rel, "?", "error", k, detail=str(exc))
        with gate_lock(project_dir):
            state = load_state(project_dir)
            record(state, verdict)
            state["stop_attempts"] = 0
            save_state(project_dir, state)
        return [verdict]

    # Decode leniently (a stray non-UTF-8 byte must not crash the hook) and stamp
    # the content hash so a later out-of-band scan treats this exact content as
    # already verified — that dedup is what keeps the Stop-gate from re-blocking
    # (and resetting its patience) on a file nothing has touched since.
    defs = extract_function_defs(raw.decode("utf-8", "replace"))
    digest = hashlib.sha256(raw).hexdigest()

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
    return verdicts

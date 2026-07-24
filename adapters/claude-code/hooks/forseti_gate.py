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
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from pathlib import Path

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

# Budget for the one `forseti list-units` parse per edited file. A `--parse-tree-only`
# run does no solving, so it is fast; keep it well under the verify budget.
LIST_UNITS_TIMEOUT_S = float(os.environ.get("FORSETI_LIST_UNITS_TIMEOUT_S", "30"))

# How many times the Stop-gate blocks before it gives up and lets the turn end
# with a LOUD unverified residual (never a silent pass, but never an infinite
# loop either).
MAX_STOP_ATTEMPTS = 3

C_SUFFIXES = {".c", ".h"}

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


class UnitsUnavailable(RuntimeError):
    """`forseti list-units` could not enumerate a file's function definitions.

    Distinct from "the file defines no functions" (an empty list): a failed
    enumeration must surface as a blocking `error` verdict, never be mistaken for
    a clean pass (kill-safety — a not-verified unit can't silently slip through).
    """


def extract_function_defs(
    file_path: str | os.PathLike[str], *, project_dir: str
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

    Only ``.c`` translation units are enumerated: ESBMC cannot parse a header
    standalone (``forseti verify`` errors on a ``.h`` too — "failed to figure out
    type of file"), and clang's path-match keeps a header-resident definition out
    of its includer's unit list, so functions defined in a ``.h`` are simply out
    of gate scope. A non-``.c`` file yields ``[]`` (a clean pass) rather than an
    unresolvable block.
    """
    if Path(file_path).suffix.lower() != ".c":
        return []
    argv = [
        *resolve_forseti_cmd(),
        "list-units",
        str(file_path),
        "--json",
        "--timeout",
        str(int(LIST_UNITS_TIMEOUT_S)),
    ]
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
    try:
        return [
            FuncDef(str(u["function"]), bool(u.get("takes_pointer")))
            for u in payload["units"]
        ]
    except (AttributeError, KeyError, TypeError) as exc:
        raise UnitsUnavailable(f"malformed list-units payload: {exc}") from exc


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
            return state
        except (json.JSONDecodeError, OSError):
            pass
    return {"units": {}, "stop_attempts": 0}


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
        defs = extract_function_defs(file_path, project_dir=project_dir)
    except UnitsUnavailable as exc:
        # Couldn't enumerate the file's units (esbmc missing, C parse error, …).
        # Record a blocking `error` verdict rather than skip: a file that was
        # edited but can't be parsed must not pass silently.
        verdict = UnitVerdict(f"{rel}::?", rel, "?", "error", k, detail=str(exc)[:800])
        with gate_lock(project_dir):
            state = load_state(project_dir)
            record(state, verdict)
            state["stop_attempts"] = 0
            save_state(project_dir, state)
        return [verdict]

    # Reconcile + record every current function BEFORE the slow verifies: drop
    # functions the file no longer defines, reset the Stop-gate's patience, and
    # pre-record each — a pointer/array-taking unit as its final `needs_contract`
    # (we skip its meaningless function-level verify), every other as pending
    # `unknown` so a mid-run kill leaves the not-yet-verified ones blocking
    # rather than absent.
    with gate_lock(project_dir):
        state = load_state(project_dir)
        state["stop_attempts"] = 0
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

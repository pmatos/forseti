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

import json
import os
import re
import shutil
import subprocess
import sys
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

# How many times the Stop-gate blocks before it gives up and lets the turn end
# with a LOUD unverified residual (never a silent pass, but never an infinite
# loop either).
MAX_STOP_ATTEMPTS = 3

C_SUFFIXES = {".c", ".h"}

_STATE_DIR = ".forseti"
_STATE_FILE = "gate_state.json"

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
    r"[ \t]*\([^;{}]*\)[ \t\r\n]*\{",
    re.MULTILINE,
)


@dataclass(frozen=True)
class UnitVerdict:
    """One function's verdict from a single ``forseti verify`` call."""

    unit_id: str  # "relpath::symbol"
    file: str
    function: str
    verdict: str  # verified | violated | unknown | error
    k: int
    counterexample: str | None = None
    detail: str | None = None

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


def extract_functions(source_text: str) -> list[str]:
    """Names of top-level functions defined in `source_text` (heuristic, deduped)."""
    seen: list[str] = []
    for match in _FUNC_RE.finditer(source_text):
        name = match.group(1)
        if name not in _KEYWORDS and name not in seen:
            seen.append(name)
    return seen


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
    return UnitVerdict(
        uid,
        rel,
        function,
        verdict,
        int(payload.get("unwind", k)),
        counterexample=payload.get("counterexample"),
        detail=payload.get("reason") or payload.get("message"),
    )


def verify_file(
    file_path: str, *, project_dir: str, k: int = DEFAULT_K
) -> list[UnitVerdict]:
    """Verify every top-level function defined in `file_path`."""
    try:
        text = Path(file_path).read_text()
    except OSError as exc:
        return [
            UnitVerdict(
                unit_id(project_dir, file_path),
                file_path,
                "?",
                "error",
                k,
                detail=str(exc),
            )
        ]
    return [
        verify_function(file_path, fn, project_dir=project_dir, k=k)
        for fn in extract_functions(text)
    ]


def _gate_path(project_dir: str) -> Path:
    return Path(project_dir) / _STATE_DIR / _STATE_FILE


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
    path = _gate_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True))


def record(state: dict, verdict: UnitVerdict) -> None:
    state["units"][verdict.unit_id] = asdict(verdict)


def prune_file_units(state: dict, project_dir: str, file_path: str) -> None:
    """Drop every tracked unit belonging to `file_path`.

    `record` only ever upserts, so a function that was renamed or removed as part
    of a fix would leave its stale (often `violated`) entry behind and the
    Stop-gate would block forever on a unit that no longer exists. The caller
    prunes here and then re-records whatever functions the file still defines, so
    the tracked set always reflects the current file.
    """
    prefix = f"{unit_id(project_dir, file_path)}::"
    for uid in [u for u in state["units"] if u.startswith(prefix)]:
        del state["units"][uid]


def unverified_units(state: dict) -> list[dict]:
    """Every tracked unit whose latest verdict is not `verified`."""
    return [u for u in state["units"].values() if u.get("verdict") != "verified"]

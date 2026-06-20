"""Invoke ESBMC and classify its output into a typed `EsbmcResult`."""

from __future__ import annotations

import re
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path

from .cex_parser import Frontend, parse_counterexample
from .result import (
    EsbmcResult,
    Error,
    RunMeta,
    Unknown,
    UnknownReason,
    Verified,
    Violated,
)

_FAILED = "VERIFICATION FAILED"
_SUCCESSFUL = "VERIFICATION SUCCESSFUL"
_CEX_START = "[Counterexample]"


def _has_banner(text: str, banner: str) -> bool:
    """True only if `banner` appears as a standalone output line.

    ESBMC prints its verdict on its own line. Requiring a line match keeps a
    broken invocation that merely *echoes* the banner text (a frontend
    diagnostic quoting source, a preprocessor error) from being read as a
    verdict, per the "never silently pass" invariant.
    """
    return any(line.strip() == banner for line in text.splitlines())


def _counterexample(text: str) -> str:
    """The trace text spanning `[Counterexample]` up to the terminal FAILED banner.

    Slices at the *last* standalone FAILED line, not the first substring match —
    so a `VERIFICATION FAILED` echoed inside the `Violated property:` block (e.g.
    an `__ESBMC_assert` message) doesn't truncate the trace early. Callers only
    invoke this once a standalone FAILED banner is known to exist.
    """
    lines = text.splitlines()
    cut = len(lines)
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip() == _FAILED:
            cut = i
            break
    body = "\n".join(lines[:cut])
    start = body.find(_CEX_START)
    return (body[start:] if start != -1 else body).strip()


def _error_message(text: str) -> str:
    """First `ERROR:`-prefixed line, so a known failure is self-describing."""
    for line in text.splitlines():
        if line.startswith("ERROR:"):
            return line[len("ERROR:") :].strip()
    return "esbmc reported an error"


def classify(meta: RunMeta, frontend: Frontend = Frontend.C) -> EsbmcResult:
    """Map one finished ESBMC run to a verdict.

    Classification keys on stdout/stderr markers, never the exit code (a
    timeout and a real violation both exit 1). Known invocation errors are
    checked before any verdict banner, and the SUCCESSFUL/FAILED banners are
    matched only as standalone lines — so a broken invocation that merely
    echoes banner text (a parse diagnostic quoting source) is never read as a
    verdict. Unrecognised output becomes `Error`, never a verdict. On VIOLATED
    the raw trace is parsed into a typed `Counterexample` for `frontend`;
    parsing returns `None` rather than disturbing the verdict on failure.
    """
    text = meta.stdout + "\n" + meta.stderr
    if meta.exit_code == 6 or "PARSING ERROR" in text or "failed to open input file" in text:
        return Error(meta, _error_message(text))
    if _has_banner(text, _FAILED):
        raw = _counterexample(text)
        return Violated(meta, raw, parse_counterexample(raw, frontend))
    if _has_banner(text, _SUCCESSFUL):
        return Verified(meta)
    if "VERIFICATION UNKNOWN" in text:
        return Unknown(meta, UnknownReason.UNCLASSIFIED)
    if "Timed out" in text:
        return Unknown(meta, UnknownReason.TIMEOUT)
    if "Out of memory" in text:
        return Unknown(meta, UnknownReason.MEMOUT)
    return Error(meta, "unclassified output")


# Wall-clock slack added on top of esbmc's own --timeout before we hard-kill it.
_GRACE_S = 5.0
_VERSION_RE = re.compile(r"ESBMC version (\S+)")


def _parse_version(text: str) -> str:
    match = _VERSION_RE.search(text)
    return match.group(1) if match else ""


def _as_text(value: str | bytes | None) -> str:
    """Captured subprocess output as text (decode bytes, treat None as empty)."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


def verify(
    source: Path,
    *,
    unwind: int,
    timeout_s: float | None = None,
    extra_flags: Sequence[str] = (),
    esbmc_bin: str = "esbmc",
    frontend: Frontend = Frontend.C,
) -> EsbmcResult:
    """Run ESBMC on a `source` and return the typed verdict.

    Uses the recommended `--unwind N --no-unwinding-assertions` bound. `unwind`
    is required and recorded in the result's `argv`, so a VERIFIED stays
    qualified as "verified up to k". When `timeout_s` is set, esbmc's own
    `--timeout` is used (it yields a clean `Timed out`), with a wall-clock
    backstop that maps a Python-level timeout to `Unknown(TIMEOUT)`. `frontend`
    selects the counterexample parser (C only, for now).
    """
    cmd = [
        esbmc_bin,
        str(source),
        "--unwind",
        str(unwind),
        "--no-unwinding-assertions",
        *extra_flags,
    ]
    if timeout_s is not None:
        cmd += ["--timeout", f"{int(timeout_s)}s"]
    argv = tuple(cmd)
    proc_timeout = timeout_s + _GRACE_S if timeout_s is not None else None

    start = time.monotonic()
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=proc_timeout
        )
    except OSError as exc:
        # The binary never ran (missing, non-executable, a directory, ...);
        # record the intended argv for provenance. FileNotFoundError is an
        # OSError subclass, so a missing binary keeps its specific message;
        # PermissionError and other OSErrors become a typed Error too, never a
        # leaked exception, per the wrapper's "invocation failures are Error"
        # invariant.
        meta = RunMeta(
            esbmc_version="",
            argv=argv,
            exit_code=-1,
            duration_s=time.monotonic() - start,
            stdout="",
            stderr="",
        )
        message = (
            f"esbmc binary not found: {esbmc_bin}"
            if isinstance(exc, FileNotFoundError)
            else f"esbmc invocation failed: {esbmc_bin}: {exc}"
        )
        return Error(meta, message)
    except subprocess.TimeoutExpired as exc:
        meta = RunMeta(
            esbmc_version="",
            argv=argv,
            exit_code=-1,
            duration_s=time.monotonic() - start,
            stdout=_as_text(exc.stdout),
            stderr=_as_text(exc.stderr),
        )
        return Unknown(meta, UnknownReason.TIMEOUT)

    meta = RunMeta(
        esbmc_version=_parse_version(proc.stdout) or _parse_version(proc.stderr),
        argv=argv,
        exit_code=proc.returncode,
        duration_s=time.monotonic() - start,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )
    return classify(meta, frontend)

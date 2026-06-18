"""Invoke ESBMC and classify its output into a typed `EsbmcResult`."""

from __future__ import annotations

import re
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path

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
_CEX_START = "[Counterexample]"


def _counterexample(text: str) -> str:
    """The trace text spanning `[Counterexample]` up to the FAILED banner."""
    body = text[: text.find(_FAILED)] if _FAILED in text else text
    start = body.find(_CEX_START)
    return (body[start:] if start != -1 else body).strip()


def _error_message(text: str) -> str:
    """First `ERROR:`-prefixed line, so a known failure is self-describing."""
    for line in text.splitlines():
        if line.startswith("ERROR:"):
            return line[len("ERROR:") :].strip()
    return "esbmc reported an error"


def classify(meta: RunMeta) -> EsbmcResult:
    """Map one finished ESBMC run to a verdict.

    Classification keys on stdout/stderr markers, never the exit code (a
    timeout and a real violation both exit 1). The failure banner is checked
    first, so a run is never read as VERIFIED while any failure marker is
    present. Unrecognised output becomes `Error`, never a verdict.
    """
    text = meta.stdout + "\n" + meta.stderr
    if _FAILED in text:
        return Violated(meta, _counterexample(text))
    if "VERIFICATION SUCCESSFUL" in text:
        return Verified(meta)
    if "VERIFICATION UNKNOWN" in text:
        return Unknown(meta, UnknownReason.UNCLASSIFIED)
    if "Timed out" in text:
        return Unknown(meta, UnknownReason.TIMEOUT)
    if "Out of memory" in text:
        return Unknown(meta, UnknownReason.MEMOUT)
    if meta.exit_code == 6 or "PARSING ERROR" in text or "failed to open input file" in text:
        return Error(meta, _error_message(text))
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
) -> EsbmcResult:
    """Run ESBMC on a C `source` and return the typed verdict.

    Uses the recommended `--unwind N --no-unwinding-assertions` bound. `unwind`
    is required and recorded in the result's `argv`, so a VERIFIED stays
    qualified as "verified up to k". When `timeout_s` is set, esbmc's own
    `--timeout` is used (it yields a clean `Timed out`), with a wall-clock
    backstop that maps a Python-level timeout to `Unknown(TIMEOUT)`.
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
    except FileNotFoundError:
        # The binary never ran; record the intended argv for provenance.
        meta = RunMeta(
            esbmc_version="",
            argv=argv,
            exit_code=-1,
            duration_s=time.monotonic() - start,
            stdout="",
            stderr="",
        )
        return Error(meta, f"esbmc binary not found: {esbmc_bin}")
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
    return classify(meta)
